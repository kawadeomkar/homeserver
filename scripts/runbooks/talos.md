# Talos: building, using and upgrading the cluster

The cluster is built by `make kubernetes` (`kubernetes.yml`): the VMs on the Proxmox host (`roles/proxmox_vm`),
then Talos on them from this machine (`roles/talos`), then Flux (`roles/flux_bootstrap`). It never reboots a node
and never moves one to another version; `make talos-upgrade` does that. Addresses and MACs live only in
`group_vars/all/local.yml`.

## Before the first build

1. **Workstation tools**, from Homebrew: `talosctl` of the pinned minor release (`talos_version` in
   `group_vars/all/kubernetes.yml`; `brew install siderolabs/tap/talosctl`), `kubectl`, `flux`, `sops`, `age`,
   `helm` and `kustomize`. The role refuses a `talosctl` of another minor release, since its defaults follow its
   own.
2. **The secrets bundle**, beside the cluster's credentials (`kubernetes_talos_config_dir`'s directory, when
   `local.yml` sets one) and in no repository:

   ```bash
   mkdir -p ~/.talos/homeserver && chmod 700 ~/.talos/homeserver
   talosctl gen secrets -o ~/.talos/homeserver/secrets.yaml && chmod 600 ~/.talos/homeserver/secrets.yaml
   ```

   It is every key of the cluster (its CAs, the service-account key, etcd's encryption key): whoever holds it holds
   the cluster, and without it the cluster can no longer be managed. Keep a copy in your password manager. Never
   commit it, not even in the vault, which this public repository publishes as ciphertext no stronger than
   `.vault_pass` (every playbook refuses `vault_talos_secrets`): a bundle that was ever committed is replaced, with a
   new cluster, which costs least before the cluster holds anything.
3. **MACs and addresses.** Choose a locally administered MAC per node (lower case, the second hex digit 2, 6, a or
   e) and put it in `local.yml` as `kubernetes_node_macs`, with each node's address in
   `kubernetes_node_addresses`. In the router (OPNsense with a NAS), reserve each address for its MAC, outside the
   DHCP range, and keep MetalLB's range (`kubernetes_lb_range`) free. All before the first run: a VM leases an
   address the moment it starts.
4. **The inventory** has `[kubernetes]` with `homeserver-k8s`, as in `inventory.example`.
5. **The age key** that decrypts the cluster's SOPS secrets, a file holding this cluster's key alone, which Flux
   receives whole (`README.md`, Kubernetes secrets). age-keygen makes no directories:

   ```bash
   mkdir -p ~/.config/sops/age && chmod 700 ~/.config/sops/age
   age-keygen -o ~/.config/sops/age/homeserver.agekey && chmod 600 ~/.config/sops/age/homeserver.agekey
   ```

## The first build

```bash
make check-truenas && make truenas        # with a NAS
make check-proxmox && make proxmox
make check-kubernetes                      # on a fresh host: what it would create, install and bootstrap
make kubernetes
```

One `make kubernetes` creates and starts the VMs, waits for each to boot the Talos ISO into maintenance mode,
installs Talos on its system disk, bootstraps etcd, waits for `talosctl health`, and writes talosconfig and
kubeconfig. The run reports each VM's MAC: check them against the DHCP reservations.

## Using the cluster

```bash
export TALOSCONFIG=~/.talos/homeserver/talosconfig    # its endpoint and node: the control plane
export KUBECONFIG=~/.talos/homeserver/kubeconfig
talosctl health
kubectl get nodes
```

**Back up `~/.talos/homeserver`** with the age key, in the password manager. Besides the secrets bundle,
talosconfig and kubeconfig it holds `bootstrapped`, which is how the role tells a first build from a cluster that lost its etcd: with it and no
running etcd, a run refuses to bootstrap again, which would make a new, empty cluster. On a new workstation,
restore that directory before the first run. `etcd/` holds the etcd snapshots.

Both client certificates are valid for a year; every run renews one that expires within 30 days.

### When the workstation, or a copy of its credentials, is lost

talosconfig (`os:admin`) and kubeconfig (`system:masters`) cannot be revoked: whoever holds either keeps full
control for the rest of its year, renewed or not. The way to end that is to rotate the CAs that signed them, from a
workstation you trust, with the bundle restored from the password manager:

```bash
talosctl -n <control plane address> rotate-ca --dry-run=false --talos=true --kubernetes=false
talosctl -n <control plane address> rotate-ca --dry-run=false --talos=false --kubernetes=true
```

Neither reboots a node; the Kubernetes rotation restarts the kubelets' connections, and pods that cache the old CA
may need restarting. The first writes a new talosconfig into the current directory: move it to
`~/.talos/homeserver/talosconfig`, and fetch a new kubeconfig with `talosctl kubeconfig`. Each prints the new CA's
certificate and key: put them into the bundle's `certs.os` and `certs.k8s`, and update its copy in the password
manager, since `roles/talos` renders from the bundle and refuses credentials of another CA.

That covers credentials copied off the machine. A lost workstation holds the bundle itself, every key of the
cluster (etcd's encryption key and the service account key among them), which no rotation replaces: rebuild the
cluster with a new bundle (`talosctl gen secrets`, then the first build above), and treat the age key, `.vault_pass`, the
TrueNAS API key and the Proxmox host's SSH key as lost too: make a new age key and Secrets
(`k8s/README-secrets.md`), re-key the vault, make a new API key through the role, and remove the old SSH key from
the host.

## Configuration changes

A change to the patches (`group_vars/kubernetes/talos.yml`) is applied by `make kubernetes` without a reboot:
Talos 1.14 applies any configuration at once in that mode (its default mode, `auto`, now means the same), and
the role never reboots a node. A running service picks up its new settings as Talos reconciles it, except etcd,
which reads its settings only when it starts: after a change to the control plane the run says so, and a change
to `cluster.etcd` takes effect after `talosctl -n <address> service etcd restart` (a moment without the API on a
single control plane). Flannel, kube-proxy and CoreDNS run from manifests Talos applies only at bootstrap, so a
change to their settings (`KubeFlannelCNIConfig`, say) reaches the cluster through
`talosctl -n <control plane> upgrade-k8s --to <running Kubernetes version>`, which re-applies them (`--dry-run` shows
the diff first; the run's note gives the command). A setting read only at boot, such as a kernel argument, takes
effect at the node's next reboot, by hand. A dry run says which nodes differ, never how, since the difference can hold secrets; to see it,
run with `ANSIBLE_ARGS='-e talos_keep_rendered=true'`, which keeps each node's rendered configuration in
`~/.talos/homeserver/rendered/` (mode 0600, holding the cluster's keys, until the next run without it removes
them), then
`talosctl apply-config -n <address> -f ~/.talos/homeserver/rendered/<node>.yaml --dry-run`.

## Upgrades

1. Bump the pins together in `group_vars/all/kubernetes.yml` (`talos_version`, `talos_contract`,
   `talos_kubernetes_version`), the Makefile's `KUBERNETES_VERSION` and CI's `TALOSCTL_VERSION` and
   `TALOSCTL_SHA256`, and upgrade the workstation's `talosctl`. Move to adjacent minor releases only, the latest
   patch of each. With them, what the Image Factory must serve for the new version, since it publishes no checksum:
   `kubernetes_iso_sha256` (`sha256sum` of the new ISO, downloaded from the URL `proxmox_vm_isos` names) and a
   `talos_installer_digests` entry for each schematic at the new version (`crane digest
   factory.talos.dev/metal-installer/<schematic id>:<version>`), keeping the old version's entries until every
   node has moved, since a node is rendered with what it runs. The schematic ids stay, unless a schematic changed.
2. `make kubernetes` between the bump and the upgrade changes nothing on the nodes: it renders each node with what
   it runs, and reports the pending upgrade. It also downloads the new ISO, which nodes built later boot.
3. `make check-talos-upgrade`: the plan per node, and Kubernetes's own upgrade dry run, deprecated APIs included.
   Read it.
4. `make talos-upgrade`: an etcd snapshot, each node's upgrade, control plane first (drained, then rebooted into
   the new release), each followed by a wait for the whole cluster's health before the next node goes down, then
   `talosctl upgrade-k8s`, which also updates kube-proxy and CoreDNS. With one node of each kind, both upgrades take
   something down for a few minutes: the control plane's, the Kubernetes API (the apps keep running, but nothing
   is scheduled or changed); the worker's, every app, since draining it leaves them nowhere to go. Choose a quiet
   time.
5. `make kubernetes`: a dry run after the upgrade shows no change.

`make etcd-snapshot` takes a snapshot on its own.

## The watchdog

Every VM has Proxmox's `i6300esb` watchdog, which resets a VM whose Talos stops feeding it for two minutes
(`WatchdogTimerConfig`). On the first build, check that `watchdog0` is that device and armed:

```bash
talosctl -n <address> read /sys/class/watchdog/watchdog0/identity     # i6300ESB timer
talosctl -n <address> get watchdogtimerstatus                         # timeout 2m
```

If `watchdog0` is the q35 chipset's `iTCO_wdt` instead, point `WatchdogTimerConfig.device` at the i6300ESB's
device in `group_vars/kubernetes/talos.yml`.

## Restoring etcd

From a VM backup, preferably: `scripts/runbooks/vm-restore.md`. From a snapshot in `~/.talos/homeserver/etcd/`:
`talosctl bootstrap --recover-from=<file>` on a control plane whose etcd waits to be bootstrapped (`Preparing`),
installed and configured, as after a reinstall; on a running node, wipe etcd's partition alone first, as
`scripts/runbooks/dead-worker.md` says, which also warns that the snapshot's age is the point every Kubernetes
object goes back to. Snapshots come only from `make etcd-snapshot` and the start of every `make talos-upgrade`.

## The ISO

`proxmox_vm` keeps every node's CD-ROM on the current Talos ISO and never ejects it: a node whose system disk is
wiped reaches maintenance mode only from it. The boot order puts the system disk first, so an installed node
never boots it. Old ISOs stay in `local` until removed by hand; the Proxmox play lists the ones no VM uses.

## Syncing a branch other than main

Flux applies the synced branch with cluster-admin rights, so whoever can push to it owns the cluster. Only `main` is
protected (the "Protect main" ruleset: a pull request and CI required, no force-push, no deletion), so every playbook
refuses another branch unless `local.yml` says you know, as for a test cluster validating a branch:

```yaml
flux_sync_ref: refs/heads/<branch>
flux_sync_ref_unprotected: true
```

Keep its source suspended while you are not testing, so a push to the branch deploys nothing. The Flux Operator
rewrites the source whenever it reconciles the FluxInstance, undoing a plain `flux suspend`, so pause the operator's
handling of it first (its own annotation); `make kubernetes` leaves both as they are:

```bash
kubectl annotate fluxinstance -n flux-system flux fluxcd.controlplane.io/reconcile=disabled --overwrite
flux suspend source git flux-system -n flux-system
```

Before pushing the next change to test, the reverse:

```bash
flux resume source git flux-system -n flux-system
kubectl annotate fluxinstance -n flux-system flux fluxcd.controlplane.io/reconcile-
```

Once the branch is merged, resume as above, delete both settings and run `make kubernetes`: the cluster follows
`main` again. While the operator is paused it acts on no change to the FluxInstance, a new Flux release included.

## Validating on a test host without a NAS

On a small host, use a fresh clone of the repository, without your `.vault_pass`, whose `local.yml` holds
`proxmox_address`, a Kubernetes section with both nodes' addresses and MACs and small sizes for both (the worker's
are required without a NAS, and the control plane's defaults would take 4 GiB and a 32 GiB disk), and a directory
of its own for the cluster's credentials, so the home cluster's are never touched:

```yaml
kubernetes_node_sizes:
  homeserver-cp-1: {cores: 2, memory_mib: 2048, system_disk_gib: 10}
  homeserver-worker-1: {cores: 2, memory_mib: 6144, system_disk_gib: 32, data_disk_gib: 11, media_disk_gib: 0}
kubernetes_media_on_data_volume: true
kubernetes_talos_config_dir: ~/.talos/homeserver-test
sops_age_key_file: ~/.config/sops/age/homeserver-test.agekey
```

That is about 8 GiB of memory and 53 GiB of disk on the installer's storage, which the disk budget counts with the
host's other VMs' disks.

A run refuses a directory that holds another cluster's talosconfig or kubeconfig, so leaving the setting out stops
at the first task that would use them. Give the test cluster a secrets bundle of its own too, never the home
cluster's: `talosctl gen secrets` into that directory, as in step 2 above, and in that clone `make vault-init` (a
vault of its own; see `README.md`). It is thrown away with the test cluster.

