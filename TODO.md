### TODO list

Ordered by whether the Proxmox + Talos migration makes the item moot. `docs/ISSUES.md` (gitignored) holds
the full audit with file/line references; the finding ids below refer to it.

## Do before migrating — these survive it

- [ ] Correct the two Ingress backends that target ports their Service does not serve: filebrowser
      8383→8080, node-exporter 7878→9100 (K8S-24)
- [ ] Drop `portainer`, or replace its `cluster-admin` binding with a scoped Role and put it behind auth.
      While it stands, every Pod Security Admission label in the tree is advisory (SEC-2)
- [ ] Stop `filebrowser` mounting host `/` writable behind an unauthenticated Ingress (SEC-3)
- [ ] Give Prometheus a ServiceAccount that actually exists — its ClusterRoleBinding names a namespace no
      manifest creates, so all Kubernetes service discovery 403s and it collects nothing (K8S-20)
- [ ] Point Grafana's datasource at in-cluster DNS instead of a hardcoded node IP + NodePort (K8S-21)
- [ ] Prometheus TSDB is an `emptyDir` with no retention while Grafana's dashboards get a 10Gi PV — invert
      that, or at least set a `sizeLimit` so it cannot evict its neighbours (K8S-23)
- [ ] Delete the placeholder alerting rule that always fires at a nonexistent Alertmanager (K8S-26)
- [ ] Fix `kube-state-metrics`: `cluster-role-binding.yaml` is a byte-identical copy of `deployment.yaml`,
      so no binding exists and the workload cannot function (K8S-19)
- [ ] Remove `spec.strategy` from `nextcloud/statefulset.yaml` (a Deployment-only field) so
      `kubeconform -strict` passes tree-wide and can gate CI (K8S-27)
- [ ] Add CI: `kubeconform -strict`, `yamllint` with a committed `.yamllint`, `ansible-lint`. Nothing is
      mechanically checked today (REPO-3)
- [ ] Add Renovate or equivalent — 23 image tags are now pinned by hand, and the repo's two oldest pins
      have sat unbumped since 2022 (REPO-3)
- [ ] Implement the SOPS + age decision for Kubernetes Secrets, and write down where the age key lives and
      how it is backed up. Until then nextcloud and photoprism cannot deploy (see `k8s/README-secrets.md`)
- [ ] Add a `gitleaks` pre-commit hook. Filename-based ignores cannot catch an extensionless private key
- [ ] Decide whether to rewrite git history for the credentials that were committed and later deleted.
      They were never deployed, but this repo is public
- [ ] Fix the broken helper scripts or delete them (`run_tests.sh` is what the README used to prescribe
      for linting) — TOOL-2
- [ ] Repair the local toolchain: `ansible-playbook`, `ansible-lint` and `yamllint` all fail on a dead
      pyenv interpreter, so no linter runs (TOOL-1)
- [ ] `k3s_version: ""` is the repo's only floating dependency — pin it, or drop it with the migration
- [ ] Real domain instead of `*.homeserver.internal`, which public ACME cannot validate. Prerequisite for
      any TLS at all (K8S-14)

## Superseded by the Proxmox + Talos migration

Talos has no SSH, no package manager, no user accounts, and an immutable root, so the layer these items
configure disappears. Left here so the intent is not lost if the migration stalls.

- ~~fail2ban: template `jail.local` / `fail2ban.local`, move vars to defaults~~ — no SSH to protect
- ~~UFW: configure allow/deny per container port~~ — Talos has its own firewall; Proxmox has its own
- ~~change `username` default var to a `usernames` list~~ — no user accounts
- ~~`package` module instead of `apt`~~ — no package manager on the nodes
- ~~microk8s custom certificates~~ — two migrations out of date; cert-manager was deleted as unsalvageable
- ~~split defaults and vars~~ — `group_vars` gets rewritten wholesale; a third of it is already inert

## Migration decisions still open

Blocked on the per-server hardware inventory.

- [ ] NAS address and export layout — three different addresses and three export roots appear across
      manifests and `group_vars` (K8S-9)
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
