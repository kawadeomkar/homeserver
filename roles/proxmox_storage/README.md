# proxmox_storage

Adds NFS shares to a Proxmox VE node as storage, with `pvesm` over SSH, and corrects the content, format,
mount options and enabled state of a storage that already exists. It can also check that storage the node
already has, usually the installer's `local-lvm` or `local-zfs`, can hold VM disks. With only its defaults
it changes nothing.

It refuses a storage id that already exists with another type, server or export, rather than replacing
a storage that may hold disks, and an export that another storage id already mounts: one export, one id,
or the same volumes are listed under two names and a disk deleted under one is gone under the other. That
is what a renamed id looks like until the old one is removed by hand, which the role never does; the
refusal names both ids, whether or not the wanted id exists yet. Renaming an id therefore means removing
the old one by hand first: `pvesm remove <id>`, once `pvesm list <id>` shows it holds no volume (if it holds
some, keep that id in `proxmox_storage_nfs` instead), which leaves an NFS storage mounted at its old path
until the next reboot. Until then every run, dry or real, refuses. An id Proxmox would reject (its rule:
two or more letters, digits, hyphens, underscores and dots, starting with a letter and ending with a letter
or digit) and an export listed twice are refused before anything runs, so a dry run catches them.
`community.proxmox.proxmox_storage` is not used: it only creates storage, never corrects it, and has no
format option.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `proxmox_storage_nfs` | `[]` | Storages: `id`, `export`; optional `server`, `content`, `format`, `nfs_options`, `enabled` |
| `proxmox_storage_nfs_server` | `""` | NFS server for entries without their own |
| `proxmox_storage_local` | `[]` | Ids of storage of any type the node must already have; checked, never created or changed |
| `proxmox_storage_nfs_options` | `""` | Mount options, e.g. `vers=4.2`; empty leaves Proxmox's defaults |
| `proxmox_storage_format` | `qcow2` | Default image format |
| `proxmox_storage_content` | `[images]` | Content types |
| `proxmox_storage_enabled` | `true` | Enabled state |
| `proxmox_storage_node` | `""` | Node name; empty means the host's short hostname |
| `proxmox_storage_startall_delay` | `null` | Seconds to wait at boot before starting VMs (0-300); null leaves it alone |
| `proxmox_storage_startall_wait` | `[]` | Storage ids the start at boot then waits for, until each is active |
| `proxmox_storage_startall_wait_timeout` | `600` | The longest that wait lasts, in seconds |
| `proxmox_storage_systemd_dir` | `/etc/systemd/system` | Where the wait's drop-in for `pve-guests.service` goes |

The input check gives each problem a sentence of its own, naming the variable to change: ids repeated or empty
across `proxmox_storage_nfs` and `proxmox_storage_local`, an id Proxmox would reject, an export listed twice, an NFS
entry with no server, a timeout that is not a whole number of seconds above 0. It refuses a string for
`proxmox_storage_local` or `proxmox_storage_startall_wait`: argument validation accepts a string for a list but
leaves the variable as it was, so without the check the role would iterate its characters.

A disk imported with `import-from` keeps the source image's format whatever the storage default says.
Whatever imports images must ask for `format=qcow2` itself.

Each id in `proxmox_storage_local` must exist on the node, not be disabled and have `images` in its
content. Its type is not checked, so an NFS storage set up by hand passes as well as the installer's own.
The role checks that from the storage configuration and fails, in check mode too, naming the id and what
it lacks and listing the storage Proxmox has. Whether a storage is active on the node, for these ids and
every NFS storage alike, is checked only for real, at the end of a run: a dry run reports `present` for a
storage that cannot be activated or is restricted to other cluster nodes, and the real run then fails
naming it and the reason. The installer creates `local-lvm` on an LVM install and `local-zfs` on a ZFS
one; `local` holds ISOs and backups and cannot take VM disks. The role does not create storage.

Proxmox starts each guest marked start-on-boot once, from `pve-guests.service`, after the node's delay. A VM whose
disk is on a storage that is not active then fails to start and is never tried again, so after a power cut a VM on
a NAS that boots more slowly than the delay stays down. With `proxmox_storage_startall_wait`, a drop-in
(`pve-guests.service.d/proxmox-storage-wait.conf`) holds the start, after the delay, until each of those storages is
active in `pvesm status` (which mounts an NFS storage as it reads it), for at most
`proxmox_storage_startall_wait_timeout` seconds, then lets the guests start whatever the storages say. The unit has
no start timeout of its own. The role reloads systemd when it writes or removes the drop-in.

