# proxmox_vm

Creates VMs on a Proxmox VE node with `qm` over SSH, every option given, and on later runs keeps the settings it
manages as configured. Downloads the ISOs they boot from with `pvesh`. With only its defaults it changes
nothing.

The tasks read the node; the `proxmox_vm_plan` filter (`filter_plugins/proxmox_vm.py`, unit-tested in
`tests/unit/test_proxmox_vm_plan.py`) works out what to do from what they read, and the tasks run the commands it
returns. Proxmox writes settings back in its own forms (tags lower-cased and sorted, a disk as a volume id with
its size, property strings in any order, numbers as text), so every setting is compared normalised.

## What it does, and never does

- **Creates** a missing VM with `qm create`, then starts it when `start` is true.
- **Corrects** cores, memory, CPU type, ballooning, start-on-boot, the startup order, the watchdog, the guest
  agent, tags, PCI devices (`hostpci`), the CD-ROM (`ide2`) and the boot order (`boot`, which Proxmox edits when
  a drive is deleted), in one `qm set`; adds a configured disk the VM
  lacks; grows a disk configured larger (`qm disk resize`); starts a stopped VM marked `start`.
- **Reports**, never changes: every VM's MAC, for checking against the router's DHCP reservations; a MAC that
  differs from the configured one; every setting that waits for a restart (on a running VM, Proxmox applies
  cores, CPU, memory, ballooning, the watchdog, the agent, the boot order and PCI devices only at the next start);
  ISOs named like the role's own that no VM on the node uses.
- **Never** moves, shrinks or detaches a disk, deletes a VM or an ISO, ejects the CD-ROM, or restarts a VM.
- **Refuses**, before changing anything:
  - a VMID that belongs to a VM of another name, or to a container;
  - a missing VM whose VMID still has disks or backups on any of the node's storages (files that belong to no
    VMID, such as imports and snippets, aside): it is being rebuilt, not built, and new empty disks beside the old
    ones would lose them. Restore it (`scripts/runbooks/vm-restore.md`), or set
    `proxmox_vm_ignore_existing_volumes` to build it new;
  - a missing VM while a storage is enabled but not active (an unplugged USB disk, an unreachable NAS), which could
    hold its old disks or backups unseen; `proxmox_vm_ignore_existing_volumes` lifts this too;
  - a disk storage that does not exist, is disabled or holds no images (in check mode, one listed in
    `proxmox_vm_pending_storages` is reported as pending instead);
  - **the disk budget**: on thin LVM and ZFS, the provisioned sizes of every disk on the storage, other VMs' and
    containers' included, plus what this run adds, may not exceed the storage's size. It is counted from what is
    provisioned, never from what is free, so the answer is the same on every run. `proxmox_vm_allow_overcommit`
    lifts it;
  - a disk configured smaller than it is, on another storage than it is, or a disk slot the configuration no
    longer lists.
- **Warns** when the running VMs' memory plus the VMs it would start exceed the node's memory less 2 GiB.

A download's own task status is checked: `pvesh` can exit 0 for a download that failed.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `proxmox_vm_vms` | `[]` | VMs (below) |
| `proxmox_vm_isos` | `[]` | ISOs to download: `storage`, `filename`, `url`, and `sha256`, the file's SHA-256, which Proxmox checks before keeping the download |
| `proxmox_vm_pending_storages` | `[]` | Storage ids an earlier role adds on a real run; pending in check mode |
| `proxmox_vm_allow_overcommit` | `false` | Let the disks on thin LVM or ZFS add up to more than it holds |
| `proxmox_vm_ignore_existing_volumes` | `false` | Create a missing VM over its VMID's old disks or backups |
| `proxmox_vm_node` | `""` | Node name; empty means the host's short hostname |

A VM:

```yaml
- vmid: 300
  name: homeserver-cp-1
  cores: 2
  memory_mib: 4096
  machine: q35                        # default
  bios: ovmf                          # default; needs efidisk_storage (efitype 4m, no pre-enrolled keys)
  efidisk_storage: local-lvm
  cpu: host                           # default
  scsihw: virtio-scsi-pci             # default; virtio-scsi-single is accepted too
  disks:                              # created once; grown, never moved, shrunk or detached
    - {slot: scsi0, storage: local-lvm, size_gib: 32, serial: talos-system}
    - {slot: scsi1, storage: nas-vm, size_gib: 200, format: qcow2, serial: talos-data}
    - {slot: scsi2, storage: local-lvm, size_gib: 500, serial: talos-media, backup: false}
  net: {bridge: vmbr0, mac: "<mac>"}  # a MAC; optional: without one Proxmox picks one
  iso: local:iso/talos-v1.14.2-ce4c9805-metal-amd64.iso
  boot: "order=scsi0;ide2"
  watchdog: {model: i6300esb, action: reset}
  onboot: true                        # default
  startup: "order=1,up=60,down=120"
  tags: [talos, k8s]
  hostpci: []                         # [{mapping: igpu, pcie: true}]: a PCI resource mapping
  start: true                         # default
```

Every disk is created with `discard=on,ssd=1`. A `serial` is passed to the guest, but on virtio-scsi Proxmox also
sets `device_id=drive-<slot>`, which names the disk's `/dev/disk/by-id/` link (`scsi-0QEMU_QEMU_HARDDISK_drive-scsiN`):
a guest finds a disk by its slot, so keep each disk in its slot.

## Examples

This repository's configuration, `group_vars/proxmox/vms.yml`, maps the Kubernetes nodes onto VMs.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `make docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
