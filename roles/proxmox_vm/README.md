# proxmox_vm

Creates VMs on a Proxmox VE node with `qm` over SSH, every option given, and on later runs keeps the settings it
manages as configured. Downloads the ISOs they boot from, and the images a disk is imported from, with `pvesh`.
With only its defaults it changes nothing.

The tasks read the node; the `proxmox_vm_plan` filter (`filter_plugins/proxmox_vm.py`, pure Python, unit-tested in
`tests/unit/test_proxmox_vm_plan.py`) works out what to do from what they read, and the tasks run the commands it
returns. Proxmox writes settings back in its own forms (tags lower-cased and sorted, a disk as a volume id with
its size, property strings in any order, numbers as text or as numbers), so every setting is compared normalised.

In this repository it runs on the Proxmox host over root SSH: in `kubernetes.yml`'s first play for the cluster's
VMs, and in `homeassistant.yml` for Home Assistant's (Examples, below); `suspend.yml` and `resume.yml` use its
`power` entry point.

## What it does, and never does

- **Creates** a missing VM with `qm create`, then starts it when `start` is true. A disk with `import_from` is
  imported from that volume (an image in a storage's import content, or a disk inside an OVA there:
  `local:import/<name>.ova/<disk>.vmdk`) at the image's own size (`<storage>:0,import-from=<volume>`: Proxmox's
  size 0 asks for it), then grown to `size_gib` in the same run; an image is downloaded only with its `sha256`,
  which Proxmox checks before it stores the file.
- **Corrects** cores, memory, CPU type, ballooning, start-on-boot, the startup order, the watchdog, the guest
  agent, tags, PCI devices (`hostpci`), USB devices (`usb`, which reach a running VM at once), the CD-ROM (`ide2`)
  and the boot order (`boot`, which Proxmox edits when a drive is deleted and never gives back the disk: a system
  disk replaced by hand would leave the VM booting its CD-ROM for good), in one `qm set`; sets a disk's `serial` or
  its `backup` flag again, on the same volume, when it differs; adds a configured disk the VM lacks, unless it is
  imported (that is the VM's own system, imported once, when the VM is created, and never again onto an existing
  VM: restore the VM instead); grows a disk configured larger (`qm disk resize`); starts a stopped VM marked
  `start`.
- **Reports**, never changes: every VM's MAC, for checking against the router's DHCP reservations; a MAC that
  differs from the configured one; every setting that waits for a restart (on a running VM, Proxmox applies
  cores, CPU, memory, ballooning, the watchdog, the agent, the boot order and PCI devices only at the next start),
  read from the VM's pending changes (`pvesh get .../pending`) on every run until that restart, and recorded for
  NEXT STEPS (`homeserver_next`, subject `restart`, command `qm reboot <vmid>`); ISOs and images named like the
  role's own that no VM on the node uses (an image is used only to create its VM, so the pinned one stays, for a
  rebuild).
- **Moves** a disk that is on another storage than configured, only when `proxmox_vm_move_disks` is set
  (below).
- **Never** shrinks or detaches a disk, deletes a VM, an ISO or an image, ejects the CD-ROM, or restarts a VM. The
  CD-ROM stays because a VM whose system disk is wiped can boot only from it (a Talos node reaches maintenance mode
  from it alone); the boot order puts the system disk first.
