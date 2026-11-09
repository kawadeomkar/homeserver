# proxmox_node_exporter

Puts Prometheus's node exporter on a Proxmox VE node, for a Prometheus elsewhere to scrape on port 9100: the node's
own CPU and memory across every guest, its disks, filesystems, temperatures and systemd units, which an exporter
inside a VM cannot see. Debian's build leaves out the filesystems under `/run`, `/mnt` and `/media`, so the NFS
storages Proxmox mounts under `/mnt/pve` are not among them. It installs Debian's `prometheus-node-exporter` with
`apt-get` over SSH and keeps it enabled and running. With only its defaults it changes nothing.

Only the node itself and `proxmox_node_exporter_scrapers` may connect. systemd's IP filter on the exporter's unit
(`IPAddressDeny=any` and `IPAddressAllow=`, in the drop-in
`prometheus-node-exporter.service.d/10-allowed-addresses.conf`) drops every other packet to or from it, whatever the
firewalls allow. The drop-in is written, and systemd reloaded, before the package is installed: the package's scripts
start the exporter as soon as it is unpacked, listening on every address. A change to the addresses rewrites the
drop-in and restarts the exporter. The filter needs a kernel with cgroup eBPF, as every Proxmox VE kernel has;
without one, systemd ignores the lines and the exporter answers anyone who can reach the node.

The package's recommended collectors (`prometheus-node-exporter-collectors`: timers that run SMART, apt, IPMI and
NVMe scripts as root) are left out, with `apt-get install --no-install-recommends`. Upgrades are apt's, with every
other package (`roles/proxmox_apt`), which also refreshes the package lists this role installs from. The role never
removes the exporter: emptying the list leaves it as it was.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `proxmox_node_exporter_scrapers` | `[]` | The IPv4 addresses that may scrape the exporter, such as the nodes a Prometheus runs on. Empty, nothing is installed or changed |
| `proxmox_node_exporter_systemd_dir` | `/etc/systemd/system` | Where systemd reads the exporter's drop-in |

## Example

```yaml
# group_vars/proxmox.yml
proxmox_node_exporter_scrapers: [192.0.2.31]
```

A Prometheus scrapes `<node>:9100`. Run from a pod, its traffic leaves its node with the node's address, which is the
one to allow.

## In this repository

`proxmox.yml` runs the role over root SSH, last, after `roles/proxmox_apt`, which refreshes the package lists it
installs from earlier in the same run, and `roles/proxmox_backup`. `group_vars/proxmox/node_exporter.yml` gives it
the cluster's nodes (`kubernetes_node_addresses`) only when there is a cluster: without one there is no Prometheus,
and the role installs nothing. The cluster's Prometheus runs on the worker, and a pod's traffic leaves its node with
the node's address, so it scrapes from the worker's; both nodes are allowed. It scrapes the host as one more target,
`${PROXMOX_ADDRESS}:9100` in the chart's `additionalScrapeConfigs` (`k8s/infrastructure/monitoring/release.yaml`),
under the nodes' exporters' own job, `node-exporter`. The host is on the tailnet too (`roles/proxmox_tailscale`):
the filter drops the tailnet's packets as well as the LAN's, whatever either firewall allows.

`tests/integration/proxmox_node_exporter.yml` is a test playbook of its own, not part of `proxmox.yml`, which the
other tests run many times; the nodes' addresses come from the test as extra vars, and the drop-in goes to the
scratch directory it passes as `proxmox_node_exporter_systemd_dir`. It runs against `fake_pve.py`'s
`apt-get install` (whose package's scripts enable and start the unit, as a real install's do), `dpkg-query` and
`systemctl`, whose `daemon-reload` reads the drop-ins and whose started unit keeps the drop-ins it started with, so
the test proves the exporter first started with the filter loaded. Every command runs through `command`, not a
module, so a fake on `PATH` can stand in for it. `test_proxmox_node_exporter_role_is_idempotent` and
`test_proxmox_node_exporter_without_a_cluster` (`tests/integration/test_idempotence.py`) cover the first run, drift,
a new address and a string for the list; `tests/policy/test_roles.py` resolves the scrapers in the four setups
(`test_node_exporter_follows_the_cluster`) and checks the input refusals (`test_node_exporter_inputs_are_checked`).

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
