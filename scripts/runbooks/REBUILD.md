# Rebuilding after a hardware failure

How to bring the NAS or the Proxmox host back after a reinstall, using this repo. Ansible restores the
configuration, but a few things must exist before it can run. Those prerequisites are below. Values are named
by their variable in `group_vars/all/local.yml`; the real values live only there.

## What comes back, and what doesn't

| Failure | What survives | What Ansible restores | What you do by hand |
| --- | --- | --- | --- |
| NAS boot drive | The data pools, with every dataset and its properties | Network, pool import, settings, NFS shares, services | Reinstall, API user and key, certificate pin |
| One disk of the RAIDZ1 pool (`persistent`) | The pool, degraded | Nothing until the pool is healthy: the role only imports ONLINE pools | Replace the disk and resilver (see [Known limits](#known-limits)) |
| The single disk of `ephemeral` | Nothing: that pool and the media on it are lost | A new, empty pool on a blank replacement disk, then its datasets and share | Allow pool creation and list the new disk |
| NAS network card | Everything on disk | Everything, once the NAS is reachable | Update the router's DHCP reservation to the new MAC |
| Proxmox boot drive | VM disks on the NAS, and the VMs' nightly backups | The no-subscription repository, the upgrade, Tailscale with its guard, the NFS storages, the start-on-boot delay and the wait for the NAS, and the backup job | Reinstall, root SSH key; with remote access, delete the old machine in Tailscale's admin console and put a new auth key in the vault; restore the VMs from their backups ([vm-restore.md](vm-restore.md)) |
| Proxmox boot drive, in a setup without a NAS | The backups, if `vm_backup_storage` is a USB disk or a backup server | The repository, the upgrade, Tailscale with its guard, the check that `local-lvm` can hold VM disks, and the backup job | Reinstall, root SSH key; with remote access, the old machine and a new auth key as above; re-add the backup storage before `make proxmox`; restore the VMs ([vm-restore.md](vm-restore.md)) |
| The controller (this Mac) | The repo on GitHub, with a NAS the vault (encrypted) | — | Restore `.vault_pass` and `local.yml` from your password manager, and without a NAS your own vault too |

## 1. The controller

Everything runs from a checkout of this repo.

1. Install Poetry outside any project environment (`pipx install poetry`; see
   https://python-poetry.org/docs/#installation), then clone the repo and create the Python environment:
   `pyenv virtualenv 3.14.6 homeserver && pyenv activate homeserver && make venv`
   (or any Python 3.12+: `make venv PYTHON=/path/to/python3`).
2. `make init`, which creates `inventory` and `group_vars/all/local.yml` from their examples.
3. Restore the two files only you have:
   - **`.vault_pass`**: the vault password, from your password manager, readable by you alone
     (`chmod 600 .vault_pass`).
   - **`group_vars/all/vault.yml`**, without a NAS, if you put secrets of your own in it: your own vault, in no
     repository. Restore it from your password manager over the tracked one, then
     `git update-index --skip-worktree group_vars/all/vault.yml` so it stays out of your commits; `make vault-init`
     then says `nothing to do`. With nothing of yours in it, `make vault-init` makes both files (see `README.md`).
   - **`group_vars/all/local.yml`**: addresses, disk serials and the certificate pin. Keep a copy of this file in
     your password manager. If it's lost, refill it from `local.yml.example`; the comments there say where each
     value comes from.
4. Check the toolchain works: `make lint && make test-idempotence`.

Run `make` with the environment active (`pyenv activate homeserver`), or pass `VENV=<path to the env>`.

## 2. The NAS

### Before Ansible can run

These can't be done by Ansible, because they are what it needs in order to connect.

1. **Install TrueNAS Community Edition** on the boot drive. Use the release the role was written for
   (`truenas_supported_version`); a different major.minor makes the role stop and ask for a review. Set the
   `truenas_admin` password in the installer. **Don't import pools, create shares or change settings in the web
   UI.** The role does all of that, and anything done by hand gets in its way.
2. **Connect the LAN port.** The router's DHCP reservation for the NAS's MAC gives a fresh install its usual
   address, so `truenas_bootstrap_address` and `truenas_static_address` stay as they are. If the network card was
   replaced, its MAC changed: update the reservation, or put whatever address DHCP handed out into
   `truenas_bootstrap_address`.
3. **Create the API user and its key** in the web UI, logged in as `truenas_admin` over **HTTPS**. TrueNAS revokes
   an API key sent over plain HTTP.
   - Credentials → Users → Add: a local user (the old one was `claude-on-proxmox`), with the auxiliary group
     `builtin_administrators` (which gives it the Full Admin role), SSH access off.
   - Create an API key owned by that user, with no expiry. Copy it once; TrueNAS won't show it again.
   - Put it in the vault: `make vault-edit`, then replace `vault_truenas_api_key`.
4. **Update the certificate pin.** A new install generates a new self-signed web certificate, so the old pin no
   longer matches. Until it does, the role refuses to connect with "not the pinned one; the API key was not sent".
   Read the new fingerprint and put it in `truenas_api_cert_sha256` in `local.yml`:
   `openssl s_client -connect <nas address>:443 </dev/null | openssl x509 -noout -fingerprint -sha256`
5. **If a data disk was replaced**, read [Known limits](#known-limits) first.

### Running it

Run one stage at a time and read each result. Every run writes a log to `logs/`. TrueNAS allows 20 API logins a
minute and a full dry run uses about 16, so leave a minute between a full dry run and a real run.

| Step | Command | Expect |
| --- | --- | --- |
| 1 | `make check-truenas` | No failures. The plan shows the static address, the pin of the system dataset, the pool import, settings, shares and services |
| 2 | `make truenas TAGS=truenas_network` | DHCP off, static address applied. If the role can't reconnect, TrueNAS rolls back by itself after 90 s |
| 3 | `make truenas TAGS=truenas_pools` | System dataset pinned to the boot pool, both pools imported. Don't accept TrueNAS's offer to upgrade pool features |
| 4 | `make check-truenas TAGS=truenas_datasets` | No change: the datasets and their properties came back with the pools. Only a recreated pool shows new datasets |
| 5 | `make truenas` | HTTPS redirect, snapshot tasks, NFS shares, alert mail, NFS started. Shares, tasks and settings live in TrueNAS's own database, so they come back here. The mail password comes from the vault |
| 6 | `make truenas` | `changed=0` |

The Proxmox host needs nothing: its NFS storages reconnect once the shares exist. `make check-proxmox` should
report no change, and `pvesm status` on the host should show every NFS storage active.

## 3. The Proxmox host

### Before Ansible can run

1. **Install Proxmox VE.** In the installer, pick the port cabled to the LAN as the management interface, give it
   `proxmox_address` with the LAN's prefix, and set the gateway, DNS server and hostname. The installer creates the
   bridge `vmbr0` on that port, and the bridge takes its MAC, which is the one the router's DHCP reservation must name. **Leave the web UI's Updates → Repositories panel alone**: the fresh install's
   enterprise repositories and missing no-subscription one are what `make proxmox` fixes first.
2. **Give the controller root SSH access.** The old host key no longer matches, so remove it first:
   `ssh-keygen -R <proxmox address>`. Then install the controller's key for this host with one password login:
   `ssh-copy-id -f -i ~/.ssh/<key>.pub -o PubkeyAuthentication=no root@<proxmox address>`. With the old key
   gone, nothing vouches for the host, so before typing the root password compare the fingerprint ssh shows
   with the one `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` prints on the Proxmox console. The flags
   matter on Proxmox VE 9, whose OpenSSH 10 enables `PerSourcePenalties`. A plain `ssh-copy-id` first tries
   each of the agent's keys in a connection of its own, to see which are installed. Each failed connection adds
   5 s of penalty, and once 15 s have built up, after three keys, sshd resets the next connection with
   `kex_exchange_identification: read: Connection reset by peer`. `-f` skips those tries, and
   `PubkeyAuthentication=no` takes the install connection straight to the password. `-f` also skips the check
   for a key already installed, so a second run adds the key again. The controller keeps one key per machine,
   with a `Host` block in `~/.ssh/config` naming the key and `IdentitiesOnly yes`, so Ansible, which connects
   by address, offers the right one.
3. **If the network card was replaced**, update the router's DHCP reservation. The bridge carries the card's MAC.
4. **With remote access** (`tailscale_advertise_routes` in `local.yml`), the host's tailnet identity was on the old
   boot drive. In Tailscale's admin console, delete the old machine, or the new one is named `<hostname>-1`. Then make
   a new one-off, pre-approved auth key tagged `tag:homeserver` and put it in the vault as `vault_tailscale_auth_key`
   (`make vault-edit`). The tailnet's policy, with its route auto-approver, and split DNS live in the console and
   survive. Until this run is done, remote access is down, so do it from home.
   [tailscale.md](tailscale.md) has the details.
5. **Without a NAS, with `vm_backup_storage`**: re-add that storage by hand, under the same id, before
   `make check-proxmox`, which refuses a backup storage that does not exist. A USB disk goes back as a checked
   mount point, as `scripts/runbooks/vm-restore.md` (step 1 of a real restore) shows; the backups on it are what
   the VMs come back from.

### Running it

| Step | Command | Expect |
| --- | --- | --- |
| 1 | `make check-proxmox` | A `proxmox.sources` file to write, `Enabled: false` for `pve-enterprise` and `ceph`, and no pending upgrade yet (the package lists are refreshed only for real). The plan shows `add` for the NFS storages (`homeserver-persistent`, `homeserver-ephemeral`, `claude-on-proxmox-ephemeral`, `homeserver-backup`) and, when `local.yml` names VMs to back up, `create` for the `vm-nightly` backup job, its storage pending. Without a NAS: `local-lvm: present` and nothing to add, and the backup job on `vm_backup_storage`, re-added in step 5 above. With remote access: Tailscale to install, the guard to load, forwarding to turn on, and "Would log the node in with the auth key" |
| 2 | `make proxmox` | Repositories written, package lists refreshed, packages upgraded, and, since that brings a newer kernel, "Reboot the node when it is safe to": reboot the host by hand. With remote access: Tailscale installed, the guard loaded, the host logged in and its route approved ("Nothing for the admin console to fix"). The storages added and active, start-on-boot delay set, backup job created. Without a NAS: no storage change |
| 3 | `make proxmox` | `changed=0` |
| 4 | `pveum aclmod /storage/claude-on-proxmox-ephemeral -user <user> -role <role>` | With a NAS: the grant for each project pool, with the user and role from that project's README, which also creates them on a fresh host |

A fresh install carries none of the old `truenas-*` storage ids, so nothing needs removing; `README.md`'s
section on storage for projects that create VMs says what each project is then told.

The VM definitions lived on the old boot drive (`/etc/pve`) and are gone. Restore the VMs from their nightly
backups, which `scripts/runbooks/vm-restore.md` describes, before anything creates them anew. With a NAS, the
backups are on it; without one, the backups are wherever `vm_backup_storage` pointed.
VMs that other projects create on the host (claude-on-proxmox's) are theirs to recreate.

## Known limits

- **A degraded pool is not imported.** The role imports only pools that report ONLINE, so with one failed disk
  in `persistent` it stops with "can be imported but is DEGRADED". Replacing the disk and resilvering the pool is
  not automated yet (`TODO.md`). Doing it in the web UI breaks the rule that every change goes through Ansible, so
  decide deliberately.
- **Recreating `ephemeral` erases nothing only on a blank disk.** To create the pool on a replacement disk, set
  `truenas_pool_allow_create: true` and list the new disk's serial under `truenas_pool_disks.ephemeral` in
  `local.yml`. The role refuses any disk that has partitions or belongs to a pool. Set the flag back to `false`
  afterwards.
- **Email alerts need a mail server** in `local.yml` and its password in the vault (`TODO.md`); without them a
  failing disk is only visible in the web UI.
- **TrueNAS's audit log keeps 7 days.** Before a planned rebuild, its Method Call entries show whether anything was
  changed outside Ansible since the last run.