- **Refuses**, before changing anything:
  - a VMID that belongs to a VM of another name, or to a container;
  - a missing VM whose VMID still has disks or backups on any of the node's storages (files that belong to no
    VMID, such as imports and snippets, aside): it is being rebuilt, not built, and new empty disks beside the old
    ones would lose them. Restore it (`scripts/runbooks/vm-restore.md`), or set
    `proxmox_vm_ignore_existing_volumes` to build it new, which the refusal names as `proxmox_vm_rebuild_command`;
  - a missing VM while a storage is enabled but not active (an unplugged USB disk, an unreachable NAS), which could
    hold its old disks or backups unseen; `proxmox_vm_ignore_existing_volumes` lifts this too;
  - a disk storage that does not exist, is disabled or holds no images (in check mode, one listed in
    `proxmox_vm_pending_storages` is reported as pending instead, with `proxmox_vm_pending_storages_hint`, which a real
    run that finds one missing repeats);
  - a slot to fill with a new disk while the VM keeps an `unusedN` volume, which is what a restore leaves of a disk
    its backup excluded (`backup: false`): the slot's data may be on it, so the refusal says how to attach it again
    (`qm set <vmid> --<slot> <volume>`) or remove it (`qm disk unlink`) to get a new, empty disk;
  - **the disk budget**: on thin LVM, ZFS, a directory or BTRFS, the provisioned sizes of every disk on the storage, other VMs' and
    containers' included (Proxmox lists a container's volumes as `rootdir`, not `images`, so the role reads every
    content type), plus what this run adds, may not exceed the storage's size: a full storage gives every VM on it
    I/O errors. It is counted from what is provisioned, never from what is free, which shrinks as data is written. A
    thin pool's size is its own, so on thin LVM the answer is the same on every run. A ZFS storage's size is its
    dataset's use plus the pool's free space, which the host's own datasets share (on a ZFS install, the root
    filesystem): it shrinks as they grow. A directory or BTRFS storage's size is its filesystem's, which the host's
    own files share the same way. `proxmox_vm_host_headroom_gib` keeps that much of such a storage out of the
    budget, and a plan that passed can still be refused later. An NFS storage is not budgeted: cap it where it
    lives, such as with a quota on the NAS. `proxmox_vm_allow_overcommit` lifts it;
  - a disk configured smaller than it is, on another storage than it is (unless `proxmox_vm_move_disks` is set), or
    a disk slot the configuration no longer lists.
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
| `proxmox_vm_rebuild_command` | `""` | How to run the role again with that switch, which the refusal names: the play's just recipe, say |
| `proxmox_vm_move_disks` | `false` | Move a disk that is on another storage than configured, instead of refusing it |
| `proxmox_vm_move_command` | `""` | How to run the role again with that switch, which the refusal names |
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
  disks:                              # created once; grown, moved only when asked, never shrunk or detached
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
refuses, naming the `pvesm set` that enables them, before creating anything. In this repository that is Home
Assistant's VM without a NAS: the owner enables images on `local` once (`scripts/runbooks/haos.md`), since the role
never changes the installer's storages, and the role refuses until then.

Every disk is created with `discard=on,ssd=1`. A `serial` is passed to the guest, but on virtio-scsi Proxmox also
sets `device_id=drive-<slot>`, which names the disk's `/dev/disk/by-id/` link (`scsi-0QEMU_QEMU_HARDDISK_drive-scsiN`):
a guest finds a disk by its slot, so keep each disk in its slot.

## Moving a disk to another storage

A disk whose volume is on another storage than its `storage` says, because the configuration changed or someone
moved it by hand, is refused before anything changes, in a dry run too, naming `proxmox_vm_move_command`: a
configuration that names the wrong storage by mistake would otherwise move the disk, a large one for a long time.
Run with `proxmox_vm_move_disks: true` (`-e proxmox_vm_move_disks=true`, for that run alone) and it moves the disk
there with `qm disk move <vmid> <slot> <storage> --delete 1`, with `--format` when the disk has one (without it,
Proxmox keeps the disk's own format, raw onto NFS too). Proxmox mirrors a running VM's disk while the guest goes on
writing to it and switches the VM to the copy, or copies a stopped VM's, then deletes the old volume. The VM is
never stopped for it. The move comes after the run's `qm set`, whose disk options name the volume where it is, and
before any resize, which then grows the disk on its new storage. The disk keeps its slot and options, so the guest
sees the same device, and the disk budget counts it whole on its new storage (and still on its old one, which holds
it until the copy is done). A dry run lists `move <slot> to <storage>` under the VM. An EFI disk on another
storage than `efidisk_storage` is refused and moved the same way, after the VM's other disks, without `--format`:
Proxmox keeps its format where the storage takes it and uses the storage's own where not. Left behind on a storage
that goes, such as a NAS the setup no longer has, it would keep the VM from starting.

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

### The cluster's VMs

`kubernetes.yml`'s first play runs the role with `group_vars/proxmox/vms.yml`, after refusing NAS settings without
a NAS address (`nas_section_error`), an incomplete Kubernetes section (`kubernetes_settings_error`), and a cluster
whose inventory lacks the `kubernetes` group with its one host: `just init` copies `inventory.example` only when
there is no inventory, so one made before the cluster has no such group, and the VMs would be created while the play
that builds Talos on them matched no host. Without a cluster (`kubernetes_present` false) there is no VM and no ISO.

`vms.yml` maps `kubernetes_nodes` (`group_vars/all/kubernetes.yml`) onto VMs 300 (the control plane) and 301 (the
worker), the same two in both setups; only where their disks live depends on the NAS:

- **With a NAS** every disk is on it, in qcow2 (`kubernetes_system_storage` by the node's role, and
  `kubernetes_data_storage`): the control plane's system disk (`serial=talos-system`, which holds etcd) and the
  worker's data disk (`talos-data`, the apps' local volumes) on `homeserver-persistent`, the worker's system disk on
  `homeserver-ephemeral` (images, logs and scratch, all back with a reinstall: sync off, no snapshots).
