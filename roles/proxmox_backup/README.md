# proxmox_backup

Keeps a Proxmox VE node's scheduled vzdump backup jobs as configured, with `pvesh` over SSH under
`/cluster/backup`: creates each job, and corrects the settings it manages of a job that exists. It never
deletes a job and never touches one it is not given. With only its defaults it changes nothing.

Before anything runs it refuses a job whose storage does not exist, is disabled or does not hold backups. A
storage of type `dir` or `btrfs` must also be a checked mount point (`is_mountpoint`, which `no`, `off`, `0` and
`false` all leave unset): without it, a USB disk that is unplugged leaves the storage looking available, and
vzdump writes into the empty mount-point directory on the node's root filesystem until it is full, which takes
the cluster filesystem and every VM down. The refusal names the fix, `pvesm set <id> --is_mountpoint yes
--create-base-path 0`.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `proxmox_backup_jobs` | `[]` | Jobs: `id`, `schedule`, `storage`, `vmid` (a list); optional `mode` (`snapshot`), `compress` (`zstd`), `prune_backups`, `notes_template`, `enabled` (`true`) |
| `proxmox_backup_pending_storages` | `[]` | Storage ids an earlier role adds on a real run: a dry run on a fresh host reports a job's storage from this list as pending instead of refusing it |

The settings compared are the schedule, storage, VMIDs (in any order), mode, compression, prune settings,
notes template and enabled flag. An optional one left out is not managed.

`notes_template` usually holds Proxmox's own `{{guestname}}` placeholder, which Ansible would try to fill in.
Inside a Jinja expression it is a string literal (`'notes_template': '{{guestname}}'`) and safe; written as a
plain value in a vars file, it must be `!unsafe '{{guestname}}'`.

## Example

```yaml
# group_vars/proxmox.yml
proxmox_backup_jobs:
  - id: vm-nightly
    schedule: "02:00"
    storage: backup-nfs
    vmid: [300, 301]
    prune_backups: keep-daily=7,keep-weekly=4
    notes_template: !unsafe '{{guestname}}'
```

Restoring is by hand: `scripts/runbooks/vm-restore.md` in this repository.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
