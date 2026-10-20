### TODO list

The running backlog: TLS, the network, CI follow-ups, the NAS and the Proxmox host, remote access, and the steps
that bring the cluster's apps up once it is built. Finding ids such as K8S-14 refer to `docs/ISSUES.md` (gitignored), the
audit of the tree before the cluster was built.

## TLS

- [ ] Trust the cluster's CA on each device (`scripts/runbooks/tls.md`): every app is HTTPS with a wildcard that
      cert-manager signs with it, Jellyfin also plain HTTP for TV apps
- [ ] Optionally, a real domain and a DNS provider with an API for cert-manager's DNS-01 challenge: publicly trusted
      certificates, nothing to install on devices, and Jellyfin's TV apps on HTTPS too (K8S-14)

## The network

- [ ] Reserve every node's address in the router's DHCP, for its MAC (`kubernetes_node_macs`): the NAS's exports
      trust the worker's address, so a device that leases it reads the cluster's shares as root
- [ ] Reserve Home Assistant's address for its MAC (`haos_mac`), with the hostname `homeassistant`, and point
      `haos_url` at that name (`scripts/runbooks/haos.md`)
- [ ] A servers VLAN for the NAS, the Proxmox host and the nodes, which the IoT and Wi-Fi devices cannot reach: NFS
      with sec=sys trusts addresses, and the nightly VM backups on the NAS hold the cluster's keys and etcd
      (CLAUDE.md, Landmines). Then narrow `kubernetes_ingress_allowed_range` to the clients' VLAN

## CI

`.github/workflows/ci.yml` gates pull requests through one check, `ci-success`. `docs/CI-PLAN.md`
(gitignored) is the full plan, and `CLAUDE.md` lists the pins Dependabot cannot see, bumped by hand.

- [ ] Check Dependabot's first pull requests: titles of the form `[dependencies] Bump …`, the `actions`,
      `ansible` and `tooling` groups, that it re-locks `poetry.lock` with a Poetry inside
      `requires-poetry` (2.5.1 in dependabot-core since 2026-09-29; production may lag), and whether the
      ruleset's undocumented `require_extra_approval_for_unattributed_changes` holds them back