Give it an age key of its own as well (`age-keygen -o ~/.config/sops/age/homeserver-test.agekey`, mode 0600), named
by `sops_age_key_file` above: left out, the setting means the home cluster's key, which Flux would copy into the test
cluster. The repository's Secrets are encrypted for the home key alone (`.sops.yaml` has one recipient), so the test
cluster cannot decrypt them, and a Flux Kustomization that holds one applies nothing there: `infra-tls`, the cluster's
CA, so Traefik serves its own certificate, which browsers warn about, and every group of apps with a Secret. Nothing
else waits for them, but `make kubernetes` waits for every Flux Kustomization as its last step, so there it ends in
failure every time, after 10 minutes, naming one that cannot decrypt: the cluster is built and the rest runs. That is
the price of keeping the home key out of a cluster that is thrown away. To test them, and for a run that passes,
encrypt their Secrets anew for the test key on the validation branch the test cluster syncs (`.sops.yaml` naming the
test key there too), and never merge that branch.

Never set `proxmox_vm_allow_overcommit` on a host whose thin pool other VMs share: a full pool gives them all I/O
errors. Once Flux is up, Homepage is the one app that starts there, since every other group holds a Secret
(FileBrowser's, in media). To test a group, encrypt its Secrets for the test key as above, and leave the heavy ones
out (`flux suspend kustomization apps-nextcloud apps-photoprism apps-frigate -n flux-system`), or point
`flux_sync_ref` at a branch that lists fewer apps (`refs/heads/<branch>`, a full reference, with
`flux_sync_ref_unprotected`; see below). Afterwards: `qm stop 300 301`, then `qm destroy 300 --purge` and
`qm destroy 301 --purge`, delete their backups and the ISO, the clone, and its `kubernetes_talos_config_dir`.
