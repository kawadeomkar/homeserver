# proxmox_node_exporter

Puts Prometheus's node exporter on a Proxmox VE node, for a Prometheus elsewhere to scrape on port 9100: the node's
own CPU and memory across every guest, its disks, filesystems, temperatures and systemd units, which an exporter
inside a VM cannot see. It installs Debian's `prometheus-node-exporter` with `apt-get` over SSH and keeps it enabled
and running. With only its defaults it changes nothing.

Only the node itself and `proxmox_node_exporter_scrapers` may connect. systemd's IP filter on the exporter's unit
(`IPAddressDeny=any` and `IPAddressAllow=`, in the drop-in
`prometheus-node-exporter.service.d/10-allowed-addresses.conf`) drops every other packet to or from it, whatever the
firewalls allow. The drop-in is written, and systemd reloaded, before the package is installed: the package's scripts
start the exporter as soon as it is unpacked, listening on every address. A change to the addresses rewrites the
drop-in and restarts the exporter. The filter needs a kernel with cgroup eBPF, as every Proxmox VE kernel has;
without one, systemd ignores it.

The package's recommended collectors (`prometheus-node-exporter-collectors`: timers that run SMART, apt, IPMI and
NVMe scripts as root) are left out. Upgrades are apt's, with every other package (`roles/proxmox_apt`), which also
refreshes the package lists this role installs from. The role never removes the exporter: emptying the list leaves
it as it was.

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

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `make docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
