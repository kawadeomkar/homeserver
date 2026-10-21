# proxmox_vm

Creates VMs on a Proxmox VE node with `qm` over SSH, every option given, and on later runs keeps the settings it
manages as configured. Downloads the ISOs they boot from, and the images a disk is imported from, with `pvesh`.
With only its defaults it changes nothing.

The tasks read the node; the `proxmox_vm_plan` filter (`filter_plugins/proxmox_vm.py`, unit-tested in
`tests/unit/test_proxmox_vm_plan.py`) works out what to do from what they read, and the tasks run the commands it
returns. Proxmox writes settings back in its own forms (tags lower-cased and sorted, a disk as a volume id with
its size, property strings in any order, numbers as text), so every setting is compared normalised.

## What it does, and never does

- **Creates** a missing VM with `qm create`, then starts it when `start` is true. A disk with `import_from` is
  imported from that volume (an image in a storage's import content, or a disk inside an OVA there:
  `local:import/<name>.ova/<disk>.vmdk`) at the image's own size, then grown to `size_gib`; an image is downloaded
  only with its `sha256`, which Proxmox checks before it stores the file.
- **Corrects** cores, memory, CPU type, ballooning, start-on-boot, the startup order, the watchdog, the guest
  agent, tags, PCI devices (`hostpci`), USB devices (`usb`), the CD-ROM (`ide2`) and the boot order (`boot`, which
  Proxmox edits when a drive is deleted), in one `qm set`; adds a configured disk the VM lacks, unless it is
  imported (that is the VM's own system, imported once, when the VM is created: restore the VM instead); grows a
  disk configured larger (`qm disk resize`); starts a stopped VM marked `start`.
- **Reports**, never changes: every VM's MAC, for checking against the router's DHCP reservations; a MAC that
  differs from the configured one; every setting that waits for a restart (on a running VM, Proxmox applies
  cores, CPU, memory, ballooning, the watchdog, the agent, the boot order and PCI devices only at the next start);
  ISOs and images named like the role's own that no VM on the node uses (an image is used only to create its VM,
  so the pinned one stays, for a rebuild).
- **Never** moves, shrinks or detaches a disk, deletes a VM, an ISO or an image, ejects the CD-ROM, or restarts a
  VM.
- **Refuses**, before changing anything:
  - a VMID that belongs to a VM of another name, or to a container;
  - a missing VM whose VMID still has disks or backups on any of the node's storages (files that belong to no
    VMID, such as imports and snippets, aside): it is being rebuilt, not built, and new empty disks beside the old
    ones would lose them. Restore it (`scripts/runbooks/vm-restore.md`), or set
    `proxmox_vm_ignore_existing_volumes` to build it new;
  - a missing VM while a storage is enabled but not active (an unplugged USB disk, an unreachable NAS), which could
    hold its old disks or backups unseen; `proxmox_vm_ignore_existing_volumes` lifts this too;
  - a disk storage that does not exist, is disabled or holds no images (in check mode, one listed in
    `proxmox_vm_pending_storages` is reported as pending instead, with `proxmox_vm_pending_storages_hint`, which a real
    run that finds one missing repeats);
  - **the disk budget**: on thin LVM, ZFS, a directory or BTRFS, the provisioned sizes of every disk on the storage, other VMs' and
    containers' included, plus what this run adds, may not exceed the storage's size. It is counted from what is
    provisioned, never from what is free. A thin pool's size is its own, so on thin LVM the answer is the same on
    every run. A ZFS storage's size is its dataset's use plus the pool's free space, which the host's own datasets
    share (on a ZFS install, the root filesystem): it shrinks as they grow. A directory or BTRFS storage's size is its
    filesystem's, which the host's own files share the same way. `proxmox_vm_host_headroom_gib` keeps that much of
    such a storage out of the budget, and a plan that passed can still be refused later. An NFS storage is not
    budgeted: cap it where it lives, such as with a quota on the NAS. `proxmox_vm_allow_overcommit` lifts it;
  - a disk configured smaller than it is, on another storage than it is, or a disk slot the configuration no
    longer lists.
- **Warns** when the running VMs' memory plus the VMs it would start exceed the node's memory less 2 GiB.

