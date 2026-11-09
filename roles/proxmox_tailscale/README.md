# proxmox_tailscale

Joins a Proxmox VE node to a tailnet, and with routes makes it the LAN's Tailscale subnet router. With
`proxmox_tailscale_enabled` alone, the node is a tailnet device of its own, reachable at its tailnet address, and
forwards nothing from the tailnet. With routes, it also advertises the LAN's prefixes, so every VM on its bridge, and
every other host on those prefixes, answers from the tailnet at its LAN address without Tailscale of its own.
Tailscale masquerades that traffic to the node's own address, so nothing on the LAN needs a route back. Either way
the node runs Tailscale behind a forwarding guard of its own. With only its defaults the role changes nothing; with
`proxmox_tailscale_enabled` false it does nothing at all.

In order, on every run:

1. **Tailscale's repository.** Its signing key is downloaded once from Tailscale's repository, as Tailscale's
   instructions do. The `tailscale-archive-keyring` package then owns the same file and brings a new key through
   apt. The repository is written as a deb822 `tailscale.sources` file, for the node's Debian release.
2. **The packages.** `tailscale`, `tailscale-archive-keyring` and `nftables` are installed and, with
   `proxmox_tailscale_upgrade`, upgraded, through `apt-get install`. Without it, an upgrade of the whole node, such as
   `roles/proxmox_apt`'s, still upgrades them with the rest. Tailscale's own updater is meant to stay off: on Debian
   it runs apt on its own schedule, and works only with a `tailscale.list` file.
3. **The guard** (below), loaded before anything is forwarded.
4. **IPv4 forwarding**, with routes only, written to `/etc/sysctl.d` and turned on. Without routes, forwarding is
   left as it is, and nothing from the tailnet is forwarded: tailscaled delivers no packet for an address the node
   neither holds nor advertises, and the guard drops all but the guarded hosts' allowed ports (below). IPv6
   forwarding stays off: it would stop the node accepting router advertisements on its bridge (`vmbr0`). IPv6 routes are
   refused for the same reason. Forwarding is the whole node's: with a second interface that has an address, such
   as a VLAN or a storage network, the node also routes between it and the LAN, which the guard does not filter.
   Bridged VM traffic is switched and needs none.
5. **The login.** `tailscaled` is enabled and started if it is not running: stopped by hand, or never started at
   boot because the guard it requires failed to load. A `tailscaled` without the interface `tailscale0`, which the
   guard filters by name, is refused before any login: one started with `--tun` naming another interface, or with
   userspace networking, whose connections come from the node itself. A logged-out node is logged in once with
   `proxmox_tailscale_auth_key`, a one-off, pre-approved key tagged with `proxmox_tailscale_tags`. The key reaches
   tailscale only as `--auth-key=file:<path>`, from a private temporary file removed afterwards, so it never appears
   in an argument list or in Ansible's output. A node stopped by hand (`tailscale down`) is brought back up without
   a key. `tailscale up` gets every preference it takes, so the node never runs, even for a moment, with Tailscale's
   defaults.
6. **The preferences.** The routes and `proxmox_tailscale_settings` are compared with `tailscale get --json`, which
   is keyed by `tailscale set`'s own flag names, and set with one `tailscale set` when anything differs.
7. **A report** of what only the tailnet's admin console, or the owner on the node, can fix:
   - a route advertised but not approved;
   - tags other than `proxmox_tailscale_tags` (tags change only by re-authenticating, so the role never changes
     them);
   - a key that expires;
   - a Tailscale Serve or Funnel configuration;
   - Tailscale's own health warnings.

   The judgment is a filter plugin (`filter_plugins/proxmox_tailscale.py`, with which preferences differ), which the
   role's read-only `status` entry point shares, so `just status` says the same. The report is never a failure. Each
   item but the health warnings is also recorded as left to do, under `homeserver_next`'s `tailscale` (NEXT STEPS);
   the health warnings are shown only, as `just status` shows them.