## Examples

A NAS's shares as VM-disk storage:

```yaml
# group_vars/proxmox.yml
proxmox_storage_nfs_server: 192.0.2.10
proxmox_storage_nfs_options: vers=4.2
proxmox_storage_nfs:
  - id: nas-vm
    export: /mnt/tank/vm
proxmox_storage_startall_delay: 120
proxmox_storage_startall_wait: [nas-vm]
```

No NAS: VM disks stay on the storage the installer created, which the role only checks:

```yaml
# group_vars/proxmox.yml
proxmox_storage_local: [local-lvm]
```

In check mode the role reads the node's configuration and prints its plan (`add`, `correct` or `ok` per NFS
storage, `present` per checked storage), and every refusal above applies. The `pvesm` commands themselves
run only for real.

## In this repository

`proxmox.yml` runs the role over root SSH, third: after `roles/proxmox_apt`, and after `roles/proxmox_tailscale`
so that remote access never waits on a storage this role refuses; before `roles/proxmox_backup`, whose job writes
to a storage this role adds (the NAS's `homeserver-backup`: `group_vars/proxmox/backup.yml` lists every id in
`proxmox_storage_nfs` as pending, so a dry run on a fresh host passes). With a NAS, `truenas.yml` runs first
(`site.yml` runs it before `proxmox.yml`), so the shares exist and are exported to this host.

`group_vars/proxmox/storage.yml` holds the Proxmox storages and the boot delay. The NFS list is
`vm_storage_nfs_exports`, from the VM storage layout in `group_vars/all/storage.yml`, which the NAS play exports from
too, so a share and the storage that mounts it cannot drift apart. With a NAS it adds, in qcow2 with `vers=4.2`
(pinned, so an upgrade on either side cannot change the protocol version; `nconnect` is left out until it has been
measured): `homeserver-persistent` and `homeserver-ephemeral`, one per project pool in `vm_storage_consumers` (today
`claude-on-proxmox-ephemeral`), and `homeserver-backup`, which holds backups only. The server is `nas_address`, the
NAS's static address, never `truenas_bootstrap_address`, a DHCP lease the NAS play replaces: with only the bootstrap
address the role refuses, naming `proxmox_storage_nfs_server`. At boot, as after a power cut, the VMs marked
start-on-boot wait 120 seconds (the NAS boots more slowly than Proxmox), then up to ten minutes more for each of
the NAS's storages that holds VM disks, the backups' left out.

`proxmox_storage_local` is the installer's storage (`vm_storage_installer`: `local-lvm`, or `local.yml`'s
`proxmox_local_vm_storage`, such as `local-zfs` on a ZFS install), checked without a NAS, where it is the only VM
storage, and with a NAS when there is a cluster, whose EFI disks go there; that is the variable to change when the
role refuses the storage. Without a NAS (`nas_present` false) the NFS list is empty, the boot delay null, so the
node's own is left alone, and nothing is waited for: a wait's drop-in left from a setup with a NAS is removed, and
systemd reloaded. There is then no persistent class and no project pool.

Storage ids say who owns a storage and what it is for, never the backend, so a project told an id learns nothing
about what is behind it and the id never changes with it. They were `truenas-*` until 2026-10-08; a host set up
before then is refused, naming both ids, until the old ones are removed by hand as above.

`tests/integration/test_idempotence.py` runs the role against `fake_pve.py` behind `fake_pve_bin/`: with this
repository's configuration through `tests/integration/proxmox.yml` (after `roles/proxmox_apt`), and with the test's
own variables through `tests/integration/proxmox_local.yml`. It covers a dry run, an apply, a second apply, a dry
run, drift corrected once, two content types, the setup without a NAS (with a wait left from a NAS), a NAS known only
by its bootstrap address, every refusal, an inactive storage and one restricted to another node, and the old
`truenas-*` ids beside the new. The fake answers as Proxmox VE 9.2 does: the setup without a NAS was run against a
fresh Proxmox VE 9.2.2 install, which is where the fake's `digest` on every `/storage` entry, `shared` in every
status answer and `content` in an order that differs from one listing to the next come from, so the role sorts
content before comparing it. `tests/policy/test_roles.py` resolves the layout in each setup
(`test_layout_resolves_per_setup`, `test_local_yml_names_the_installer_storage`) and runs the argument-spec pass in
each.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
