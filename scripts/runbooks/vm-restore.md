# Restoring a VM from its nightly backup

The `vm-nightly` job (`roles/proxmox_backup`, configured in `group_vars/proxmox/backup.yml`) backs up the
cluster's VMs and Home Assistant's (VM 310) every night at 02:00, with a NAS to `homeserver-backup` on the NAS's
redundant pool, without one to the storage `local.yml` names as `vm_backup_storage`. It keeps seven daily and four
weekly backups, and also covers any VM named in `vm_backup_extra_vmids`. After a restore,
`just deploy homeassistant` starts Home Assistant's VM as `just deploy kubernetes` does the cluster's, and
leaves Home Assistant itself alone. A node's media disk carries `backup=0` and is never backed up.

Three procedures: the drill, which proves a backup restores without touching the running cluster, a real restore
after losing the Proxmox host or its boot disk, and one VM restored on a host that is still running. Commands run
on the Proxmox host as root.

Find a VM's backups with `pvesm list <storage> --content backup --vmid <vmid>`; the newest is last. Each
archive's notes carry the VM's name.

## The drill

A restored Talos VM is the same node as the original: unless restored with `--unique`, it carries the node's
MAC, and in any case its hostname, its certificates and the cluster's secrets. Booted with its network up, it
would join the cluster beside the original, and two kubelets would run the same pods against the same NAS
datasets. So the drill restores to a scratch VMID, with a new MAC, and never lets it on the network.

1. Restore it, not started, on storage with room:

   ```bash
   qmrestore <archive> 9301 --unique 1 --start 0 --storage <storage>
   ```

   With a NAS, use `homeserver-ephemeral`: the installer's storage has no room for a second system disk, since
   the provisioned disks there never add up to more than its size. Without a NAS, check `pvesm status` first.
2. Before anything starts it, cut its network and keep it from starting at boot:

   ```bash
   qm set 9301 --net0 virtio,bridge=vmbr0,link_down=1 --onboot 0
   ```

3. Check that its configuration and disks came back: `qm config 9301`. Optionally start it and watch the
   console until Talos reaches its configured state. **Never bring its link up.**
4. Remove it: `qm destroy 9301 --purge`.

## A real restore, after losing the Proxmox host or its boot disk

The run order after a reinstall is `just deploy truenas`, `just deploy proxmox`, **this restore**, then
`just deploy kubernetes` (`REBUILD.md`). Done out of order it fails safe: `proxmox_vm` refuses to create a VM whose
VMID still has disks or backups, naming them and this runbook, and `talos` refuses to bootstrap a cluster it has
bootstrapped before, rather than building an empty cluster over the old data.

1. **Get the backup storage back.** With a NAS, `just deploy proxmox` adds `homeserver-backup` again. Without one,
   re-add the USB disk or Proxmox Backup Server by hand, under the same id as before (`vm_backup_storage`), and
   **before** `just deploy proxmox`, which refuses a backup storage that does not exist. A USB disk goes back as a
   checked mount point:

   ```bash
   blkid                                                  # the disk's filesystem UUID and type
   mkdir -p /mnt/usb-backup
   echo 'UUID=<uuid> /mnt/usb-backup <type> defaults,nofail 0 2' >> /etc/fstab
   systemctl daemon-reload && mount /mnt/usb-backup
   pvesm add dir <id> --path /mnt/usb-backup --content backup --is_mountpoint yes --create-base-path 0
   ```

   A Proxmox Backup Server goes back with `pvesm add pbs <id> …` and its datastore, user and fingerprint.
2. **Restore the control plane** from its newest archive:

   ```bash
   qmrestore <newest archive of 300> 300
   ```

3. **Restore the worker** the same way, as VMID 301. Without a NAS its data disk comes back from the backup, as of
   02:00, and its media disk, never backed up, is lost: `just deploy kubernetes` adds it again, empty, and once the cluster
   is up, `kubectl -n local-path-storage delete job static-volume-directories` has Flux make the library's and the
   recordings' directories on it again (the restored Job ran before, on the old disk). With a NAS its data
   disk lives on `homeserver-persistent`, on the NAS, so it survived the host and is newer than the backup: after
   the restore, point `scsi1` back at the surviving volume, and delete the restored copy only once the apps check
   out:

   ```bash
   pvesm list homeserver-persistent --vmid 301           # the surviving data disk's volid
   qm set 301 --scsi1 <volid>,discard=on,ssd=1,serial=talos-data,format=qcow2
   ```