**The `status` entry point** (`tasks_from: status`) reads the node and changes nothing, with the role's module
`tailscale_read`, which runs `tailscale status --json`, `get --json` and `serve status --json`, the guard's table (`nft
list table inet tailnet_guard`) and IPv4 forwarding (`sysctl -n net.ipv4.ip_forward`) at once, in one round trip to the
node, where five tasks took 1.1 to 1.4 s on the home host, and runs nothing else. Whether `tailscaled` runs is whether
`tailscale status` answers, and whether the guard is loaded whether nft lists its table, so there is no `systemctl`.
It records in `proxmox_tailscale_status_state` what they say against the role's
variables: whether Tailscale is installed and `tailscaled` answers, the backend state, the node's tailnet name,
addresses, tags and key expiry, each route as configured, advertised and served by this node, the preferences, a
Serve configuration, the health warnings, the guard's sets as loaded, forwarding, and the report above. `tailscale
status --json` lists every device and login on the tailnet, and for a personal tailnet its name is the owner's
login: the reads are under `no_log`, and only the node's own entry is kept. A node that does not answer is recorded,
never a failure. `just status` (roles/status) runs it.

The role never logs the node out, deletes it, changes its tags, restarts `tailscaled` itself, or advertises an
exit node. It never touches `/etc/network/interfaces`, `/etc/resolv.conf`, `/etc/nftables.conf`, Debian's
`nftables.service`, another nftables table or Proxmox's firewalls. Taking the routes out withdraws them and reloads
the guard with none, leaving forwarding as it was; turning `proxmox_tailscale_enabled` off stops the role doing
anything. Either way it removes nothing.

## The guard

A subnet router that masquerades makes every tailnet client look like the node to the LAN. Anything on the LAN that
trusts the node by its address, such as an NFS server exporting to it with `sec=sys`, trusts every tailnet client
the tailnet's policy lets through. The policy is enforced by tailscaled on this node. The guard is a second control
that holds even if the policy is reset to allow-all: each of the two is enough alone, and both are allow-lists, for
the reason the first rule below gives. A node without routes forwards nothing, and loads the guard
all the same: tailscaled requires it, and routes added later need no other step.

It is an nftables table of its own, `inet tailnet_guard`, in `proxmox_tailscale_guard_file`. Its forward chain runs
at priority `filter - 10`. A drop in any nftables base chain is final, so the guard holds whatever Tailscale's own
rules (iptables or nftables) and Proxmox's firewalls accept. It:

- drops everything from `tailscale0` to `proxmox_tailscale_guard_hosts`, except `proxmox_tailscale_guard_allow`. It
  is an allow-list because an NFS server's mountd, statd and lock manager listen on random ports. A host is guarded
  at the addresses listed, so list every address it answers on;
- drops everything from `tailscale0` outside the advertised prefixes, and IPv6, as Tailscale advises a subnet
  router's firewall to deny forwarding by default;
- drops new connections from the LAN into the tailnet, the host-side equivalent of `--stateful-filtering`.

The rules run in that order, so a guarded host's allowed ports pass the guard even outside the advertised
prefixes, and on a node without routes, which has none. Tailscale delivers neither: tailscaled drops a packet for
an address the node neither holds nor advertises, before it reaches the kernel.

**How it is loaded.** Its unit, `tailnet-guard.service`, loads it before `network-pre.target` and before
`tailscaled`. A drop-in, `tailscaled.service.d/10-tailnet-guard.conf`, makes `tailscaled` `Requires=` and `After=`
it, which makes the guard **fail-closed**: if the table cannot load, Tailscale does not start and nothing is
forwarded, at the cost of remote access until someone fixes it at the console. The next run then loads the guard and
starts `tailscaled` again. Fail-closed covers starting: a table
removed while both run, with `nft flush ruleset` or `nft delete table`, leaves `tailscaled` forwarding unguarded until
the next run, which loads it again.

**How the role changes it.**

- Every file is checked with `nft -c` before it is installed.
- Every change is a reload, `systemctl reload-or-restart tailnet-guard.service`: a reload when the unit is active, a
  start when it is not, never a restart. A reload replaces the table atomically, keeps the old one if the new one
  fails, and, unlike a restart, does not reach `tailscaled` through its `Requires=`.
- The unit has no stop command, so stopping it never removes the table; it stops `tailscaled` instead.

By hand too: reload `tailnet-guard.service`, never restart it, which restarts Tailscale.

**Debian's `nftables.service` is refused, enabled or running.** It loads `/etc/nftables.conf`, which on Debian begins
with `flush ruleset` and would remove the guard at every boot, and its stop command flushes every table. The guard's
unit conflicts with it, so starting it later stops the guard, and `tailscaled` with it, before the flush.

**What it cannot see.** It sees only forwarded packets. A proxy running on the node itself connects from the node's
own address, which the guarded hosts trust: Tailscale Serve with a non-local target (which the report names), `ssh
-L`, socat, or Proxmox SDN's Simple-zone SNAT. So does `tailscaled` itself with the debug setting
`TS_DEBUG_NETSTACK_SUBNETS=1` in its environment (`/etc/default/tailscaled`), which makes it carry the advertised
routes' traffic in userspace although it has `tailscale0`, so the role's interface check passes. Never set it.

**Proxmox's own firewall cannot do this job.** Tailscale accepts tailnet traffic at the top of iptables' INPUT and
FORWARD, so the PVE firewall never filters it. If the datacenter firewall is enabled later, proxmox-firewall's input
chain drops by default, and the tailnet's traffic to the node and Tailscale's own UDP need rules there.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `proxmox_tailscale_enabled` | `false` | Run Tailscale on the node; false, the role does nothing; true without routes, the node joins the tailnet alone |
| `proxmox_tailscale_advertise_routes` | `[]` | The LAN prefixes to advertise, IPv4, in CIDR form, which need `proxmox_tailscale_enabled`; empty, the node forwards nothing from the tailnet |
| `proxmox_tailscale_tags` | `[]` | The tags the node joins with, each `tag:<name>`; at least one once enabled, since an untagged node's key expires |
| `proxmox_tailscale_auth_key` | `""` | A one-off auth key, read only while the node is logged out (`no_log`) |
| `proxmox_tailscale_settings` | `{}` | Preferences kept with `tailscale set`, by its flag names, each a boolean or a string; not `advertise-routes` or `advertise-tags` |
| `proxmox_tailscale_upgrade` | `false` | Upgrade Tailscale on every run; false leaves an installed release alone, though an upgrade of the whole node, such as `roles/proxmox_apt`'s, still upgrades it |
| `proxmox_tailscale_guard_hosts` | `[]` | IPv4 addresses that tailnet clients may reach only on `proxmox_tailscale_guard_allow` |
| `proxmox_tailscale_guard_allow` | `[]` | The ports still reachable on them, each `tcp/<port>` or `udp/<port>` |
| `proxmox_tailscale_suite` | `""` | The Debian release for Tailscale's repository; empty reads it from the node's `/etc/os-release` |
| `proxmox_tailscale_repository` | `https://pkgs.tailscale.com/stable/debian` | Tailscale's Debian repository, which also serves its signing key |
| `proxmox_tailscale_keyring` | `/usr/share/keyrings/tailscale-archive-keyring.gpg` | Where the signing key goes |
| `proxmox_tailscale_sources_dir` | `/etc/apt/sources.list.d` | Where apt keeps its deb822 files |
| `proxmox_tailscale_sysctl_file` | `/etc/sysctl.d/99-tailscale.conf` | The file that turns on IPv4 forwarding at boot |
| `proxmox_tailscale_guard_file` | `/etc/nftables.d/tailnet-guard.nft` | The guard's table |
| `proxmox_tailscale_systemd_dir` | `/etc/systemd/system` | Where the guard's unit and `tailscaled`'s drop-in go |

