# Home Assistant OS on Proxmox

Home Assistant runs as a VM of its own, Home Assistant OS (HAOS), not in the cluster: home automation must keep
working while the cluster is down, and HAOS manages its own updates, add-ons and backups. It is made by hand, once,
following Home Assistant's Proxmox instructions; no role manages it. Commands run on the Proxmox host as root.

## Make the VM

1. Download the current HAOS image for KVM (`haos_ova-<version>.qcow2.xz`) from
   https://github.com/home-assistant/operating-system/releases and unpack it on the host: `xz -d haos_ova-*.qcow2.xz`.
2. Create the VM, VMID 310, 2 vCPU, 4 GiB, UEFI without pre-enrolled keys:

   ```bash
   qm create 310 --name homeassistant --ostype l26 --machine q35 --bios ovmf \
     --efidisk0 <storage>:1,efitype=4m,pre-enrolled-keys=0 --cores 2 --memory 4096 --balloon 0 \
     --scsihw virtio-scsi-pci --net0 virtio,bridge=vmbr0 --agent enabled=1 --onboot 1 --startup order=3,up=60
   qm disk import 310 haos_ova-<version>.qcow2 <storage>
   qm set 310 --scsi0 <storage>:vm-310-disk-1,discard=on,ssd=1 --boot order=scsi0   # the volume the import names
   qm disk resize 310 scsi0 32G
   ```

   `<storage>` is `homeserver-persistent` with a NAS, `local-lvm` without one. On `homeserver-persistent` the
   imported volume is `310/vm-310-disk-1.qcow2`, and the import must ask for qcow2 (`--format qcow2`).
3. Pass the Zigbee or Z-Wave USB stick through by vendor and product, which survives a change of port:
   `lsusb` on the host shows them, then `qm set 310 --usb0 host=<vendor>:<product>`.
4. In the router, reserve an address for the VM's MAC (`qm config 310 | grep net0`) with the hostname
   `homeassistant`, so it is reachable as `homeassistant.<the router's local domain>`. Never under the cluster's
   wildcard: a `*.<domain>` record answers every name below it with Traefik's address, so the name would reach Traefik
   and get a 404. Home Assistant serves on its own port, 8123, never through the cluster.
5. `qm start 310`, then open `http://homeassistant.<local domain>:8123` and restore a backup or onboard.

## Backups

The nightly VM backup covers it: add its VMID to `local.yml`,

```yaml
vm_backup_extra_vmids: [310]
```

then `make proxmox`. The `vm-nightly` job backs it up with the cluster's VMs, in both setups and whether or not a
cluster exists. Home Assistant's own backups land on its disk, so they are inside that backup too. Restoring:
`scripts/runbooks/vm-restore.md`.

## Homepage

Homepage's Home Assistant tile links to `haos_url` from `local.yml`, when it is set: a URL on the VM's local name,
such as `http://homeassistant.<local domain>:8123`, never tracked. Without it the tile links nowhere.

## TLS

When the cluster gets TLS, Home Assistant keeps its own certificate (its Let's Encrypt or NGINX add-on), since it
is not behind Traefik.
