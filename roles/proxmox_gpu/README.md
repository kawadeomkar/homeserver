# proxmox_gpu

Finds the GPU a Proxmox VE host can pass whole to a VM, keeps the PCI resource mapping a VM is given it through, and
keeps the host's own drivers off it from boot. Over root SSH, with `pvesh`: no `community.proxmox` module manages PCI
mappings. With only its defaults it reads nothing and changes nothing. It never deletes a mapping, gives a VM the
device (that is `proxmox_vm`'s `hostpci`, naming the mapping) or reboots anything.

**Which GPU** (`proxmox_gpu_select`). The role reads Proxmox's list of the host's PCI devices
(`pvesh get /nodes/<node>/hardware/pci`, every class, so it sees whatever shares a device's IOMMU group) and, from
sysfs, whether the host has an IOMMU, which device the firmware made the console (`boot_vga`), and the driver holding
each device and its modalias. A GPU is a display controller (class `0x03`); it can be passed alone when the IOMMU is
on and its IOMMU group holds nothing but its own functions and PCI bridges, since Proxmox binds `vfio-pci` to every
other device of the group as the VM starts, taking it from the host. Then:

- `auto` takes the one GPU that can be passed alone, of a vendor in `proxmox_gpu_vendors`, that is not the console:
  passing the console blanks the host's own screen. None means no GPU, and the run says why each GPU was left alone;
  more than one is refused, naming them.
- An address (`0000:01:00.0`, `01:00.0`, or `01:00` for the slot) takes that GPU, the console included, with a warning.
  One no longer in the host means no GPU, so a VM that had it can be started again without it; one that is not a GPU,
  cannot be passed alone, or is of another vendor is refused.
- `none`, or empty, reads nothing.

The GPU chosen is left in `proxmox_gpu_selected` for later plays (`{}` for none): its address, the mapping's path
(the slot, for a card with an audio or USB function beside it, so the VM gets every function), its vendor and device
ids, subsystem, IOMMU group, whether it is the console, the drivers holding each function now, and `drivers`, the
kernel modules that claim it (`modprobe --resolve-alias` on its modalias), which says which driver a guest's kernel of
the same years binds, such as Intel's `i915` or `xe`.

**The mapping** (`proxmox_gpu_mapping`). Created, or its entry for this node set again, so it names the GPU as it is
now: its path, `id` (vendor:device), `iommugroup` and, when the host lists one, `subsystem-id`, each of function 0 for
a slot. These are what Proxmox checks as a VM using the mapping starts, refusing to start it when one differs ("PCI
device mapping invalid (hardware probably changed)"): a card moved, or another card added, which renumbers the IOMMU
groups, is followed by the next run. Other nodes' entries are sent back as they were, under the digest read. On a
real run the role then reads the mapping with `--check-node` and refuses one Proxmox finds invalid, rather than leave
it to the VM's start. With no GPU the mapping is kept as it is, for the card's return.

**The host's drivers** (`proxmox_gpu_vfio`). A modprobe.d file (`proxmox_gpu_modprobe_file`) has `vfio-pci` claim
each of the GPU's functions by id and subsystem, and load before every module that claims one (`softdep`), then the
initramfs is rebuilt (`update-initramfs -u -k all`; Proxmox's hook copies it to every boot partition
`proxmox-boot-tool` keeps), so from the next boot no host driver takes the GPU: not the console's framebuffer, not a
sound driver on its audio function. Proxmox also takes a device from its driver as a VM starts, which may work
without a reboot. While a host driver holds a function every run says that the host must reboot, every VM down first;
the role never reboots. Off, or with no GPU, the file is removed and the initramfs rebuilt, giving the GPU back to the
host from its next boot.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `proxmox_gpu_select` | `""` | `auto`, `none` (or empty), or the GPU's PCI address |
| `proxmox_gpu_select_name` | `proxmox_gpu_select` | What messages call the setting it comes from |
| `proxmox_gpu_vendors` | `[]` | PCI vendor ids `auto` may take and an address may name (`8086`); empty, any |
| `proxmox_gpu_mapping` | `""` | The PCI resource mapping kept for the GPU; empty, none |
| `proxmox_gpu_mapping_description` | `""` | Its description when the role creates it |
| `proxmox_gpu_vfio` | `false` | Keep the host's drivers off the GPU from its next boot |
| `proxmox_gpu_modprobe_file` | `/etc/modprobe.d/proxmox-gpu.conf` | The modprobe.d file that does it |
| `proxmox_gpu_sysfs` | `/sys` | Where the host's sysfs is |
| `proxmox_gpu_node` | `""` | Node name; empty means the host's short hostname |

## Entry points

- `main`: all of the above.
- `detect`: the choice alone, read-only, for a play that only needs `proxmox_gpu_selected`.

## Example

The one Intel GPU that is not the console, given to a VM:

```yaml
# group_vars/proxmox.yml
proxmox_gpu_select: auto
proxmox_gpu_vendors: ["8086"]
proxmox_gpu_mapping: gpu
proxmox_gpu_vfio: true
proxmox_vm_vms:
  - {vmid: 400, name: transcoder, ..., hostpci: "{{ [{'mapping': 'gpu', 'pcie': true}] if proxmox_gpu_selected else [] }}"}
```

In check mode the role reads the host, chooses, refuses what it would refuse, and shows the mapping's plan (`create`,
`set` or `ok`, with the entry) and the modprobe.d file's diff. `pvesh`'s changes and the initramfs run only for real.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `make docs` from the role itself: its
argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