The paths are there for the idempotence tests, which point them at scratch directories. Every command runs through
`command`, not a module, so the tests can stand in for apt-get, ip, nft, sysctl, systemctl and tailscale with fakes on
`PATH`.

## Example

The node of a LAN whose NFS server exports to it, kept current, with Tailscale's updater off:

```yaml
# group_vars/proxmox.yml
proxmox_tailscale_enabled: true
proxmox_tailscale_advertise_routes: [192.0.2.0/24]
proxmox_tailscale_tags: [tag:homeserver]
proxmox_tailscale_auth_key: "{{ vault_tailscale_auth_key }}"
proxmox_tailscale_settings: {accept-routes: false, accept-dns: false, ssh: false, auto-update: false}
proxmox_tailscale_upgrade: true
proxmox_tailscale_guard_hosts: [192.0.2.10]
proxmox_tailscale_guard_allow: [tcp/443]
```

Without `proxmox_tailscale_advertise_routes`, the same node joins the tailnet alone: it is reachable at its tailnet
address, as the tailnet's policy allows, and forwards nothing from the tailnet.

A dry run shows the files it would write, with a diff, and what it would install, load, turn on, log in with and
set. On a fresh node, apt does not know Tailscale's repository until a real run has refreshed the lists, so the dry
run says it would install Tailscale without simulating it.