A download's own task status is checked: `pvesh` can exit 0 for a download that failed.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `proxmox_vm_vms` | `[]` | VMs (below) |
| `proxmox_vm_isos` | `[]` | ISOs to download: `storage`, `filename`, `url`, and `sha256`, the file's SHA-256, which Proxmox checks before keeping the download |
| `proxmox_vm_imports` | `[]` | Images to download as import content: `storage`, `filename`, `url` and `sha256`, checked the same way |
| `proxmox_vm_pending_storages` | `[]` | Storage ids an earlier role adds on a real run; pending in check mode |
| `proxmox_vm_pending_storages_hint` | `""` | What adds them, such as the play to run first: shown in check mode and when one is missing |
| `proxmox_vm_allow_overcommit` | `false` | Let the disks on thin LVM, ZFS, a directory or BTRFS add up to more than it holds |
| `proxmox_vm_host_headroom_gib` | `0` | GiB of a ZFS, directory or BTRFS storage kept out of the disk budget, for the host's own files |
| `proxmox_vm_ignore_existing_volumes` | `false` | Create a missing VM over its VMID's old disks or backups |
| `proxmox_vm_rebuild_command` | `""` | How to run the role again with that switch, which the refusal names: the play's make target, say |
| `proxmox_vm_node` | `""` | Node name; empty means the host's short hostname |
| `proxmox_vm_power` | `""` | The power entry point: `stopped` or `running`; empty does nothing |
| `proxmox_vm_power_vmids` | `[]` | The power entry point's VMs |
| `proxmox_vm_power_timeout` | `180` | Seconds a VM's own shutdown may take before the run fails |

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
  usb: []                             # [{host: "10c4:ea60"}] (lsusb's vendor:product, or bus-port) or [{mapping: zigbee}]; usb3: true
  start: true                         # default
```

A disk imported from an image, here Home Assistant OS's OVA, downloaded through `proxmox_vm_imports`:

```yaml
  disks:
    - {slot: scsi0, storage: homeserver-persistent, size_gib: 32, format: qcow2,
       import_from: "local:import/haos_ova-18.3.ova/home-assistant.vmdk"}
  import_working_storage: homeserver-persistent   # qm create --import-working-storage
```

Proxmox extracts a disk out of an OVA before converting it, onto the VM's `import_working_storage` (a parameter of
`qm create`, shared by every import of the command) or else the OVA's own storage, which must hold images and have a
path (a directory or NFS storage). The installer's `local` holds no images unless they are enabled, so the role
refuses, naming the `pvesm set` that enables them, before creating anything.

Every disk is created with `discard=on,ssd=1`. A `serial` is passed to the guest, but on virtio-scsi Proxmox also
sets `device_id=drive-<slot>`, which names the disk's `/dev/disk/by-id/` link (`scsi-0QEMU_QEMU_HARDDISK_drive-scsiN`):
a guest finds a disk by its slot, so keep each disk in its slot.

## Shutting VMs down and starting them

The `power` entry point (`tasks_from: power`) does only this: with `proxmox_vm_power: stopped` it shuts each VM of
`proxmox_vm_power_vmids` down with `qm shutdown`, the guest's own shutdown through ACPI or its agent, never a forced
stop, and fails the run, the VM still running, when that takes more than `proxmox_vm_power_timeout` seconds; with
`running` it starts each with `qm start`. A VM already as asked is left alone, a VMID that does not exist is refused,
and a dry run says what it would do. The main entry point starts again a VM marked `start`, and the host one marked
`onboot` when it boots. This repository's `suspend.yml` and `resume.yml` use it for Home Assistant's VM.

```yaml
- ansible.builtin.import_role:
    name: proxmox_vm
    tasks_from: power
  vars:
    proxmox_vm_power: stopped
    proxmox_vm_power_vmids: [310]
```

## Examples

This repository's configuration, `group_vars/proxmox/vms.yml`, maps the Kubernetes nodes onto VMs, and
`group_vars/proxmox/haos.yml` the Home Assistant OS VM, which `homeassistant.yml` passes to the role as play vars.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `make fix TYPE=docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