- [ ] Raise `requires-python` in `pyproject.toml` (and ruff's target, and the "3.12+" in the setup docs) to 3.13:
      CI dropped its 3.12 leg, so 3.12 is allowed but untested. Likewise raise the upper bound when a 3.15 leg
      is added
- [ ] Check the first Scorecard run on `main`: that the OpenSSF API accepted the result, and which low
      scores are worth acting on. Code-Review (every change is the maintainer's own), Fuzzing,
      CII-Best-Practices and Packaging are expected to score low. Add its badge to `README.md` if wanted
- [ ] docsible 0.8.0 leaves a block's `always` tasks out of each role's `REFERENCE.md`, so proxmox_apt's
      page never shows it recording a reboot, and shows no tags for a task inside a block, so truenas's
      page leaves its eight stage tags blank. Both are reported upstream (docsible#137, docsible#138) and still
      on its main branch, which fixes multi-line comments in tables (docsible#135) and `!unsafe` tags
      (docsible#134), unreleased. With a release that has them, bump the pin, think again about `--comments`,
      check whether #137 and #138 are fixed too, and run `make fix TYPE=docs`

## TrueNAS

The NAS was reinstalled on TrueNAS Community Edition and is configured only through Ansible:
`roles/truenas` (`truenas.yml`) and, on the Proxmox side, `roles/proxmox_storage` (`proxmox.yml`).

- [ ] Recreate the home NAS's redundant pool under its class name, `persistent`. It is still called by its
      old hardware name, so until then `local.yml` must set `truenas_pool_persistent` to that name, or every
      run looks for a pool called `persistent` and stops. Do it together with the storage-id move under
      Proxmox, since both re-create the Proxmox storages:
      1. Check that the pool holds nothing worth keeping: list its datasets (read-only), and run `pvesm list`
         on the Proxmox storages that mount it.
      2. Destroy the pool in the TrueNAS UI. The role never destroys a pool.
      3. In `local.yml`, remove `truenas_pool_persistent`, key the five disk serials under `persistent`, and
         set `truenas_pool_allow_create: true`.
      4. Run `make check TYPE=truenas`, then `make deploy TYPE=truenas`.
      5. Remove `truenas_pool_allow_create`.

      This is the first real run of the role's pool creation, which no test exercises. Leave the `ephemeral`
      pool alone: it already has its class name and holds the media dataset.

      **Do it before the cluster's first live build with a NAS**, which puts the worker's data disk on the
      pool (`persistent/proxmox/vm`), while it still holds nothing worth keeping: step 2 destroys it. Once it
      holds that disk and the VM backups, never destroy it to rename it; keep `truenas_pool_persistent` in
      `local.yml` for good instead.
- [ ] Size `vm_storage_ephemeral_reservation` (`group_vars/all/storage.yml`) to what the VMs on the ephemeral
      pool need, such as the sum of the project pools' quotas, before the media apps start writing to the media
      dataset on the same pool. Until then nothing stops the media from filling the pool under the VMs.
- [ ] Automate recovery of a degraded pool. `roles/truenas` imports only ONLINE pools, so after a disk
      failure in the RAIDZ1 pool it stops, and replacing the disk and resilvering has no Ansible path yet
      (see `scripts/runbooks/REBUILD.md`)
- [ ] Move the router's DHCP dynamic range off the addresses the servers use. The NAS and the Proxmox host
      are now reserved in DHCP (OPNsense Dnsmasq host entries), but the range still overlaps server
      addresses, which is how the NAS's first-choice static address turned out to be taken
- [ ] Run the VM-restore drill (`scripts/runbooks/vm-restore.md`) once the cluster's VMs exist and the first
      nightly backup has run, and again after any change to the backup job
- [ ] Measure synchronous write speed on the persistent storage (`fio` with `fsync=1`). The pool's
      drives have no power-loss protection and there is no log device; a slow result calls for a log
      device, not `sync=disabled`
- [ ] Decide whether to delete the TrueNAS CORE leftovers on the `ephemeral` pool: an orphaned snapshot of
      the media dataset and the old `.system` dataset. The role leaves both alone
- [ ] Decide whether the NAS keeps IPv6 autoconfiguration (`truenas_ipv6_auto`)
- [ ] Give the NAS a mail server for its alerts: the `truenas_mail_*` settings and `truenas_alert_email` in
      `local.yml` (`local.yml.example` lists them), and `vault_truenas_mail_password` in the vault. Until
      then nothing is mailed, so a degraded pool shows only in the web UI
- [ ] Before upgrading the NAS to TrueNAS 26, replace the role's hand-written WebSocket client
      (`roles/truenas/module_utils/truenas_api.py`) with iXsystems' official library,
      [`truenas/api_client`](https://github.com/truenas/api_client). Its README says TrueNAS 26 changes
      the default login method, and the library is maintained alongside each release. It is not on
      PyPI, publishes no release assets (only tags such as `TS-25.10.7`), and Poetry records no hash
      for a git dependency: pin it in `pyproject.toml` as the tag's commit archive URL
      (`https://github.com/truenas/api_client/archive/<commit>.tar.gz`), check that `make lock` writes
      a hash for it, and bump it by hand (Dependabot will not). GitHub keeps generated archives
      byte-stable only with advance notice, so a hash failure there means re-check the commit and
      relock. The archive is a source distribution, which `installer.only-binary` (`poetry.toml`)
      forbids and whose build would fetch unhashed build dependencies, so building a wheel from the
      commit and keeping it outside the repo is likely the better route.
      Only the connection layer changes; the modules keep their logic. Also raise
      `truenas_supported_version` and re-check the API calls the modules make against the new release
- [ ] At that upgrade, decide whether to hold `tests/integration/fake_truenas.py` to the real API. Dump the
      `accepts`/`returns` schemas of the methods the role calls with `core.get_methods` (a read-only query,
      run once from a reviewed `main` checkout with the existing key), diff 25.10 against 26 to see which
      calls and fake handlers change, and have the fake validate requests and replies against them.
      Deferred from the CI plan (decision D6): the real NAS already accepted every 25.10 request the
      configuration sends. Note that `pool.create` is exercised by neither the fake nor a real run

## Proxmox

- [ ] `make deploy TYPE=proxmox` says when the host needs a reboot and leaves it to a person. Once the Talos VMs exist,
      decide how that reboot is done: drain the cluster first, or let Proxmox stop the VMs and start them
      again after the boot delay
- [ ] Move the home host to the owner-and-class storage ids, once, by hand: check with `pvesm list` that
      `truenas-persistent` and `truenas-ephemeral` hold no volume, `pvesm remove` both (until they are gone
      every run refuses, naming them), then `make check`, `make deploy` twice, and grant `claude-on-proxmox`'s
      token its pool
      (`pveum aclmod /storage/claude-on-proxmox-ephemeral …`, the line from that project's README). Nothing
      in `claude-on-proxmox` changes: its `local.yml` for this host sets `proxmox_storage` to the id
- [ ] Pick a quota for `claude-on-proxmox-ephemeral` (`quota` on its `vm_storage_consumers` entry, in bytes or
      with a binary suffix such as `200G`); the parent's 1 TiB applies meanwhile
- [ ] Turn on remote access through the host's Tailscale (`scripts/runbooks/tailscale.md`):
      - the tailnet's policy first, its allow-all rule deleted, the NAS limited to tcp/443 and its tests passing;
      - split DNS;
      - two-factor for `root@pam`;
      - a one-off tagged key in the vault, and `tailscale_advertise_routes` in `local.yml`;
      - `make check TYPE=proxmox` and `make deploy TYPE=proxmox` from home;
      - the checks from outside, each control alone.

      Then compare `tailscale status --json` and `tailscale get --json` on the host with `fake_tailscale.py`, which
      was written from a 1.102 client, and fix the fake where they differ
- [ ] A `make` target that prints the tailnet's policy for this setup as JSON, to copy and paste into the admin
      console's Access controls: section 1 of `scripts/runbooks/tailscale.md` rendered from `local.yml` (the LAN's
      prefix from `tailscale_advertise_routes`, the NAS's addresses, the Proxmox host's, the tag from
      `proxmox_tailscale_tags`), with or without a NAS, and its `tests` with the owner's login (asked for, or a
      `local.yml` setting). The console saves the whole policy at once, so the output says which sections to merge
      into a policy that already has others, such as another project's tags and grants (`claude-on-proxmox`'s
      `tag:claude-vm`), rather than replace it. The runbook's section 1 then points at the target instead of its
      placeholder JSON

## Bringing the apps up

Once the cluster is built and Flux syncs it (`scripts/runbooks/talos.md`). Merge app by app, or suspend the app
Kustomizations and resume one at a time, in this order: homepage; prowlarr, sonarr, radarr, sabnzbd, seerr and
filebrowser; jellyfin; nextcloud and photoprism; frigate. Each must start, survive `kubectl delete pod` and a node
reboot (`talosctl reboot -n <worker>`), keep its data, and come back from a VM backup once, by the isolated drill
(`scripts/runbooks/vm-restore.md`).

- [ ] With a NAS, before the media apps import anything: move the media dataset's content into the apps' layout and
      fix its ownership once (`README.md`, Manifests). Check first that its NFSv4 ACLs let uid 1000 write over NFS
      with root mapped to root, and that `chown-mount` can chown its mount point
- [ ] The TrueNAS CORE snapshot of 2024 on the media dataset pins every block the move replaces: decide with the
      "TrueNAS CORE leftovers" item whether to delete it, through the role
- [ ] After each app's first start, its own steps through a port forward (`scripts/runbooks/first-run.md`), then
      `kubernetes_ingress_allowed_range` to open the LAN; Frigate's cameras after
- [ ] With an Intel iGPU in the host, pass it to the worker for Jellyfin and Frigate
      (`scripts/runbooks/gpu.md`), and run that runbook's checks: the render node's group inside the pods, and
      that without a GPU both apps still schedule
- [ ] Check that LinuxServer.io's images start with `allowPrivilegeEscalation: false`, which their s6 init has not
      been seen to need. (Frigate 0.17's form under `go2rtc` is settled: it fills in `{FRIGATE_...}` only in streams
      and the RTSP user and password, and go2rtc expands `${...}` everywhere else, from its environment.)

## Removed with the Ubuntu host layer

The Ansible that configured the single Ubuntu host (`roles/main`, `roles/k8s`, the playbooks, the Vagrant
setup and the helper scripts) was deleted, and its backlog went with it: fail2ban jails, UFW rules, the
`usernames` list, `package` instead of `apt`, the defaults/vars split, and pinning `k3s_version`. The code
is in the git history.

## Renovate

- [ ] Install the Mend Renovate app on the repository once the ruleset is split (REPO-3, `docs/CI-PLAN.md` §7.3):
      `renovate.json` is ready. Until then the images and charts under `k8s/` are bumped by hand

## The cluster's secrets

- [ ] Keep copies of the cluster's age key (`~/.config/sops/age/homeserver.agekey`) and its Talos secrets bundle
      (`~/.talos/homeserver/secrets.yaml`) in the password manager. The repo holds neither, so a lost workstation
      without them means new Secrets and a new cluster (`k8s/README-secrets.md`, `README.md`)