The tailnet's side lives in Tailscale's admin console and is set by hand: the policy (the tag's owner, the grants,
and with routes the route's auto-approver), with routes split DNS, and the auth key. The policy comes first, before
the node's first run and so its first advertisement: a new tailnet lets every device reach every other on every port,
subnet routes included. In this repo, `scripts/runbooks/tailscale.md` says how.

## In this repo

`proxmox.yml` runs the role second, on the Proxmox host over the same root SSH as the other Proxmox roles. It comes
after `roles/proxmox_apt`, since a fresh host installs nothing until the enterprise repositories are off (its refresh
of the package lists fails with 401 until then), and before `roles/proxmox_storage`, whose refusals must not hold
remote access back. `group_vars/proxmox/tailscale.yml` sets it from `group_vars/all/local.yml` and the vault:

- **The switch.** `proxmox_tailscale_enabled` is `local.yml`'s `tailscale_enabled` or, without it, whether
  `tailscale_advertise_routes` lists a route, so routes alone turn Tailscale on. `tailscale_enabled: false` with
  routes is refused, as a contradiction. With neither, the role runs no command and nothing is installed.
- **The routes** are `tailscale_advertise_routes`, the LAN's prefix: every VM on the host's bridge, the NAS and the
  router then answer from the tailnet without Tailscale of their own. With `tailscale_enabled` alone, the host joins
  the tailnet as a device of its own.
- **The tag** is `tag:homeserver`: a tagged node's key never expires, and the tailnet's policy grants by tag.
- **The auth key** is the vault's `vault_tailscale_auth_key`, a one-off, pre-approved key tagged `tag:homeserver`,
  spent at the host's first login and read only while it is logged out. A spent key does nothing, so it stays in the
  vault.
- **The preferences** are accept-routes, accept-dns, ssh and auto-update, all off. A subnet router must not route its
  own LAN back through the tunnel (accept-routes). Proxmox writes `/etc/resolv.conf`, which Tailscale would otherwise
  take over (accept-dns). OpenSSH with per-host keys stays the only root login (ssh). Tailscale's own updater would
  run apt on its own schedule and needs a `tailscale.list` file (auto-update).
- **Upgrades** are `roles/proxmox_apt`'s: `proxmox_tailscale_upgrade` is off, unlike the example above, and that role's
  `dist-upgrade`, earlier in the same run, upgrades Tailscale with every other package. `just deploy proxmox` is then
  the one moment anything on the host is upgraded, and `proxmox_apt_upgrade` the one switch for every upgrade.
- **The guard's hosts** are every address `local.yml` gives the NAS (`truenas_static_address` and
  `truenas_bootstrap_address`) and any other NFS server the host's storages mount (`proxmox_storage_nfs`), with only
  `tcp/443`, the NAS's web UI and API, allowed. Every address, not only the one the storages mount: NFS answers on all
  of them, and with only the bootstrap address the shares are exported while `roles/proxmox_storage`, later in the
  run, refuses to mount them. Without a NAS the guard guards no host and keeps its other rules.

