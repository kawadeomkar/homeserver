### TODO list

Ordered by whether the Proxmox + Talos migration makes the item moot. `docs/ISSUES.md` (gitignored) holds
the full audit with file/line references; the finding ids below refer to it.

## Do before migrating — these survive it

- [ ] Delete the placeholder alerting rule that always fires at a nonexistent Alertmanager (K8S-26)
- [ ] Add Renovate for the 21 container image tags, which are pinned and bumped by hand. Wait until the
      cluster exists, and split the ruleset first so an app with write access cannot merge on its own
      (REPO-3). Dependabot covers the SHA-pinned actions and the Python lock, not image tags
- [ ] Write nextcloud's and photoprism's Secrets as `*.sops.yaml` files (`k8s/README-secrets.md`, which also says
      where the age key lives and how it is backed up). Until then neither can deploy
- [ ] Real domain instead of `*.homeserver.internal`, which public ACME cannot validate. Prerequisite for
      any TLS at all (K8S-14)

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
      page leaves its six stage tags blank. Both are reported upstream (docsible#137, docsible#138) and still
      on its main branch, which fixes multi-line comments in tables (docsible#135) and `!unsafe` tags
      (docsible#134), unreleased. With a release that has them, bump the pin, think again about `--comments`,
      check whether #137 and #138 are fixed too, and run `make docs`

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
      4. Run `make check-truenas`, then `make truenas`.
      5. Remove `truenas_pool_allow_create`.

      This is the first real run of the role's pool creation, which no test exercises. Leave the `ephemeral`
      pool alone: it already has its class name and holds the media dataset.

      **Do it before the first live run that creates the cluster's datasets on the pool** (Nextcloud's and
      PhotoPrism's), while it still holds nothing worth keeping: step 2 destroys it. Once it holds their files
      and the VM backups, never destroy it to rename it; keep `truenas_pool_persistent` in `local.yml` for good
      instead.
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
- [ ] Decide which host, if any, mounts the `ephemeral/jellyfin` media dataset, then export it to that
      host only. Until then it stays unexported
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

- [ ] `make proxmox` says when the host needs a reboot and leaves it to a person. Once the Talos VMs exist,
      decide how that reboot is done: drain the cluster first, or let Proxmox stop the VMs and start them
      again after the boot delay
- [ ] Move the home host to the owner-and-class storage ids, once, by hand: check with `pvesm list` that
      `truenas-persistent` and `truenas-ephemeral` hold no volume, `pvesm remove` both (until they are gone
      every run refuses, naming them), then `make check`, `make site` twice, and grant `claude-on-proxmox`'s
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
      - `make check-proxmox` and `make proxmox` from home;
      - the checks from outside, each control alone.

      Then compare `tailscale status --json` and `tailscale get --json` on the host with `fake_tailscale.py`, which
      was written from a 1.102 client, and fix the fake where they differ

## Manifest defects found in review

From a review of the change stack in October 2026. Read from the manifests and the upstream images; none
was reproduced on a cluster, because there is none.

- [ ] `grafana` runs as uid 472 and relies on `fsGroup` to make its hostPath PV writable, which Kubernetes
      does not apply to hostPath volumes, so it cannot write `/var/lib/grafana`. `prometheus` depends the
      same way on `/opt/prometheus` being owned by uid 1000. Both go away with the hostPath→PVC conversion
- [ ] `filebrowser`: `FileOrCreate` makes `filebrowser.db` a root-owned 0644 file, but the pinned image
      runs as uid 1000 and needs to write it
- [ ] `dashy` is pinned to 4.4.12, which listens on 8080 and reads `/app/user-data/conf.yml`; the manifest
      still uses containerPort and targetPort 80 and mounts `/app/public/conf.yml`
- [ ] `homepage` is pinned to v1.13.2, which rejects requests unless `HOMEPAGE_ALLOWED_HOSTS` names the
      host it is reached by; the Deployment sets no env at all
- [ ] `prometheus` Deployment has no `strategy: Recreate` although it now holds a hostPath PV, so a rollout
      starts a second pod against the same TSDB and hangs

## Removed with the Ubuntu host layer

The Ansible that configured the single Ubuntu host (`roles/main`, `roles/k8s`, the playbooks, the Vagrant
setup and the helper scripts) was deleted, and its backlog went with it: fail2ban jails, UFW rules, the
`usernames` list, `package` instead of `apt`, the defaults/vars split, and pinning `k3s_version`. The code
is in the git history.

## Migration decisions still open

Blocked on the per-server hardware inventory.

- [ ] NAS address and export layout for the cluster — two different addresses and two export roots appear
      across the manifests, and one root is on a pool that no longer exists. The NAS now has one address
      (`truenas_static_address`) but exports only the two Proxmox VM datasets (K8S-9)
- [ ] LoadBalancer implementation and VIP range. k3s's ServiceLB does not exist on Talos, so jellyfin's
      DLNA/discovery Services need MetalLB or Cilium LB IPAM there (K8S-5)
- [ ] Ingress controller. Talos bundles none, so every class-less Ingress in this repo routes nowhere
      until one is installed (K8S-6)
- [ ] CSI driver / StorageClass. Talos ships neither, and the `/opt` hostPath model does not survive an
      immutable root — this is also what the outstanding PSA violations really need (K8S-4)
- [ ] GPU passthrough: jellyfin and frigate both want `/dev/dri`, and one Intel iGPU cannot easily be
      shared between two VMs
- [ ] Home Assistant's placement — it needs device access and host networking, and is the workload least
      suited to Talos
- [ ] Control-plane count, and where etcd snapshots go
- [ ] Replace `k8s/prometheus`, `k8s/grafana`, `k8s/node-exporter` and `k8s/kube-state-metrics` with
      `kube-prometheus-stack` once the cluster is built. Decided in October 2026; until then the
      hand-written tree is left as it is, including its two open defects listed above
- [ ] dashdot's placement — inside a VM it reports the VM's virtual disk, not real hardware (K8S-10)

## The cluster's secrets

- [ ] Keep a copy of the cluster's Talos secrets bundle (`~/.talos/homeserver/secrets.yaml`) in the password
      manager. The repo never holds it, so a lost workstation without it means a new cluster (`README.md`)
