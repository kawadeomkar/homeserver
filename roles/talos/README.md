# talos

Builds a Talos Linux cluster from the controller with `talosctl` and `kubectl`, and keeps its configuration. With
only its defaults it does nothing. It runs on `homeserver-k8s` (`ansible_connection: local`: the cluster is built
from the workstation, and the host's variables are `group_vars/all/`'s and `group_vars/kubernetes/`'s), with
`talosctl` and `kubectl` from `PATH`. Three entry points:

- **`main`** (`just deploy kubernetes`; `kubernetes.yml`'s second play, after the first made the VMs with
  `roles/proxmox_vm` on the Proxmox host, and before `roles/flux_bootstrap`, which reads the kubeconfig it writes):
  installs Talos on nodes in maintenance mode, keeps a configured node's configuration, bootstraps etcd once, waits
  for the cluster to be healthy, and writes talosconfig and kubeconfig. It never reboots a node and never moves one
  to another version (`just check kubernetes` never upgrades); it reports what the pins wait for.
- **`upgrade`** (`just talos-upgrade`, `talos-upgrade.yml`; `tasks_from: upgrade`): the only way a running node
  moves to new pins. An etcd snapshot, then each node whose Talos version or schematic differs from the pins, the
  control plane first, one at a time (`upgrade-node.yml`: `talosctl upgrade`, which drains the node first, no
  PodDisruptionBudget blocking it here, and waits for it to come back, then a wait for the cluster's health, so a
  node that comes back broken stops the run with the rest of the cluster still up), then Kubernetes with `talosctl
  upgrade-k8s`, which pre-pulls the images and updates each node's configuration and the bootstrap manifests
  (kube-proxy's, CoreDNS's) that a configuration change alone would not, then the cluster's health. The snapshot is
  taken only when something moves. It refuses a cluster that is not all up: every node configured and the
  cluster's Kubernetes version readable, or `just deploy kubernetes` first. In check mode (`just talos-upgrade
  --check`) it shows the plan, a line per node in the order they go and one for Kubernetes, and Kubernetes's own
  dry run (`upgrade-k8s --dry-run`, deprecated APIs included), without pulling images (`--pre-pull-images=false`:
  talosctl pulls the new images onto every node first, dry run or not, unless told otherwise); read it first. It
  names each installer by its digest alone (`<name>@sha256:…`, the `talos_image_by_digest` filter): talosctl pulls
  the image onto the node first, where containerd keeps a `<name>:<tag>@<digest>` as `<name>@<digest>`, then asks
  for it by the reference it was given, which a Talos 1.14 node never finds ("not found in containerd store", seen
  on the home cluster on 2026-10-28, Talos 1.14.2 nodes and talosctl 1.14.4). The digest is the pin either way.
  Neither of Talos 1.14's upgrade paths writes the image into the node's configuration, so the upgrade leaves it
  naming its old installer, which the next `main` run replaces, without a reboot. Bump to adjacent minor releases
  only, to the latest patch of each (`talos-upgrade.yml`, `scripts/runbooks/talos.md`).
- **`etcd-snapshot`** (`just etcd-snapshot`, `etcd-snapshot.yml`; `tasks_from: etcd-snapshot`): a snapshot of the
  control plane's etcd into `talos_config_dir/etcd/` (`~/.talos/homeserver/etcd/`, or the `etcd/` under `local.yml`'s
  `kubernetes_talos_config_dir`), named `etcd-<UTC time, %Y%m%dT%H%M%SZ>.db`, which `just status` dates the snapshots
  by, and which `talosctl bootstrap --recover-from=<snapshot>` takes back. `upgrade` takes the same snapshot
  (`snapshot.yml`). It asks only the control plane (`talos_prepare_minimal`), never the Image Factory or the other
  nodes, so it works on a degraded cluster, when a snapshot matters most. The run says where the snapshot went, under
  `~` when it is in the home directory, and how big it is.

Each of the three playbooks first asserts that `nas_section_error` and `kubernetes_settings_error` are empty, and
ends the host, saying why, when `local.yml` names no `kubernetes_node_addresses`. `talos-upgrade.yml` and
`etcd-snapshot.yml` begin with a play on localhost that refuses an inventory without the `kubernetes` group (one made
before the cluster has none), whose play would otherwise match no host and end without a word; `kubernetes.yml`
refuses it in its first play, before the VMs are made.

Every entry point first checks the role's inputs (`validate.yml`, which needs no connection and no talosctl, runs
under every tag and gives each problem a sentence naming the variable: exactly one control plane, every node named,
addressed and given a known schematic, the versions, `talos_contract` being `talos_version`'s major and minor, and
`talos_config_dir` and `talos_secrets_file` outside the repository). Then, in `prepare.yml`:

- **Checks** the workstation's `talosctl` (`talosctl version --client --short`, read locally): its minor release
  must be `talos_contract`, since talosctl's defaults (the installer image, the Kubernetes version) follow its own
  release, so a talosctl of another minor would render other configurations.
- **Asks the Image Factory** (`POST /schematics`) for each schematic's id. The id is the hash of the schematic's
  content, so asking again changes nothing and runs in check mode too; the factory answers 201 (or 200). It refuses
  an id other than `talos_schematic_pins`' (a changed schematic needs its new id pinned; otherwise the factory
  answered something else), and gives every installer image the digest `talos_installer_digests` pins for its
  schematic and version, refusing a schematic in use at `talos_version` without one: a tag on the factory's
  registry can be moved, a digest cannot. Not in `etcd-snapshot`.
- **Reads the secrets bundle** (below), and **refuses another cluster's credentials** in `talos_config_dir`: a
  talosconfig whose endpoint is not the control plane or whose CA is not the secrets bundle's, a kubeconfig whose
  server is not `talos_endpoint` or whose CA is not the bundle's. Kept, they would answer for that cluster (its
  Kubernetes version read, Flux handed to it); replaced, that cluster would lose its credentials. So a test host's
  run, whose `local.yml` forgot `kubernetes_talos_config_dir`, never reads, replaces or hands Flux the home
  cluster's. The refusal says how to recover when they are this cluster's and its control plane's address or its
  bundle changed: move talosconfig and kubeconfig out, keeping `bootstrapped` and `etcd/`.
- **Makes a scratch directory** (below), with a talosconfig for the run alone, made from the secrets bundle with
  the control plane as endpoint and node, so the probes work on a new workstation and in check mode alike.
- **Probes** each node (`probe.yml`; not in `etcd-snapshot`): configured (it answers `talosctl version` with the
  run's talosconfig), in maintenance mode (it answers only `--insecure`, as a node booted from the ISO does), or not
  answering. A real run waits for a node that does not answer yet (`talos_probe_retries`, `talos_probe_delay`: its
  VM may have started seconds before, in the previous play), asks the ones still silent once more as configured
  nodes (one may have rebooted into its configuration meanwhile), and then fails naming the address and asking
  whether its VM runs and the router's DHCP reserves that address for its MAC (`kubernetes_node_macs`). A dry run
  asks once, since on a fresh host the VMs do not exist, and reports "would install … once its VM is up". Of each
  configured node it reads the Talos tag it runs from the `NODE:` block of `talosctl version`, never the output's
  first `Tag:`, which is the client's own; the schematic it booted (`get extensions`, the extension named
  `schematic`); and, with a kubeconfig, the cluster's Kubernetes version (`kubectl version`).

What `main` does then, in order:

- **Renders** each node's configuration with `talosctl gen config` (`render.yml`), from the secrets bundle and the
  patches: those for every node, then for its role, then its own, each passed inline (`--config-patch` takes the
  YAML itself), so none is written to disk. A node in maintenance mode gets the pins; a configured node gets what it
  runs (its Talos tag, the contract of that tag, the schematic it booted with its installer at that tag and its
  pinned digest, the cluster's Kubernetes version), so a pin bump changes nothing until `upgrade` moves it. Without
  the Kubernetes version (no kubeconfig on this workstation yet) a configured node is not rendered, and so not
  checked for drift, that run, which says so: the run fetches a kubeconfig, and the next one checks it. It first
  renders a configuration with talosctl's defaults and keeps the pod network talosctl gives a cluster, which the
  patches can use as `talos_pod_subnets` (a firewall rule for the pod network, say) without writing the range out;
  a talosctl that writes no `KubeNetworkConfig` with `podSubnets` renders a form the role does not know, and is
  refused.
- **Installs** a node in maintenance mode with `apply-config --insecure`, then waits on its install status (`get
  unattendedinstallstatuses` with the talosconfig, `talos_install_retries` times, `talos_probe_delay` apart) until it
  reports `installed`. On Talos 1.14 the node answers with the talosconfig at once and installs in the background,
  still running from the ISO: `pending` until a disk matches the install selector, `installing`,
  `waiting-for-reboot`, and once rebooted from its system disk, `installed`; while it reboots it answers nothing. The
  installer image that `gen config` writes into its `UnattendedInstallConfig` (from `--install-image`) is what makes
  Talos reboot, and within one boot it never reports `waiting-for-reboot` as `installed`, so `installed` means it
  runs from its system disk. A disk selector that matches nothing shows up as a lasting `pending`, with Talos's
  error: Talos 1.14.2 leaves that zero phase out of the YAML and gives the error "no disk matched the selector",
  which the `talos_install_status` filter reads (found in the first live run on the home machines, 2026-10-17). A
  lasting `pending` or a `failed` install fails the run with Talos's error, and points at `talosctl get disks`,
  before bootstrap, which Talos refuses until then, so the failure is never a bootstrap timeout. Once the cluster
  was bootstrapped, a node in maintenance mode is installed only when `talos_reinstall` names it (or
  `talos_allow_bootstrap` starts a new cluster), in a dry run too: a node in maintenance mode takes its
  configuration, which holds the cluster's keys, from whoever sends it, over TLS that verifies nothing, and
  `--insecure` proves nothing about what answers at the address (a device spoofing it, or the node's address taken
  over while it reboots). Before the bootstrap that is the only way in. The refusal says to check on the VM's
  console in Proxmox that it waits in maintenance mode at that address, then name it (`-e
  talos_reinstall=<node>`), and otherwise to find what else answers there.
- **Corrects drift** on a configured node: `apply-config --dry-run` finds it (`No changes.` when there is none).
  The diff is never printed, since it can hold secrets; the run names the nodes that differ and says how to see it
  by hand: `-e talos_keep_rendered=true`, then `talosctl apply-config --dry-run` on the kept file. `--mode
  no-reboot` applies it, and the node is **never rebooted**: Talos 1.14 applies any configuration at once in that
  mode (`auto`, its default, means the same now; ApplyConfiguration never refuses for a reboot), so nothing is ever
  staged. The apply's output stays `no_log`, since it could hold the configuration, and one that fails stops the
  run with Talos's error lines. After a change applied to the control plane the run says to restart etcd
  (`talosctl service etcd restart`), which reads its settings only when it starts, and to sync the Flannel,
  kube-proxy and CoreDNS manifests (`talosctl upgrade-k8s --to <the running version>`), which Talos applies only at
  bootstrap (after a change to `KubeFlannelCNIConfig`, say).
- **Bootstraps etcd once** (`bootstrap.yml`), recording it in `talos_config_dir/bootstrapped`. With that record and
  etcd not `Running` (`talosctl service etcd`, waited for, since etcd takes a moment after a boot; a dry run asks
  once, when the control plane answers), it refuses, in a dry run too, and names the ways back: `talosctl bootstrap
  --recover-from=<snapshot>` with a snapshot from `talos_config_dir/etcd/`, the VM's backup
  (`scripts/runbooks/vm-restore.md`), or `-e talos_allow_bootstrap=true` for a new cluster on purpose. A second
  bootstrap on a control plane that lost its etcd (a reinstalled system disk, a VM made anew after a host loss)
  makes a new, empty cluster, over which Flux would recreate every volume empty while the old data sat orphaned.
  Back `talos_config_dir` up: on a new workstation without it the role cannot tell a first build from a lost
  cluster. The first call after an install may find the node not ready (`FailedPrecondition`) and is retried;
  `AlreadyExists`, from a run that bootstrapped and failed before writing the record, is fine.
- **Checks** the cluster's health (`talosctl health`, `talos_health_timeout`), and that every user volume the
  patches declare is mounted (`ready`, from `get volumestatus`): a user volume whose disk selector matches nothing,
  or whose disk is smaller than its minimum, is never mounted, and neither Talos nor its health check says so, while
  local-path would then make the apps' directories on the system disk, which a reinstall wipes.
- **Writes** talosconfig (the control plane as endpoint and node) and kubeconfig (`talosctl kubeconfig`) to
  `talos_config_dir`, a 0700 directory, each mode 0600, and renews them `talos_renew_before_days` before their client
  certificates expire (`openssl x509 -checkend`; talosconfig's is valid for a year); a dry run says it would.
- **Reports** the Talos release and schematic each node runs, the cluster's Kubernetes version, and what the pins
  wait for, and records what is left with `set_stats` under `homeserver_next`, which the stdout callback prints as
  NEXT STEPS: `talos.pins` (the pins waiting, with `talos_upgrade_command`), and after a change to the control plane
  `talos.manifests` and `etcd.restart`, each with its command.

The secrets bundle comes from `talos_secrets_file` (`talosctl gen secrets`), which must exist, be readable by its
owner alone (mode 0600 or 0400) and lie outside the repository: it holds every key of the cluster (its CAs, the
service account key, etcd's encryption key), so it is never committed, even encrypted, and never kept in the vault,
where `kubernetes_settings_error` refuses `vault_talos_secrets`. Every entry point refuses it missing, open to others
or inside the repository, reads it with `slurp` under `no_log`, checks it by its text alone (it must hold the
`certs:` line `gen secrets` writes; a YAML parser's error could quote a line of it) and only ever writes it with
`no_log`. Each run works in a scratch directory: a copy of the bundle, the run's talosconfig, and the rendered files.
It is made with `check_mode: false` and `changed_when: false`, so a dry run renders and a second run stays at
`changed=0`, and is removed in each entry point's block's `always`, unless `talos_keep_rendered` keeps a copy of the
rendered files in `talos_config_dir/rendered/` (mode 0600) to see a diff by hand, which the next run without it
removes, since they hold every key of the cluster. A run that is interrupted (Ctrl-C, a lost terminal) never reaches
that removal and leaves a copy of the bundle behind, so each run removes the scratch directories (`ansible.*.talos`
in the temporary directory) a day old or older.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `talos_cluster_name` | `""` | The cluster's name |
| `talos_endpoint` | `""` | The Kubernetes API, `https://<control plane address>:6443` |
| `talos_extra_sans` | `[]` | More names for the API server's certificate |
| `talos_version` | `""` | The Talos release a new node is installed with, such as `v1.14.2` |
| `talos_contract` | `""` | The configuration contract talosctl renders for, `talos_version`'s minor |
| `talos_kubernetes_version` | `""` | The Kubernetes version a new cluster runs |
| `talos_secrets_file` | `""` | The file holding the secrets bundle (`talosctl gen secrets`), mode 0600 or 0400, outside the repository; never printed |
| `talos_nodes` | `[]` | Each node: `name`, `role` (`controlplane` or `worker`), `address`, `schematic`, `patches` |
| `talos_schematics` | `{}` | Image Factory schematics by name, each a list of official extensions |
| `talos_image_factory_url` | `https://factory.talos.dev` | The Image Factory |
| `talos_patches_common` | `[]` | Patches for every node, as YAML documents in strings |
| `talos_patches_controlplane` | `[]` | Patches for control planes |
| `talos_patches_worker` | `[]` | Patches for workers |
| `talos_config_dir` | `""` | Where talosconfig, kubeconfig, the bootstrap record and etcd snapshots live; outside the repository, one per cluster |
| `talos_renew_before_days` | `30` | Renew client certificates that expire within this many days |
| `talos_health_timeout` | `10m` | How long `talosctl health` waits |
| `talos_probe_retries` | `30` | How many times a real run asks a node that does not answer yet |
| `talos_probe_delay` | `10` | Seconds between those questions, and between install status reads |
| `talos_install_retries` | `60` | How many times it reads an installing node's status before giving up |
| `talos_allow_bootstrap` | `false` | Bootstrap etcd although this cluster was bootstrapped before: a new, empty cluster |
| `talos_reinstall` | `[]` | The nodes, by name, that may be installed although the cluster was bootstrapped (a list, or names joined by commas) |
| `talos_schematic_pins` | `{}` | Each schematic's id, by name: the Image Factory's answer is refused otherwise |
| `talos_installer_digests` | `{}` | Each installer image's digest, by `<schematic id>:<version>`, given beside the tag; once set, each schematic in use at `talos_version` needs one |
| `talos_keep_rendered` | `false` | Keep each node's rendered configuration in `talos_config_dir/rendered/` (mode 0600), until the next run without it |
| `talos_upgrade_command` | `""` | What moves the nodes to the pins, recorded under `homeserver_next` as left to do beside what the pins wait for, which the stdout callback prints as NEXT STEPS; empty, what they wait for alone |

A variable for one run follows the playbook:
`just deploy kubernetes -e talos_allow_bootstrap=true`. `scripts/runbooks/talos.md` covers building,
upgrading and restoring; this repository's configuration is `group_vars/kubernetes/talos.yml` (below).

## Example

```yaml
talos_cluster_name: homelab
talos_endpoint: https://192.0.2.30:6443
talos_version: v1.14.2
talos_contract: v1.14
talos_kubernetes_version: 1.37.1
talos_secrets_file: "{{ lookup('env', 'HOME') }}/.talos/homelab/secrets.yaml"
talos_config_dir: "{{ lookup('env', 'HOME') }}/.talos/homelab"
talos_schematics: {base: [siderolabs/qemu-guest-agent]}
talos_nodes:
  - {name: cp-1, role: controlplane, address: 192.0.2.30, schematic: base, patches: []}
```

## This repository's cluster

`group_vars/kubernetes/talos.yml` holds the cluster's configuration: its name (`homeserver`), the nodes, the
schematics and the patches. `talos_config_dir` is `~/.talos/homeserver`, or `local.yml`'s
`kubernetes_talos_config_dir` for a second cluster, such as a test host's: a `talos_config_dir` set in `local.yml`
would lose to `group_vars/kubernetes/`, which Ansible reads after `group_vars/all/`, so the setting has a name of its
own. `talos_secrets_file` is `secrets.yaml` there, with a copy in the owner's password manager
(`scripts/runbooks/talos.md`, which also says to back the directory up with the age key). `talos_endpoint` is
`https://<control plane>:6443` from `local.yml`'s `kubernetes_node_addresses`, and `talos_extra_sans` is `local.yml`'s
`kubernetes_api_sans`, names the API can later be reached by, set before the first build. `talos_upgrade_command` is
`status_commands.talos_upgrade` (`group_vars/all/status.yml`), in `just status`'s words.

**The pins** live in `group_vars/all/kubernetes.yml`, since the Proxmox play reads them too (it downloads the ISO of
`talos_version`, `kubernetes_iso_schematic`'s, which is the `base` schematic): `talos_version`, `talos_contract`
(fixed, so a newer talosctl renders the same configuration), `talos_kubernetes_version` (the justfile's
`kubernetes_version` too, which kubeconform validates the manifests against), the ISO's SHA-256
(`kubernetes_iso_sha256`), `talos_schematic_pins` and `talos_installer_digests`. The Image Factory publishes no
checksum, so each is pinned once, from a download checked against the copy already in use. A bump moves them
together, quarterly, with CI's `TALOSCTL_VERSION` and its SHA-256 (`.github/workflows/ci.yml`), which must be the
pinned release itself; it adds the new release's installer digests and keeps those of the release the nodes run
until `just talos-upgrade` has moved them, since a running node's configuration is rendered with its own release's
installer, which without its digest would be a bare tag that every run would want to apply.

**The schematics**: `base`, the QEMU guest agent, and `gpu`, Intel's i915 driver and firmware besides. No CPU
microcode: the host applies it to a VM.

**The nodes** (`talos_nodes`) come from `kubernetes_nodes` (`group_vars/all/kubernetes.yml`), the same two in both
setups, a control plane and a worker. Each gets a `HostnameConfig` (its name, `auto: "off"`), and a `UserVolumeConfig`
for each disk it has: `local-path-provisioner` on the data disk and `media` on the media disk (the worker's, without
a NAS), each xfs, at least 10 GiB and grown to the disk. Talos mounts a user volume under `/var/mnt/<name>`, where
local-path-provisioner (`/var/mnt/local-path-provisioner`) and the media settings Flux substitutes
(`MEDIA_LOCAL_PATH`, `FRIGATE_LOCAL_PATH`) look, so a volume's name is a path other files depend on.
`kubernetes_settings_error` refuses a data or media disk under 11 GiB: the volume takes 10 GiB, and the partition
table a little more.

**Disks are found by their slot, never their serial.** The install disk and the user volumes are selected by the
by-id link QEMU names after the disk's SCSI slot, `scsi-0QEMU_QEMU_HARDDISK_drive-scsiN` (the selectors match a link
ending in `_drive-scsiN`), the slots being `kubernetes_disk_slots` (`group_vars/all/kubernetes.yml`: system `scsi0`,
data `scsi1`, media `scsi2`), which the VMs' layout (`group_vars/proxmox/vms.yml`) reads too, so the two cannot
drift apart. Proxmox gives each disk `device_id=drive-scsiN`, which udev prefers to the `serial=` `roles/proxmox_vm`
sets, and Talos reads no `disk.serial` from a virtio-scsi disk: both seen on the home host (Proxmox VE 9.2, Talos
1.14.2) in the first live run, 2026-10-17. The install selector (`talos_patch_install`, an `UnattendedInstallConfig`)
names the system disk's slot and wipes the disk first; a user volume's selector also excludes the system disk.

**The other patches.** Every node: a `WatchdogTimerConfig` on `/dev/watchdog0` with a 2-minute timeout, which arms
the VM's i6300esb watchdog so a hung node is reset, and the firewall (below). The control plane: its controller
manager's, scheduler's, kube-proxy's and etcd's metrics bound to `0.0.0.0`, for kube-prometheus-stack, set before the
first build, since etcd reads its settings only when it starts and a later change waits for a restart of etcd by
hand; and Flannel's network policy agent, kube-network-policies (`talos_patch_network_policies`, a
`KubeFlannelCNIConfig`): without it Kubernetes accepts every NetworkPolicy and nothing enforces one, so every pod
would reach every other pod's ports and, through the node's address, the NAS. The control plane renders the
cluster's CNI manifests, so it alone needs that patch. Nothing lets the control plane run apps: it keeps Talos's
taint, and the label that keeps load balancers off it, in both setups, so the worker runs the apps. The workers get
no patch of their own (`talos_patches_worker: []`).

