# talos

Talos Linux for the cluster's nodes, from the controller with `talosctl`. So far it renders each node's machine
configuration; installing it on the nodes comes next. With only its defaults it does nothing.

`tasks/render.yml` renders each node's configuration with `talosctl gen config`, a secrets bundle and the patches,
into a scratch directory the caller names (`talos_workspace`, which holds `secrets.yaml`), for the nodes the caller
lists (`talos_render`: each node's `name`, `role`, `contract`, `kubernetes_version`, `install_image` and
`patches`). It first asks talosctl for the pod network it gives a cluster by default, which the patches can use as
`talos_pod_subnets` (a firewall rule for the pod network, say) without writing the range out. A render runs locally,
in check mode too, and never prints the rendered configurations, which hold the bundle's keys.

`make lint-talos` renders every setup of this repository through it (`tests/talos_validate.yml`) and validates each
configuration with `talosctl validate --mode metal --strict`.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `talos_cluster_name` | `""` | The cluster's name |
| `talos_endpoint` | `""` | The Kubernetes API, `https://<control plane address>:6443` |
| `talos_extra_sans` | `[]` | More names for the API server's certificate |
| `talos_contract` | `""` | The configuration contract talosctl renders for, `talos_version`'s minor |
| `talos_patches_common` | `[]` | Patches for every node, as YAML documents in strings |
| `talos_patches_controlplane` | `[]` | Patches for control planes |
| `talos_patches_worker` | `[]` | Patches for workers |

This repository's configuration is `group_vars/kubernetes/talos.yml`.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `make docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