- **Without one** every disk is on the installer's storage (`vm_storage_installer`), the worker's media disk
  (`talos-media`, `backup=0`, never backed up) among them.
- **The EFI disks** stay on the installer's storage in both setups, 4 MiB each. Until 2026-10-25 the system disks
  were there too with a NAS, which overfilled the home host's thin pool beside other projects' VMs.

Each disk sits in its slot of `kubernetes_disk_slots` (system `scsi0`, data `scsi1`, media `scsi2`), whose by-id
link Talos's disk selectors match; the serials only name the disks for whoever reads the VM's configuration. Every
node has the `i6300esb` watchdog (reset), `virtio-scsi-pci`, q35 with OVMF, the CPU type `host`, its MAC from
`local.yml`'s `kubernetes_node_macs` on `vmbr0`, the tags `talos` and `k8s`, start-on-boot with the control plane
first (startup order 1, the worker 2), and the boot order system disk, then CD-ROM. The CD-ROM always holds the
current Talos ISO, so a node whose system disk is wiped reaches maintenance mode from it.

The ISO is downloaded from the Image Factory to `kubernetes_iso_storage` (`local`, or `local-btrfs` on a BTRFS
install, which `local.yml` names). Its file name carries the Talos version and the schematic, so a new pin is a new
file; the old one stays until removed by hand, which the role's report suggests once no VM uses it. Its `sha256` is
the pin `kubernetes_iso_sha256`: the role requires one of every ISO, and the Image Factory publishes no checksum.

With `local.yml`'s `kubernetes_gpu_mapping`, the id of a Proxmox PCI resource mapping made by hand
(`scripts/runbooks/gpu.md`), the worker (`kubernetes_data_node`) gets `hostpci0` with that mapping (PCIe). On a
running worker it waits for a restart, which is by hand, as is the `just talos-upgrade` to the `gpu` schematic
(`roles/talos`).

The rest of `vms.yml`:

- `proxmox_vm_host_headroom_gib: 16`, for a ZFS or BTRFS install or a directory storage.
- `proxmox_vm_pending_storages`: the NFS storages `roles/proxmox_storage` adds in `proxmox.yml` (the ids of
  `proxmox_storage_nfs`), never this play, with the hint "just deploy proxmox adds it: run that first".
- `proxmox_vm_rebuild_command` and `proxmox_vm_move_command`: `just deploy kubernetes -e
  proxmox_vm_ignore_existing_volumes=true` and `just deploy kubernetes -e proxmox_vm_move_disks=true`.

The NAS's storages are not budgeted (NFS); the NAS's quota on `persistent/proxmox/vm`
(`vm_storage_persistent_quota_gib`) caps them.

A built cluster moved to the other setup is refused before anything changes, its disks being on the other storages,
until run with `proxmox_vm_move_disks`. The nightly backup covers both VMs in either setup, and the root README's
"Switching setups" gives the rest of the migration, by hand.

### Home Assistant's VM

`homeassistant.yml` runs this role, then `roles/haos_onboard`, on the Proxmox host, with play vars made from
`group_vars/proxmox/haos.yml`: `proxmox_vm_vms` is `haos_vms`, `proxmox_vm_isos` empty, `proxmox_vm_imports` the
pinned OVA (`haos_imports`: `haos_version` with `haos_ova_sha256`), `proxmox_vm_pending_storages`
`haos_pending_storages`, and the rebuild and move commands name `just deploy homeassistant`. They are the names
`vms.yml` gives the cluster's VMs, so they are set for that play alone. The VM's layout is in
`roles/haos_onboard`'s README.

## Tests

`tests/unit/test_proxmox_vm_plan.py` covers the plan filter on its own. `tests/integration/test_idempotence.py` runs
`kubernetes.yml`'s first play (`tests/integration/kubernetes.yml`, with this repository's group_vars) against the
fake Proxmox host, `fake_pve.py` behind `fake_pve_bin/`: with and without a NAS, a dry run on a fresh host, drift,
refusals, a disk moved only when told (`test_proxmox_vm_moves_disks_only_when_told`), a built cluster that cannot
switch setups (`test_a_built_cluster_cannot_switch_setups`, which also checks that the backup keeps both VMs),
volumes left from an old VM, VMs restored after a host reinstall, and a failed download and start. Home Assistant's
VM is tested with `roles/haos_onboard`. These tests are marked `cluster`. The fake's `qm` and VM routes are written
from Proxmox VE 9's API schema and documentation, not yet from a capture on a host.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