**The GPU.** With `local.yml`'s `kubernetes_gpu_mapping` (a Proxmox PCI resource mapping made by hand,
`scripts/runbooks/gpu.md`), the worker (`kubernetes_data_node`) boots the `gpu` schematic and gets a
`KubeNodeConfig` labelling it `homeserver.internal/gpu: intel`, which Intel's GPU plugin (`k8s/infrastructure/gpu/`)
selects; `roles/proxmox_vm` gives its VM `hostpci0`. On a running cluster the label is drift, applied by the next
`just deploy kubernetes`, while the schematic is a pin the worker waits for: restarting the VM for `hostpci0` and the
`just talos-upgrade` that moves the worker to the `gpu` schematic are by hand.

### The nodes' firewall

The nodes' ingress firewall drops whatever no rule lets in, so a new port on a node needs a rule. It is
`talos_patches_firewall_common` and `talos_patches_firewall_controlplane`: a `NetworkDefaultActionConfig` that
blocks, and a `NetworkRuleConfig` for each port let in.

| Rule | Port | On | From |
|---|---|---|---|
| `talos-api` | tcp 50000, the Talos API | every node | anywhere, IPv4 and IPv6 |
| `kubernetes-api` | tcp 6443, the Kubernetes API | the control plane | anywhere, IPv4 and IPv6 |
| `trustd` | tcp 50001 | the control plane | the nodes |
| `etcd` | tcp 2379-2380 | the control plane | the control plane alone |
| `kubelet` | tcp 10250 | every node | the nodes and the pod network |
| `node-metrics` | tcp 9100 (node-exporter), 10249 (kube-proxy) | every node | the nodes and the pod network |
| `controlplane-metrics` | tcp 2381 (etcd), 10257 (controller manager), 10259 (scheduler) | the control plane | the nodes and the pod network |
| `flannel-vxlan` | udp 4789 | every node | the nodes |
| `metallb-memberlist-tcp`, `metallb-memberlist-udp` | 7946 | every node | the nodes |

