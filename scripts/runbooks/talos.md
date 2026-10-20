# Talos: building and using the cluster

The cluster is built by `make kubernetes` (`kubernetes.yml`): the VMs on the Proxmox host (`roles/proxmox_vm`),
then Talos on them from this machine (`roles/talos`). It never reboots a node and never moves one to another
version. Addresses and MACs live only in `group_vars/all/local.yml`.

## Before the first build

1. **Workstation tools**, from Homebrew: `talosctl` of the pinned minor release (`talos_version` in
   `group_vars/all/kubernetes.yml`; `brew install siderolabs/tap/talosctl`) and `kubectl`. The role refuses a
   `talosctl` of another minor release, since its defaults follow its own.
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
   DHCP range. All before the first run: a VM leases an address the moment it starts.
4. **The inventory** has `[kubernetes]` with `homeserver-k8s`, as in `inventory.example`.
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

**Back up `~/.talos/homeserver`** in the password manager. Besides the secrets bundle,
talosconfig and kubeconfig it holds `bootstrapped`, which is how the role tells a first build from a cluster that lost its etcd: with it and no
running etcd, a run refuses to bootstrap again, which would make a new, empty cluster. On a new workstation,
restore that directory before the first run.

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
cluster with a new bundle (`talosctl gen secrets`, then the first build above), and treat `.vault_pass`, the
TrueNAS API key and the Proxmox host's SSH key as lost too: re-key the vault, make a new API key through the role,
and remove the old SSH key from the host.

## Configuration changes

A change to the patches (`group_vars/kubernetes/talos.yml`) is applied by `make kubernetes` without a reboot:
Talos 1.14 applies any configuration at once in that mode (its default mode, `auto`, now means the same), and
the role never reboots a node. A running service picks up its new settings as Talos reconciles it, except etcd,
which reads its settings only when it starts: after a change to the control plane the run says so, and a change
to `cluster.etcd` takes effect after `talosctl -n <address> service etcd restart` (a moment without the API on a
single control plane). A setting read only at boot, such as a kernel argument, takes
effect at the node's next reboot, by hand. A dry run says which nodes differ, never how, since the difference can hold secrets; to see it,
run with `ANSIBLE_ARGS='-e talos_keep_rendered=true'`, which keeps each node's rendered configuration in
`~/.talos/homeserver/rendered/` (mode 0600, holding the cluster's keys, until the next run without it removes
them), then
`talosctl apply-config -n <address> -f ~/.talos/homeserver/rendered/<node>.yaml --dry-run`.

## The watchdog

Every VM has Proxmox's `i6300esb` watchdog, which resets a VM whose Talos stops feeding it for two minutes
(`WatchdogTimerConfig`). On the first build, check that `watchdog0` is that device and armed:

```bash
talosctl -n <address> read /sys/class/watchdog/watchdog0/identity     # i6300ESB timer
talosctl -n <address> get watchdogtimerstatus                         # timeout 2m
```

If `watchdog0` is the q35 chipset's `iTCO_wdt` instead, point `WatchdogTimerConfig.device` at the i6300ESB's
device in `group_vars/kubernetes/talos.yml`.

## The ISO

`proxmox_vm` keeps every node's CD-ROM on the current Talos ISO and never ejects it: a node whose system disk is
wiped reaches maintenance mode only from it. The boot order puts the system disk first, so an installed node
never boots it. Old ISOs stay in `local` until removed by hand; the Proxmox play lists the ones no VM uses.

## Validating on a test host without a NAS

On a small host, use a fresh clone of the repository, without your `.vault_pass`, whose `local.yml` holds
`proxmox_address`, a Kubernetes section with both nodes' addresses and MACs and small sizes for both (the worker's
are required without a NAS, and the control plane's defaults would take 4 GiB and a 32 GiB disk), and a directory
of its own for the cluster's credentials, so the home cluster's are never touched:

```yaml
kubernetes_node_sizes:
  homeserver-cp-1: {cores: 2, memory_mib: 2048, system_disk_gib: 10}
  homeserver-worker-1: {cores: 2, memory_mib: 6144, system_disk_gib: 10, data_disk_gib: 10, media_disk_gib: 0}
kubernetes_media_on_data_volume: true
kubernetes_talos_config_dir: ~/.talos/homeserver-test
```

That is about 8 GiB of memory and 30 GiB of disk on the installer's storage, which the disk budget counts with the
host's other VMs' disks.

A run refuses a directory that holds another cluster's talosconfig or kubeconfig, so leaving the setting out stops
at the first task that would use them. Give the test cluster a secrets bundle of its own too, never the home
cluster's: `talosctl gen secrets` into that directory, as in step 2 above, and in that clone `make vault-init` (a
vault of its own; see `README.md`). It is thrown away with the test cluster.

Never set `proxmox_vm_allow_overcommit` on a host whose thin pool other VMs share: a full pool gives them all I/O
errors. Afterwards: `qm stop 300 301`, then `qm destroy 300 --purge` and `qm destroy 301 --purge`, delete their
backups and the ISO, the clone, and its `kubernetes_talos_config_dir`.
