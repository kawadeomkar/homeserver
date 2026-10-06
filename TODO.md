### TODO list

Ordered by whether the Proxmox + Talos migration makes the item moot. `docs/ISSUES.md` (gitignored) holds
the full audit with file/line references; the finding ids below refer to it.

## Do before migrating — these survive it

- [ ] Delete the placeholder alerting rule that always fires at a nonexistent Alertmanager (K8S-26)
- [ ] Remove `spec.strategy` from `nextcloud/statefulset.yaml` (a Deployment-only field) so
      `kubeconform -strict` passes tree-wide and can gate CI (K8S-27)
- [ ] Add CI running `make lint` (yamllint, ansible-lint at the production profile, the module unit
      tests), `make test-idempotence` and `kubeconform -strict`. The configs exist; nothing runs them automatically (REPO-3)
- [ ] Add Renovate or equivalent — 23 image tags are now pinned by hand, and the repo's two oldest pins
      have sat unbumped since 2022 (REPO-3)
- [ ] Implement the SOPS + age decision for Kubernetes Secrets, and write down where the age key lives and
      how it is backed up. Until then nextcloud and photoprism cannot deploy (see `k8s/README-secrets.md`)
- [ ] Add a `gitleaks` pre-commit hook. Filename-based ignores cannot catch an extensionless private key
- [ ] Decide whether to rewrite git history for the credentials that were committed and later deleted.
      They were never deployed, but this repo is public
- [ ] Real domain instead of `*.homeserver.internal`, which public ACME cannot validate. Prerequisite for
      any TLS at all (K8S-14)

## TrueNAS

The NAS was reinstalled on TrueNAS Community Edition and is configured only through Ansible:
`roles/truenas` (`truenas.yml`) and, on the Proxmox side, `roles/proxmox_storage` (`proxmox.yml`).

- [ ] First real run: `make check-truenas`, then `make truenas` (or one stage at a time with
      `TAGS=truenas_network` and so on), then `make check-proxmox` and `make proxmox`. A second run of each
      must report no changes. TrueNAS allows 20 logins a minute and a full check uses about 13, so leave a
      minute between a full check and the network stage
- [ ] Reserve the NAS's static address on the router for its MAC, or move the DHCP pool off it. The address
      may sit inside the pool, where DHCP could hand it to another device
- [ ] Decide whether persistent VMs are backed up (a `backup` dataset with 1M records and a Proxmox
      backup storage) or simply recreated after a Proxmox wipe
- [ ] Measure synchronous write speed on the persistent storage (`fio` with `fsync=1`). The pool's
      drives have no power-loss protection and there is no log device; a slow result calls for a log
      device, not `sync=disabled`
- [ ] Decide whether to delete the TrueNAS CORE leftovers on the `ephemeral` pool: an orphaned snapshot of
      the media dataset and the old `.system` dataset. The role leaves both alone
- [ ] Decide whether the NAS keeps IPv6 autoconfiguration (`truenas_ipv6_auto`)
- [ ] Set up email alerts. Nothing is configured, so a degraded pool is silent. Needs SMTP credentials in
      the vault
- [ ] Decide which host, if any, mounts the `ephemeral/jellyfin` media dataset, then export it to that
      host only. Until then it stays unexported
- [ ] Before upgrading the NAS to TrueNAS 26, replace the role's hand-written WebSocket client
      (`roles/truenas/module_utils/truenas_api.py`) with iXsystems' official library,
      [`truenas/api_client`](https://github.com/truenas/api_client). Its README says TrueNAS 26 changes
      the default login method, and the library is maintained alongside each release. It is not on
      PyPI: pin it in `requirements.txt` to the git tag of the installed release (e.g. `TS-25.10.7`).
      Only the connection layer changes; the modules keep their logic. Also raise
      `truenas_supported_version` and re-check the API calls the modules make against the new release

## Proxmox

- [ ] In `claude-on-proxmox` (the repo that creates VMs): choose the storage by VM class
      (`truenas-persistent` or `truenas-ephemeral`), pass `format=qcow2` when importing the cloud image
      (`import-from` keeps the source image's format and ignores the storage default), turn on
      `destroy-unreferenced-disks` when deleting ephemeral VMs, and give its API user access to the new
      storage ids. That user, `ansible@pve`, does not exist on the reinstalled host yet

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
- [ ] Say why in a comment on the `pod-security.kubernetes.io/enforce: privileged` label of the `frigate`,
      `jellyfin` and `homeassistant` namespaces, as the convention in `CLAUDE.md` asks

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
