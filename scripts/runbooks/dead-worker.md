# A node that stays down

The cluster runs on one physical host, in both setups as a control plane (VM 300) and a worker (VM 301) that runs
every app. Nothing fails over: a node that stays down takes its apps with it until it is back. The two nodes fail
differently, so they have a section each; where the setups differ, the section says so. Run the
`qm` commands on the Proxmox host as root, and `talosctl` and `kubectl` on the workstation with
`TALOSCONFIG=~/.talos/homeserver/talosconfig` and `KUBECONFIG=~/.talos/homeserver/kubeconfig`.

## First, in both setups

1. Look at the VM: `qm status <vmid>`. A hung VM is normally reset by its watchdog within about two minutes, plus
   its boot.
2. If it is stopped, `qm start <vmid>`. If it hangs, `qm reset <vmid>`. Give it five minutes.

## The worker (VM 301) stays down

- **Leave its pods alone.** The `node.kubernetes.io/out-of-service` taint would delete them, but the only other node
  is the control plane, which takes no apps, and the apps on `local` volumes wait for their node anyway.
- **The node answers the Talos API but its system is broken**: wipe its system disk and reinstall it, keeping its
  data:

  ```bash
  talosctl reset -n <worker address> --graceful=false --wipe-mode system-disk --reboot
  make deploy TYPE=kubernetes ANSIBLE_ARGS='-e talos_reinstall=homeserver-worker-1'
  ```

  The VM boots the Talos ISO, which stays attached for this, into maintenance mode, and `make deploy TYPE=kubernetes`
  installs it again, since `talos_reinstall` names it: a bootstrapped cluster's keys go to no node in maintenance
  mode otherwise, so check first on the VM's console in Proxmox that it waits there at its address. The data
  volume, on its own disk, is untouched, and so is the media disk without a NAS. Without `--reboot` the reset powers the VM off, and `make deploy TYPE=kubernetes`
  starts it. **Never `--wipe-mode all`**: that wipes the user volumes, the apps' data.
  The node keeps its name, which the local volumes are pinned to.
- **The node does not answer at all**: `talosctl reset` needs the Talos API, so wipe the system disk from Proxmox
  instead:
  1. `qm stop 301`
  2. Detach the old system disk: `qm set 301 --delete scsi0`. It becomes `unused0`, and Proxmox drops it from the
     boot order, which is now the ISO alone.
  3. Give it a fresh one with the same size and serial, on the installer's storage (`local-lvm`, or
     `proxmox_local_vm_storage` from `local.yml` on a ZFS install; the size is `system_disk_gib` for the worker,
     64 by default), and put the boot order back, system disk first, or the node would boot the ISO after every
     install:

     ```bash
     qm set 301 --scsi0 local-lvm:64,discard=on,ssd=1,serial=talos-system --boot 'order=scsi0;ide2'
     ```

     `make deploy TYPE=kubernetes` corrects the boot order too, but on a running VM only from its next restart: Talos's
     own reboot after installing is not one.
  4. `qm start 301`. The new disk is blank, so it boots the ISO into maintenance mode; `make deploy TYPE=kubernetes
     ANSIBLE_ARGS='-e talos_reinstall=homeserver-worker-1'` installs it. The data disk is untouched.
  5. Once the node is back: `qm disk unlink 301 --idlist unused0 --force 1`.

  This path is not yet tried on a real host; try it on a test host first.
- **The VM's disks are lost**: restore it from its backup, `scripts/runbooks/vm-restore.md`.

## The control plane (VM 300) stays down

It holds etcd, the cluster's only member, in both setups. The worker's pods keep running without it, but nothing is
scheduled, Flux stops, and `kubectl` does not answer: use the Proxmox console and `talosctl`.

- **Never `--wipe-mode system-disk` or `all`.** The system disk holds etcd. The next run would then refuse to
  bootstrap again, since a new bootstrap makes a new, empty cluster; forced, Flux would recreate every volume
  claim, and the apps would start on new, empty directories while their data sat orphaned on the worker's data
  volume.
- **To recover, restore VM 300 from last night's backup** (`scripts/runbooks/vm-restore.md`, "One VM, on a host
  that is still running"): it has the system disk alone, so nothing needs parking. Its etcd comes back as of 02:00,
  and so does every Kubernetes object: a claim made since is gone, and Flux re-creates it on a new, empty
  directory while the data written since sits orphaned on the worker's data volume. Compare `kubectl get pvc -A`
  with what the apps hold afterwards.
- **If only etcd is lost**, restore it from a snapshot. The snapshot's age is the point every Kubernetes object
  goes back to: a claim made since is gone, and Flux re-creates it on a new, empty directory while the data
  written since sits orphaned on the worker's data volume. Snapshots come only from `make etcd-snapshot` and from
  the start of every `make talos-upgrade`, so take one before any risky change, or run `make etcd-snapshot` on a
  schedule from the workstation (cron or launchd). Talos recovers etcd only on a node whose etcd waits to be
  bootstrapped (`Preparing`), so wipe etcd's partition alone first:

  ```bash
  ls -t ~/.talos/homeserver/etcd/ | head -1                  # the newest snapshot, and its date
  talosctl reset -n <control plane address> --graceful=false --reboot --system-labels-to-wipe=EPHEMERAL
  talosctl service etcd -n <control plane address>           # wait for STATE Preparing
  talosctl bootstrap -n <control plane address> --recover-from="$HOME/.talos/homeserver/etcd/<that file>"
  ```

  Never any other `--system-labels-to-wipe` or `--wipe-mode`. `--recover-from` alone, on a node whose etcd is
  running or failing, is refused. This path is not yet tried on a real host; try it on a test host first.
