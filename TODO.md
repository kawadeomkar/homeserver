### TODO list

Ordered by whether the Proxmox + Talos migration makes the item moot. `docs/ISSUES.md` (gitignored) holds
the full audit with file/line references; the finding ids below refer to it.

## Do before migrating — these survive it

- [ ] Delete the placeholder alerting rule that always fires at a nonexistent Alertmanager (K8S-26)
- [ ] Remove `spec.strategy` from `nextcloud/statefulset.yaml` (a Deployment-only field) so
      `kubeconform -strict` passes tree-wide and can gate CI (K8S-27)
- [ ] Add CI: `kubeconform -strict`, `yamllint` with a committed `.yamllint`, and `ansible-lint` once
      there are roles to lint. Nothing is mechanically checked today (REPO-3)
- [ ] Add Renovate or equivalent — 23 image tags are now pinned by hand, and the repo's two oldest pins
      have sat unbumped since 2022 (REPO-3)
- [ ] Implement the SOPS + age decision for Kubernetes Secrets, and write down where the age key lives and
      how it is backed up. Until then nextcloud and photoprism cannot deploy (see `k8s/README-secrets.md`)
- [ ] Add a `gitleaks` pre-commit hook. Filename-based ignores cannot catch an extensionless private key
- [ ] Decide whether to rewrite git history for the credentials that were committed and later deleted.
      They were never deployed, but this repo is public
- [ ] Repair the local toolchain: `ansible-playbook`, `ansible-lint` and `yamllint` all fail on a dead
      pyenv interpreter, so no linter runs (TOOL-1). Needed before any new role can be run from here
- [ ] Real domain instead of `*.homeserver.internal`, which public ACME cannot validate. Prerequisite for
      any TLS at all (K8S-14)

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

- [ ] NAS address and export layout — two different addresses and two export roots appear across the
      manifests; one root is on a pool that no longer exists, and the reinstalled NAS exports nothing yet
      (K8S-9)
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
- [ ] Whether to adopt `kube-prometheus-stack`, which would replace most of `k8s/prometheus`,
      `k8s/grafana`, `k8s/node-exporter` and `k8s/kube-state-metrics` outright
- [ ] dashdot's placement — inside a VM it reports the VM's virtual disk, not real hardware (K8S-10)
