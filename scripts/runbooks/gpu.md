# A GPU for Jellyfin and Frigate

The worker can have a GPU of the host's, whole, in either setup: Jellyfin transcodes and Frigate detects on it. Intel's
GPUs only, for now: the host's iGPU, or an Arc card in a PCIe slot. `make proxmox` and `make kubernetes` find it
themselves (`roles/proxmox_gpu`), as `local.yml`'s `kubernetes_gpu` says:

- **unset, or `auto`:** the one Intel GPU the host can pass alone that is not its console (the device the firmware
  shows the boot screen on) and that no other VM or container uses. A host whose only GPU is its iGPU passes nothing,
  since that is its console; one with two cards it could pass is refused until `kubernetes_gpu` names one.
- **a PCI address**, as `lspci` shows it, **in quotes** (`"0000:00:02.0"`, `"01:00.0"`): that GPU, the console
  included. Its own screen then goes dark once `vfio-pci` takes it, so set up a second way in first (step 1 below).
  Without the quotes YAML reads an address of digits as a number, which the runs refuse.
- **`none`:** no GPU.

A GPU another guest uses (a VM's `hostpci`, by address or through another mapping, or a container's `/dev/dri`) is
left alone by `auto` and refused by address: take it from that guest first.

What the runs do, every time: find the GPU in Proxmox's list of the host's PCI devices; refuse one whose IOMMU group
holds another device than the card's own (a VM would take that from the host too; an Arc card's audio, on a bus of its
own behind the card's switch, is the card's), or with the IOMMU off; keep the PCI resource mapping `homeserver-gpu`
naming its address, ids and IOMMU group, which Proxmox then checks; keep the host's own drivers off it and its audio
from its next boot (`/etc/modprobe.d/proxmox-gpu.conf`, `vfio-pci` by id, then the initramfs rebuilt, again on the
next run if a build fails); give the worker's VM `hostpci0` through the mapping; and give the worker the `gpu`
schematic (Intel's `i915` and `xe` drivers) and the label Intel's GPU plugin runs on. Jellyfin and Frigate ask for
the device once the worker offers it, under the name of the driver it binds, `gpu.intel.com/i915` (iGPUs before
Lunar Lake, Arc's A-series) or `gpu.intel.com/xe` (Arc's B-series and newer): until then, and without a GPU, the same
manifests run both apps on the CPU, never Pending.

What they never do is reboot anything, so the host's reboot, the worker's restart and the Talos upgrade are yours,
below. Commands on the Proxmox host run as root.

## 1. The host, once

1. **The IOMMU.** Turn on VT-d (an Intel CPU) or AMD-Vi (an AMD one) in the firmware if it is off, which means
   rebooting the host with every VM down; for an Arc card turn on Above 4G Decoding and Resizable BAR too. Proxmox
   VE's kernel turns the IOMMU on by itself once the firmware has it. Until then the runs say the GPU cannot be
   passed and why.
2. **A second way in, before passing the console.** Only when `kubernetes_gpu` names the host's console, its iGPU
   usually. The management port can come up without a link at boot (`TODO.md`: the host's main link fails its cable
   check at some boots), and with the console gone that leaves no way in. Give a second port, such as the onboard
   one, a fallback address on the LAN, reserved in the router and outside the DHCP range, with no gateway (the bridge
   keeps it). In `/etc/network/interfaces`:

   ```text
   auto <second port>
   iface <second port> inet static
       address <fallback address>/<prefix>
   ```

   Then `ifreload -a`, plug the port in, and check from the workstation that the host answers there with the main
   link unplugged: `ssh root@<fallback address> true`. Write the address in `docs/MACHINES.md`.
   If the host is ever unreachable on both, recover the console at the boot menu: edit the kernel command line
   (`e`) and add `modprobe.blacklist=vfio_pci`, which leaves the GPU to the host's own driver for that boot.

## 2. Passing it

1. `make check-proxmox`, then `make proxmox`. It names the GPU it found and why it left any other alone, creates the
   mapping, writes the binding, and says the host must reboot while a host driver still holds the GPU.
2. `make check-kubernetes`, then `make kubernetes`. Expect the worker's VM to gain `hostpci0`, reported as waiting
   for a restart, and its Talos node to report a pending upgrade to the `gpu` schematic. The apps carry on on the CPU.
3. **Reboot the host**, which also restarts the worker with the device: Proxmox adds a PCI device only to a VM it
   starts, so a reboot from inside the VM is not enough. Drain the worker first:

   ```bash
   kubectl drain homeserver-worker-1 --ignore-daemonsets --delete-emptydir-data
   ```

   then shut every VM down and reboot the host from its web UI or `reboot`, and once the worker is Ready,
   `kubectl uncordon homeserver-worker-1`. When no reboot is needed (`make proxmox` said nothing of one), restarting
   the worker alone is enough: `qm shutdown 301 && qm start 301`.
4. `make check-talos-upgrade`, then `make talos-upgrade`: the same Talos release with the new schematic, so the
   worker reboots into Intel's drivers, and Intel's GPU plugin offers the device.
5. `make kubernetes` once more: the worker offers the device now, so the cluster-settings change and Jellyfin and
   Frigate restart asking for it. `make proxmox` and `make kubernetes` afterwards show no change.

## 3. Checks

Worth doing once on the first host with a GPU, and once on a host without one, since neither has been seen to work
yet:

- **The host:** `lspci -nnk -s <address>` says `Kernel driver in use: vfio-pci`, and `make proxmox` no longer asks
  for a reboot.
- **The driver and the device:** `talosctl -n <address> get extensions` lists `i915` and `xe`, and
  `talosctl -n <address> ls /dev/dri` shows `card0` and `renderD128`.
- **The plugin:** `kubectl get node homeserver-worker-1 -o jsonpath='{.status.capacity}'` offers
  `gpu.intel.com/i915` or `gpu.intel.com/xe`, `2`: what step 5's settings follow.
- **The device inside the pods:** `kubectl exec -n media deploy/jellyfin -- ls -ln /dev/dri` shows the render
  node, with its group and mode. LinuxServer.io's Jellyfin adds its user to the device's group as it starts, and
  Frigate runs as root. If Jellyfin's log says it cannot open the render node, add that group id under the pod's
  `securityContext.supplementalGroups`.
- **Without a GPU,** Jellyfin and Frigate run (`kubectl get pods -n media`, `kubectl get pods -n frigate`): a
  limit of `0` schedules on a node with neither resource at all.

## 4. The apps

- **Jellyfin:** Dashboard → Playback → Transcoding: hardware acceleration Intel QuickSync (QSV), and hardware
  decoding for the codecs the GPU's generation decodes (Jellyfin's Intel guide lists them). Leave the low-power
  encoders off unless the node's driver loads the GuC/HuC firmware they need.