4. **Leave them stopped, and run `just check kubernetes`, then `just deploy kubernetes`.** Do not start them
   by hand: their CD-ROM names the Talos ISO in `local`, which was on the lost boot disk, and Proxmox refuses to start a
   VM one of whose drives is missing. `just deploy kubernetes` downloads the ISO again, corrects the CD-ROM to it,
   and starts the stopped VMs, which boot from their system disks. The dry run cannot reach the stopped nodes and
   says it would install them; the real run waits for them to answer, finds them configured with a running
   etcd, and neither installs nor bootstraps. `talosctl health` and `kubectl get nodes` confirm the cluster.
5. **A deliberate fresh build** over old backups, instead of a restore, needs
   `just deploy kubernetes -e proxmox_vm_ignore_existing_volumes=true`. Move the old backups aside first: the nightly
   job's pruning would delete them within one to
   four weeks.

## Without a backup

If no backup survived, the cluster is rebuilt from Git, a new cluster on purpose (`talos_allow_bootstrap`, since
the bootstrap marker in `~/.talos/homeserver` remembers the old one; it also lets the run install the new VMs, which
wait in maintenance mode):

```bash
just deploy kubernetes -e proxmox_vm_ignore_existing_volumes=true -e talos_allow_bootstrap=true
```

Flux recreates every app. With a NAS, its datasets (Nextcloud's and PhotoPrism's files, media, recordings) keep their
data; without one, nothing outside the VMs held any. The apps' local volumes, their databases and settings, start
empty.

**With a NAS, the worker's old data disk survived** on `homeserver-persistent`, holding those local volumes. The
rebuild gives VM 301 a new, empty data disk beside it, so swap the old one in before Talos formats the new one:
run the VMs' play alone first (`--limit homeserver`), then the rest.

```bash
just deploy kubernetes -e proxmox_vm_ignore_existing_volumes=true --limit homeserver
qm stop 301
pvesm list homeserver-persistent --vmid 301      # the old data disk, and the new one (the higher number)
qm set 301 --scsi1 <old volid>,discard=on,ssd=1,serial=talos-data,format=qcow2
qm disk unlink 301 --idlist unused0 --force 1    # the new, empty one
just deploy kubernetes -e talos_allow_bootstrap=true
```

Talos finds the old volume by its label and mounts it as it is. The new cluster's claims get new directories on
it, though (local-path names each after its volume), so move each app's old data into its new directory by
hand, with the app scaled to 0 meanwhile, from a debug pod in `kube-system` (which Pod Security exempts):
`kubectl -n kube-system debug node/homeserver-worker-1 -it --image=busybox --profile=sysadmin`, then under
`/host/var/mnt/local-path-provisioner/`. Not yet tried on a real host.

## One VM, on a host that is still running

Prefer the paths that keep the disks: a node whose system is broken gets a new system disk
(`scripts/runbooks/dead-worker.md`). Restore from a backup when its disks are lost or bad, or, for the control
plane, to take etcd back to last night (`dead-worker.md` says what that costs).

While the VM is defined, `qmrestore <archive> <vmid>` refuses, and with `--force` it replaces the VM and every
disk it owns, with the backup's copies: the worker's data disk too, on the NAS or not, which is newer than the
backup. A disk marked `backup=0` (the worker's media disk without a NAS) is not in the backup and comes back
unattached, as `unusedN`, while `just deploy kubernetes` would add a new, empty one in its place. So park each disk worth keeping on a placeholder VM first, where the restore cannot reach it, and put it
back afterwards. For the worker, with its data disk (`scsi1`):

```bash
qm stop 301
qm create 9302 --name parked-disks                # holds the disks while 301 is restored
qm disk move 301 scsi1 --target-vmid 9302 --target-disk scsi1
qmrestore <newest archive of 301> 301 --force
qm set 301 --delete scsi1                         # the backup's older copy, now unused0
qm disk move 9302 scsi1 --target-vmid 301 --target-disk scsi1
qm config 301 | grep scsi1                        # serial=talos-data still there? If not, qm set it back
qm disk unlink 301 --idlist unused0 --force 1
qm destroy 9302
```

Without a NAS, park `scsi2`, the media disk, the same way, always, and `scsi1` only if the data disk is good
(otherwise the backup's copy, from last night, is what comes back). The control plane, VM 300, has its system disk
alone: `qm stop 300`, then `qmrestore <newest archive of 300> 300 --force`. Then `just check kubernetes` and
`just deploy kubernetes`, which start the VM. Not yet tried on a real host; try it on a test host first.