**Why the NAS needs the guard.** The subnet router masquerades every tailnet client to the Proxmox host's address,
which is exactly the address the NAS exports the VM disks and backups to, with root kept as root. Two controls keep
tailnet clients to the NAS's web UI, each enough alone: the tailnet policy's grants, kept by hand in the admin console
(`scripts/runbooks/tailscale.md`), and this role's fail-closed guard. Both are allow-lists, because the NAS's mountd,
statd and lock manager use random ports, so a list of NFS ports to block would miss them. A proxy on the host itself
connects from the trusted address, unseen by the guard (What it cannot see, under The guard): Tailscale Serve with a
non-local target, `ssh -L`, SDN Simple-zone SNAT, or `tailscaled` with `TS_DEBUG_NETSTACK_SUBNETS=1`.

`status.yml` runs the role's `status` entry point on the Proxmox host before `roles/status`, when Tailscale is on and
the sections shown need it, and `group_vars/proxmox/status.yml` hands its `proxmox_tailscale_status_state` to
`roles/status` as `status_tailscale_state`.

## Tests

`tests/integration/test_tailscale.py` (`just test idempotence`) runs the role with this repo's own configuration
(`tests/integration/proxmox_tailscale.yml`, which loads `group_vars/proxmox/tailscale.yml` and the storage files the
guard's hosts come from) against fakes of its own, `tests/integration/fake_tailscale.py` behind
`fake_tailscale_bin/`: `apt-get`, `ip`, `nft`, `sysctl`, `systemctl` and `tailscale`, backed by a state file that
records every call and counts every change. Every path the role writes is a scratch path, the repository a `file://`
URL serving a fake signing key, and the addresses RFC 5737's, so nothing reaches the network or the system.

From a fresh host, a dry run changes nothing and says what it would do; the first run installs Tailscale, loads the
guard, turns on forwarding and logs the node in, in that order; a second run and a dry run after it change nothing;
drift is corrected by one run and left alone by the next. The tests also cover a node stopped by hand, brought back
up without a key; a stopped `tailscaled`, started again after the guard; a logged-out node without a key, refused;
the key never reaching an argument list or Ansible's output; an upgrade installed only when asked for; the guard's
hosts following the setup (none without a NAS, the bootstrap address alone, both addresses); the guard reloaded and
never restarted, since the fake's `systemctl` refuses a plain restart of the guard, which would restart `tailscaled`
through its drop-in; Debian's `nftables.service` refused enabled or running; a `tailscaled` without `tailscale0`
refused before it logs in, in a dry run too; the node alone without routes, forwarding left as it is; routes taken out,
withdrawn; and the repo's preferences, each a flag the fake's `tailscale set` knows, so a typo fails the tests.

The fake follows Tailscale 1.104 on Linux: its flags; `status --json` as 1.104.2 prints it on a Proxmox host, with
another device and its owner, which nothing may keep; `get --json` as a 1.102 client prints it, `advertise-routes` a
comma-separated string; and `dns status --json` as the workstation's own 1.102 prints it, for `just status`. The
rest, `up` and `set` among it, is written from Tailscale 1.104's flags, not captured from the host.
`tests/unit/test_proxmox_tailscale_filters.py` covers the filter plugin with Tailscale 1.104.2's shapes and nft
1.1.3's listing of the guard's table, captured on a real host, every address and name replaced (the tailnet's built at
run time, since the leak checks refuse a tailnet name or a CGNAT address in the repository).
`tests/policy/test_roles.py` resolves the inventory in each setup: the guard's hosts, the switch from
`tailscale_enabled` or routes, and each input refusal's message. `tests/integration/test_status.py` reads a seeded
host's Tailscale through the same fake.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
