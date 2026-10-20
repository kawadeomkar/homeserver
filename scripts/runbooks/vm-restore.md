# Restoring a VM from its nightly backup

The `vm-nightly` job (`roles/proxmox_backup`, configured in `group_vars/proxmox/backup.yml`) backs up the
cluster's VMs every night at 02:00, with a NAS to `homeserver-backup` on the NAS's redundant pool, without one to
the storage `local.yml` names as `vm_backup_storage`. It keeps seven daily and four weekly backups, and also covers
any VM named in `vm_backup_extra_vmids`. A disk marked `backup=0`, such as a node's media disk, is never backed up.

Three procedures: the drill, which proves a backup restores without touching the running VM, a real restore
after losing the Proxmox host or its boot disk, and one VM restored on a host that is still running. Commands run
on the Proxmox host as root.

Find a VM's backups with `pvesm list <storage> --content backup --vmid <vmid>`; the newest is last. Each
archive's notes carry the VM's name.

## The drill

A restored VM is the same machine as the original: unless restored with `--unique`, it carries its MAC, and in any
case its hostname and whatever keys it holds. Booted with its network up, it would answer beside the original. So
the drill restores to a scratch VMID, with a new MAC, and never lets it on the network.

1. Restore it, not started, on storage with room:

   ```bash
   qmrestore <archive> 9301 --unique 1 --start 0 --storage <storage>
   ```

   With a NAS, use `homeserver-ephemeral`. Without one, check `pvesm status` first.
2. Before anything starts it, cut its network and keep it from starting at boot:

   ```bash
   qm set 9301 --net0 virtio,bridge=vmbr0,link_down=1 --onboot 0
   ```

3. Check that its configuration and disks came back: `qm config 9301`. Optionally start it and watch its
   console. **Never bring its link up.**
4. Remove it: `qm destroy 9301 --purge`.

## A real restore, after losing the Proxmox host or its boot disk

The run order after a reinstall is `make truenas`, `make proxmox`, then **this restore** (`REBUILD.md`).

1. **Get the backup storage back.** With a NAS, `make proxmox` adds `homeserver-backup` again. Without one,
   re-add the USB disk or Proxmox Backup Server by hand, under the same id as before (`vm_backup_storage`), and
   **before** `make proxmox`, which refuses a backup storage that does not exist. A USB disk goes back as a
   checked mount point:

   ```bash
   blkid                                                  # the disk's filesystem UUID and type
   mkdir -p /mnt/usb-backup
   echo 'UUID=<uuid> /mnt/usb-backup <type> defaults,nofail 0 2' >> /etc/fstab
   systemctl daemon-reload && mount /mnt/usb-backup
   pvesm add dir <id> --path /mnt/usb-backup --content backup --is_mountpoint yes --create-base-path 0
   ```

   A Proxmox Backup Server goes back with `pvesm add pbs <id> …` and its datastore, user and fingerprint.
2. **Restore each VM** from its newest archive, under its own VMID, then start it:

   ```bash
   qmrestore <newest archive of the VM> <vmid>
   qm start <vmid>
   ```

## One VM, on a host that is still running

While the VM is defined, `qmrestore <archive> <vmid>` refuses, and with `--force` it replaces the VM and every
disk it owns with the backup's copies. A disk marked `backup=0` is not in the backup and comes back unattached, as
`unusedN`. So park each disk worth keeping, one newer than the backup or never backed up, on a placeholder VM
first, where the restore cannot reach it, and put it back afterwards. For a VM with such a disk in `scsi1`:

```bash
qm stop <vmid>
qm create 9302 --name parked-disks                # holds the disks while the VM is restored
qm disk move <vmid> scsi1 --target-vmid 9302 --target-disk scsi1
qmrestore <newest archive of the VM> <vmid> --force
qm set <vmid> --delete scsi1                      # the backup's older copy, if it had one, now unused0
qm disk move 9302 scsi1 --target-vmid <vmid> --target-disk scsi1
qm disk unlink <vmid> --idlist unused0 --force 1  # only if the line above it left one
qm destroy 9302
qm start <vmid>
```

Not yet tried on a real host; try it on a test host first.
