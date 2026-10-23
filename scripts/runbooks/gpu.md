# The iGPU for Jellyfin and Frigate

The host's Intel iGPU can go whole to the worker, in either setup, where Jellyfin transcodes and Frigate detects on
it. Whether a host has one is per hardware, so it is `local.yml`'s `kubernetes_gpu_mapping`, never a tracked
setting. With it set:

- `make deploy TYPE=kubernetes` gives that node's VM the PCI device (`hostpci0`), the `gpu` schematic, which adds
  Intel's i915 driver, and the label `homeserver.internal/gpu: intel`;
- Flux runs Intel's GPU plugin (`infra-gpu`) on the labelled node alone; it offers `gpu.intel.com/i915` to two
  pods at a time;
- the cluster settings ask for the device: `GPU_I915` `1`, and `FRIGATE_DETECTOR_DEVICE` `GPU`.

Without it, the same manifests ask for no device (a limit of `0`) and both apps run on the CPU.

The host gives the device up entirely, so its own screen goes dark once the initramfs has run: from then on the
network is the only way in. The host setup below is done by hand, once, and no role manages it; after a reinstall
of the host it is done again (`REBUILD.md`). Commands on the Proxmox host run as root.

## 1. The host

1. **A second way in, first.** The management port can come up without a link at boot (`TODO.md`: the host's
   main link fails its cable check at some boots), and with the console gone that leaves no way in. Give a second
   port, such as the onboard one, a fallback address on the LAN, reserved in the router and outside the DHCP
   range, with no gateway (the bridge keeps it). In `/etc/network/interfaces`:

   ```text
   auto <second port>
   iface <second port> inet static
       address <fallback address>/<prefix>
   ```

   Then `ifreload -a`, plug the port in, and check from the workstation that the host answers there with the main
   link unplugged: `ssh root@<fallback address> true`. Write the address in `docs/MACHINES.md`.
   If the host is ever unreachable on both, recover the console at the boot menu: edit the kernel command line
   (`e`) and add `modprobe.blacklist=vfio_pci`, which leaves the iGPU to the host's own driver for that boot.
2. **VT-d.** Enable it in the firmware if it is off, which means rebooting the host with every VM down. Proxmox
   VE's kernel turns Intel's IOMMU on by itself once the firmware has it. Check:

   ```bash
   dmesg | grep -e DMAR -e IOMMU                                    # IOMMU, DMA remapping or interrupt remapping enabled
   find /sys/kernel/iommu_groups/ -type l | grep 0000:00:02.0       # the iGPU's group
   ls /sys/kernel/iommu_groups/<group>/devices                      # the iGPU alone in it
   ```

   Record the result in `docs/MACHINES.md`.
3. **Keep the host's driver off it.** `lspci -nnk -s 00:02.0` shows the iGPU's `8086:<device>` id and the driver
   holding it. Have `vfio-pci` claim it at boot instead of i915:

   ```bash
   printf 'options vfio-pci ids=8086:<device>\nsoftdep i915 pre: vfio-pci\n' > /etc/modprobe.d/vfio-igpu.conf
   update-initramfs -u -k all
   ```

   Reboot the host, every VM down, and check that `lspci -nnk -s 00:02.0` now says `Kernel driver in use:
   vfio-pci`.
4. **The PCI resource mapping**, which `kubernetes_gpu_mapping` names: in the web UI, Datacenter → Resource
   Mappings → PCI Devices → Add, id `igpu`, the iGPU at `0000:00:02.0` (the UI fills in its ids and IOMMU group);
   or

   ```bash
   pvesh create /cluster/mapping/pci --id igpu --map node=<node>,path=0000:00:02.0,id=8086:<device>
   ```

## 2. The cluster

1. In `local.yml`:

   ```yaml
   kubernetes_gpu_mapping: igpu
   ```

2. `make check TYPE=kubernetes`, then `make deploy TYPE=kubernetes`. Expect the worker's VM to gain `hostpci0`, reported
   as waiting for a restart; its Talos node to report a pending upgrade to the `gpu` schematic; and the
   cluster-settings to change. From here until step 4 is done, Jellyfin and Frigate wait in Pending for a device
   nothing offers yet: expected.
3. **Restart the VM by hand.** Proxmox adds a PCI device only to a VM it starts, so a reboot from inside is not
   enough. Drain the worker first:

   ```bash
   kubectl drain homeserver-worker-1 --ignore-daemonsets --delete-emptydir-data
   qm shutdown 301 && qm start 301
   kubectl uncordon homeserver-worker-1                                           # once it is Ready
   ```

4. `make check TYPE=talos-upgrade`, then `make talos-upgrade`: the same Talos release with the new schematic, so the
   node reboots into i915. `make deploy TYPE=kubernetes` afterwards shows no change.

## 3. Checks

Worth doing once on the first host with a GPU, and once on a host without one, since neither has been seen to
work yet:

- **The driver and the device:** `talosctl -n <address> get extensions` lists `i915`, and
  `talosctl -n <address> ls /dev/dri` shows `card0` and `renderD128`.
- **The plugin:** `kubectl get node <node> -o jsonpath='{.status.allocatable.gpu\.intel\.com/i915}'` says `2`.
- **The device inside the pods:** `kubectl exec -n media deploy/jellyfin -- ls -ln /dev/dri` shows the render
  node, with its group and mode. LinuxServer.io's Jellyfin adds its user to the device's group as it starts, and
  Frigate runs as root. If Jellyfin's log says it cannot open the render node, add that group id under the pod's
  `securityContext.supplementalGroups`.
- **Without a GPU,** Jellyfin and Frigate run (`kubectl get pods -n media`, `kubectl get pods -n frigate`): a
  limit of `0` schedules on a node with no `gpu.intel.com/i915` at all.

## 4. The apps

- **Jellyfin:** Dashboard → Playback → Transcoding: hardware acceleration Intel QuickSync (QSV), and hardware
  decoding for the codecs the iGPU's generation decodes (Jellyfin's Intel guide lists them). Leave the low-power
  encoders off unless the node's i915 loads the GuC/HuC firmware they need.
- **Frigate:** its configuration was written once, at its first start, with the device of that time. If that was
  `CPU`, set `detectors.ov.device` to `GPU` in Frigate's configuration editor and restart it. To decode the
  cameras' streams on the iGPU as well, add `ffmpeg: {hwaccel_args: preset-vaapi}` (Frigate's hardware
  acceleration docs). Leave its GPU statistics off: `intel_gpu_top` needs `CAP_PERFMON`, which the `baseline` pod
  security level Talos enforces does not allow.

## Taking it away

Unset `kubernetes_gpu_mapping`, then `make deploy TYPE=kubernetes` (the VM loses `hostpci0`), restart the VM by hand as
above, and `make talos-upgrade` back to the base schematic. Set Frigate's detector device back to `CPU`. The mapping and
the `vfio-pci` binding on the host can stay.