The two APIs answer the whole LAN, since the workstation's address changes, and both require a client certificate.
The nodes are taken from `kubernetes_nodes`, each its `/32`, never from every line of `kubernetes_node_addresses`.
The pod network is talosctl's default, which the role asks talosctl for as it renders (`talos_pod_subnets`), so no
private range is written in the repository: Prometheus scrapes the kubelet and the metrics from it, since a pod
reaches its own node's address from the pod network, while a pod on another node reaches it from that node's
address. Everything else to a node is dropped, among it kube-proxy's health checks, the node port Kubernetes gives
Traefik's Service for its local endpoints among them, and kube-network-policies' metrics. Talos always lets in
established connections, ICMP, traffic between pods and services, and pods' DNS queries to the node, and the
firewall filters only what is addressed to the node itself, never what kube-proxy forwards to pods, so the load
balancers' addresses (Traefik's, Frigate's) are untouched. A hostNetwork pod's new port, or a new scrape target on a
node, times out until a rule names it. Try a changed rule first with `talosctl apply-config --mode try` on the
rendered configuration (`-e talos_keep_rendered=true`), which reverts by itself: a rule that closes 50000 locks the
role out of the node, leaving only its console in Proxmox and a reinstall.

## Tests

- `roles/talos/filter_plugins/talos.py` reads talosctl's and kubectl's output (a node's tag, the client's minor, the
  schematic a node booted, a service's state, the install status, the volumes' phases, the user volumes a node's
  patches declare, the server's Kubernetes version) and names the installer images, by tag and digest and by digest
  alone. It is pure, connects nowhere, and is unit-tested in `tests/unit/test_talos_filters.py`.
- `just lint talos` (`tests/talos_validate.yml`, one setup at a time in `tests/talos_validate_setup.yml`) renders the
  nodes of three setups (with a NAS; without one, the worker with a media disk; without one, with the iGPU, the media
  on the data volume and extra API names) through the role's own `render.yml`, with the pins, placeholder addresses
  and a throwaway secrets bundle, nothing connecting anywhere. It runs `talosctl validate --mode metal --strict` on
  each configuration, which catches a misspelt field a render alone lets through, then checks that each one's
  `UnattendedInstallConfig` names its pinned installer image and leaves reboot on, which the install wait counts on,
  and that each kubelet rule names the pod network the node gives its pods. talosctl comes from `PATH`: without it
  the check is skipped locally, saying so, and fails in CI, whose kubernetes job downloads it by version and
  checksum.
- `tests/policy/test_roles.py`: `test_talos_nodes_resolve_on_the_kubernetes_host` resolves the nodes and patches on
  the kubernetes host through the real inventory layout in both setups (the two nodes, the control plane's taint
  kept, the policy agent on, each firewall rule's sources), and `test_talos_pins_match_the_justfile_and_ci` holds
  `talos_kubernetes_version` to the justfile's `kubernetes_version`, `talos_contract` to `talos_version`, and CI's
  `TALOSCTL_VERSION` to `talos_version`.
- The idempotence tests (`test_talos_*` in `tests/integration/test_idempotence.py`, marked `cluster`) run
  `kubernetes.yml`'s second play (`tests/integration/talos.yml`) and the `upgrade` and `etcd-snapshot` entry points
  (`tests/integration/talos_upgrade.yml`) with this repository's configuration, against `fake_talos.py` behind
  `fake_talos_bin/` (`talosctl`, `kubectl` and `git`, which `roles/flux_bootstrap`'s, `roles/app_scale`'s and
  `just status`'s tests use too) and an Image Factory in the test's process that answers 201 with the body's SHA-256 as
  the id, as the real one does. The fake is written from talosctl v1.14.2's flags and output and the Talos
  documentation, not from a node, its upgrade from the home cluster's failure and Talos's source: a configured node
  answers only with a talosconfig and one in maintenance mode only `--insecure`, an installing node goes through
  its phases in the background, and an upgrade's image named by tag and digest fails as a Talos 1.14 node fails it.
  It renders a configuration as text carrying the secrets bundle's cluster secret, which the tests prove never
  reaches the output, makes real client certificates with openssl, so the expiry check runs, and fails a flag it
  does not list, as an unexpected command fails the test. The tests run with `HOME` set to their own directory, and
  `talos_config_dir` and the secrets bundle are the test's own, never the owner's.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
