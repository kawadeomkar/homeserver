# talos

Builds a Talos Linux cluster from the controller with `talosctl` and `kubectl`, and keeps its configuration. With
only its defaults it does nothing. Three entry points:

- **`main`** (`make apply TYPE=kubernetes`): installs Talos on nodes in maintenance mode, keeps a configured node's
  configuration, bootstraps etcd once, waits for the cluster to be healthy, and writes talosconfig and kubeconfig.
  It never reboots a node and never moves one to another version.
- **`upgrade`** (`make talos-upgrade`; `tasks_from: upgrade`): an etcd snapshot, then each node whose Talos version
  or schematic differs from the pins, control planes first, then Kubernetes with `talosctl upgrade-k8s`. In check
  mode it shows the plan and Kubernetes's own dry run, without pulling images.
- **`etcd-snapshot`** (`make etcd-snapshot`; `tasks_from: etcd-snapshot`): a snapshot of the control plane's etcd
  into `talos_config_dir/etcd/`. It asks only the control plane, never the Image Factory or the other nodes, so it
  works on a degraded cluster.

What `main` does, in order:

- **Checks** the workstation's `talosctl` (its minor release must be `talos_contract`, since talosctl's defaults
  follow its own release), and refuses a talosconfig or kubeconfig in `talos_config_dir` that belongs to another
  cluster: another endpoint, or a CA that is not the secrets bundle's.
- **Probes** each node: configured (it answers with the cluster's talosconfig), in maintenance mode (it answers
  only `--insecure`), or not answering, which a real run waits for (`talos_probe_retries`, `talos_probe_delay`).
- **Renders** each node's configuration with `talosctl gen config`, the secrets bundle and the patches. A node in
  maintenance mode gets the pins; a configured node gets what it runs (its Talos version and schematic, the
  cluster's Kubernetes version), so a pin bump changes nothing until `upgrade` moves it. It first asks talosctl
  for the pod network it gives a cluster by default, which the patches can use as `talos_pod_subnets` (a firewall
  rule for the pod network, say) without writing the range out.
- **Installs** a node in maintenance mode with `apply-config --insecure`, then waits on its install status until
  it reports `installed` (`talos_install_retries`): Talos installs in the background from the ISO and reboots into
  its system disk. A disk selector that matches nothing shows up as a lasting `pending`, with Talos's error.
  Once the cluster was bootstrapped, a node in maintenance mode is installed only when `talos_reinstall` names it
  (or `talos_allow_bootstrap` starts a new cluster): its configuration holds the cluster's keys, and nothing proves
  that what answers at its address in maintenance mode is the node.
- **Corrects drift** on a configured node: `apply-config --dry-run` finds it (the diff is never printed, since it
  can hold secrets), and `--mode no-reboot` applies it at once, which Talos 1.14 does for any configuration. etcd
  reads its settings only when it starts, so after a change to the control plane the run says how to restart it.
- **Bootstraps etcd once**, recording it in `talos_config_dir/bootstrapped`. With that record and etcd not
  running, it refuses: a second bootstrap makes a new, empty cluster. `talos_allow_bootstrap` overrides it, for a
  new cluster on purpose.
- **Checks** the cluster's health, and that every user volume the patches declare is mounted (`ready`).
- **Writes** talosconfig (the control plane as endpoint and node) and kubeconfig to `talos_config_dir`, mode 0600,
  and renews them `talos_renew_before_days` before their client certificates expire; a dry run says it would.

The secrets bundle comes from `talos_secrets_file`, which must exist, be readable by its owner alone (mode 0600) and
lie outside the repository: it holds every key of the cluster, so it is never committed, even encrypted. It is
checked by its text alone and only ever written with `no_log`; the rendered files live in a scratch directory
removed at the end of every run, unless `talos_keep_rendered` keeps a copy, which the next run without it removes.
A run that is interrupted never reaches that removal, so each run removes the scratch directories (`ansible.*.talos`
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
| `talos_secrets_file` | `""` | The file holding the secrets bundle (`talosctl gen secrets`), mode 0600, outside the repository; never printed |
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

A variable for one run goes through `ANSIBLE_ARGS`:
`make apply TYPE=kubernetes ANSIBLE_ARGS='-e talos_allow_bootstrap=true'`. `scripts/runbooks/talos.md` covers building,
upgrading and restoring; this repository's configuration is `group_vars/kubernetes/talos.yml`.

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

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `make fix TYPE=docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