- **Frigate:** its configuration was written once, at its first start, with the device of that time. With an iGPU
  or an Arc A-series card, if that was `CPU`, set `detectors.ov.device` to `GPU` in Frigate's configuration editor
  and restart it. An Arc B-series card keeps detection on the CPU: Frigate 0.17's OpenVINO has no drivers for it,
  which Frigate 0.18 adds. To decode the cameras' streams on the GPU, add `ffmpeg: {hwaccel_args: preset-vaapi}`
  (Frigate's hardware acceleration docs). Leave its GPU statistics off: `intel_gpu_top` needs `CAP_PERFMON`, which
  the `baseline` pod security level Talos enforces does not allow.

## When the card comes out

Proxmox refuses to start a VM whose mapped device is gone, so after the host boots without the card the worker stays
off until the next `make kubernetes`, which drops `hostpci0` and starts it; the worker offers no device any more, so
the apps' settings go back to the CPU in the same run. It also removes the binding, and keeps the mapping for the
card's return. The worker can stay on the `gpu` schematic, whose drivers find nothing to bind; `make talos-upgrade`
moves it back to the base one.

Taking a GPU away on purpose is the same with `kubernetes_gpu: none`, except that the worker's VM, still running,
loses `hostpci0` at its next restart: restart it as in step 3, then `make kubernetes` again for the apps' settings. Set
Frigate's detector device back to `CPU`.

## Coming from `kubernetes_gpu_mapping`

The runbook before this one had the mapping and the `vfio-pci` binding made by hand, and `local.yml` name the mapping.
The runs refuse that setting now. Delete it from `local.yml` (and set `kubernetes_gpu` to the iGPU's address, in
quotes, to keep passing it), then on the Proxmox host remove what was made by hand, which the runs never touch:

```bash
rm /etc/modprobe.d/vfio-igpu.conf && update-initramfs -u -k all
pvesh delete /cluster/mapping/pci/igpu
```

## Other vendors

AMD's and NVIDIA's GPUs need other drivers in Talos (NVIDIA's cannot share a schematic: on a node without its card
they hold the boot), other device plugins, and other Frigate images, so `auto` leaves them alone and names them in
its output, and naming one in `kubernetes_gpu` is refused.
