# Rebuilding after a hardware failure

How to bring the NAS or the Proxmox host back after a reinstall, using this repo. Ansible restores the
configuration, but a few things must exist before it can run. Those prerequisites are below. Values are named
by their variable in `group_vars/all/local.yml`; the real values live only there.

## What comes back, and what doesn't

| Failure | What survives | What Ansible restores | What you do by hand |
| --- | --- | --- | --- |
| NAS boot drive | The data pools, with every dataset and its properties | Network, pool import, settings, NFS shares, services | Reinstall, API user and key, certificate pin |
| One disk of the RAIDZ1 pool (`nvme_gen3`) | The pool, degraded | Nothing until the pool is healthy: the role only imports ONLINE pools | Replace the disk and resilver (see [Known limits](#known-limits)) |
| The single disk of `ephemeral` | Nothing: that pool and the media on it are lost | A new, empty pool on a blank replacement disk, then its datasets and share | Allow pool creation and list the new disk |
| NAS network card | Everything on disk | Everything, once the NAS is reachable | Update the router's DHCP reservation to the new MAC |
| Proxmox boot drive | VM disks, on the NAS | Both NFS storages and the start-on-boot delay | Reinstall, root SSH key; recreate VM definitions |
| The controller (this Mac) | The repo on GitHub, the vault (encrypted) | — | Restore `.vault_pass` and `local.yml` from your password manager |

## 1. The controller

Everything runs from a checkout of this repo.

1. Clone the repo and create the Python environment:
   `pyenv virtualenv 3.14.6 homeserver && pyenv activate homeserver && pip install -r requirements.txt`
   (or any Python 3.12+: `make venv PYTHON=/path/to/python3`).
2. `make init`, which creates `inventory` and `group_vars/all/local.yml` from their examples.
3. Restore the two files only you have:
   - **`.vault_pass`**: the vault password, from your password manager.
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
minute and a full dry run uses about 13, so leave a minute between a full dry run and a real run.

| Step | Command | Expect |
| --- | --- | --- |
| 1 | `make check-truenas` | No failures. The plan shows the static address, the pin of the system dataset, the pool import, settings, shares and services |
| 2 | `make truenas TAGS=truenas_network` | DHCP off, static address applied. If the role can't reconnect, TrueNAS rolls back by itself after 90 s |
| 3 | `make truenas TAGS=truenas_pools` | System dataset pinned to the boot pool, both pools imported. Don't accept TrueNAS's offer to upgrade pool features |
| 4 | `make check-truenas TAGS=truenas_datasets` | No change: the datasets and their properties came back with the pools. Only a recreated pool shows new datasets |
| 5 | `make truenas` | HTTPS redirect, NFS shares, NFS started. Shares and settings live in TrueNAS's own database, so they come back here |
| 6 | `make truenas` | `changed=0` |

The Proxmox host needs nothing: its NFS storages reconnect once the shares exist. `make check-proxmox` should
report no change, and `pvesm status` on the host should show both storages active.

## 3. The Proxmox host

### Before Ansible can run

1. **Install Proxmox VE.** In the installer, pick the 40 GbE port as the management interface, give it
   `proxmox_address` with the LAN's prefix, and set the gateway, DNS server and hostname. The installer creates the
   bridge `vmbr0` on that port.
2. **Give the controller root SSH access.** The old host key no longer matches, so remove it first:
   `ssh-keygen -R <proxmox address>`, then `ssh-copy-id root@<proxmox address>`.
3. **If the network card was replaced**, update the router's DHCP reservation. The bridge carries the card's MAC.

### Running it

| Step | Command | Expect |
| --- | --- | --- |
| 1 | `make check-proxmox` | The plan shows `add` for both storages |
| 2 | `make proxmox` | Both storages added and active, start-on-boot delay set |
| 3 | `make proxmox` | `changed=0` |

The VM disks are still on the NAS, but the VM definitions lived on the old boot drive (`/etc/pve`) and are gone.
Recreate the VMs with the VM-provisioning repo and attach the existing disks. There are no Proxmox backups yet;
whether to add them is an open item in `TODO.md`.

## Known limits

- **A degraded pool is not imported.** The role imports only pools that report ONLINE, so with one failed disk
  in `nvme_gen3` it stops with "can be imported but is DEGRADED". Replacing the disk and resilvering the pool is
  not automated yet (`TODO.md`). Doing it in the web UI breaks the rule that every change goes through Ansible, so
  decide deliberately.
- **Recreating `ephemeral` erases nothing only on a blank disk.** To create the pool on a replacement disk, set
  `truenas_pool_allow_create: true` and list the new disk's serial under `truenas_pool_disks.ephemeral` in
  `local.yml`. The role refuses any disk that has partitions or belongs to a pool. Set the flag back to `false`
  afterwards.
- **Email alerts are not configured** (`TODO.md`), so a failing disk is only visible in the web UI.
- **TrueNAS's audit log keeps 7 days.** Before a planned rebuild, its Method Call entries show whether anything was
  changed outside Ansible since the last run.
