# proxmox_backup

Keeps a Proxmox VE node's scheduled vzdump backup jobs as configured, with `pvesh` over SSH under
`/cluster/backup`: creates each job, and corrects the settings it manages of a job that exists. It never
deletes a job and never touches one it is not given. With only its defaults it changes nothing. Backup jobs are
the cluster's, which neither `pvesm` nor `qm` manage, and `community.proxmox` has no module for them.

Before anything runs it refuses a job whose storage does not exist, is disabled or does not hold backups. A
storage of type `dir` or `btrfs` must also be a checked mount point (`is_mountpoint`, which `no`, `off`, `0` and
`false` all leave unset): without it, a USB disk that is unplugged leaves the storage looking available, and
vzdump writes into the empty mount-point directory on the node's root filesystem until it is full, which takes
the cluster filesystem (pmxcfs) and every VM down. The refusal names the fix, `pvesm set <id> --is_mountpoint yes
--create-base-path 0`.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `proxmox_backup_jobs` | `[]` | Jobs: `id`, `schedule`, `storage`, `vmid` (a list); optional `mode` (`snapshot`), `compress` (`zstd`), `prune_backups`, `notes_template`, `enabled` (`true`) |
| `proxmox_backup_pending_storages` | `[]` | Storage ids an earlier role adds on a real run: a dry run on a fresh host reports a job's storage from this list as pending instead of refusing it |

The settings compared are the schedule, storage, VMIDs (in any order), mode, compression, prune settings,
notes template and enabled flag. An optional one left out is not managed. `pvesh` may give the prune settings as
text or as an object, and the enabled flag as a number or not at all, so the role reads each form.

Commands do not run in check mode, so a dry run shows the plan: `create`, `ok` or the settings to correct, per job,
and a storage an earlier role adds only on a real run. That task counts as a change in a dry run when there is
something to do, so the recap says so; a real run counts the commands instead.

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

Restoring is by hand: `scripts/runbooks/vm-restore.md` in this repository, which also has the drill that proves a
backup restores without touching the running cluster (restored to a scratch VMID with `--unique`, its link down).

## In this repository

`proxmox.yml` runs the role over root SSH, after `roles/proxmox_storage` and before `roles/proxmox_node_exporter`.
`roles/proxmox_storage` adds the NAS's backup storage only on a real run, so `group_vars/proxmox/backup.yml` sets
`proxmox_backup_pending_storages` to every id in `proxmox_storage_nfs`: a dry run on a fresh host passes with the
storage pending, and a real run that finds it still missing is refused.

`group_vars/proxmox/backup.yml` holds the nightly backup job, owned by no single project: one job, `vm-nightly` at
02:00, `snapshot` mode, `zstd`, kept seven days and four weeks, its notes each VM's name. The VMs it covers
(`vm_backup_vmids`) are the cluster's nodes when there is a cluster, Home Assistant's when there is one, and any VM
`local.yml` names in `vm_backup_extra_vmids`, so there is a job with or without a cluster. Where it writes
(`vm_backup_target`) is the NAS's `homeserver-backup` with a NAS (a dataset on the persistent pool, which
`roles/proxmox_storage` mounts as a storage that holds backups only), and without one the storage `local.yml`
names in `vm_backup_storage`, such as a USB disk set up as a checked mount point or a Proxmox Backup Server; with a
NAS, `nas_section_error` refuses `vm_backup_storage`. The notes template, Proxmox's own `{{guestname}}`, is a
string literal inside that file's expression, so Ansible never templates it.

Without a NAS and without `vm_backup_storage` there is no job, and a post-task of `proxmox.yml`, tagged `always`,
says which VMs go unbacked and records it for NEXT STEPS (`backup`, `target`). `just status` checks the host's job
against this one (`status_backup_jobs` in `group_vars/proxmox/status.yml` is `proxmox_backup_jobs`) and reports on
the same VMs (`status_vms`, `group_vars/all/status.yml`); `tests/policy/test_roles.py` holds both so.

The tests are marked `cluster` and run from a test playbook of their own, `tests/integration/proxmox_backup.yml`,
not from `proxmox.yml`, which the other tests run many times; the addresses come from the test as extra vars.
`test_proxmox_backup_with_a_nas` and `test_proxmox_backup_without_a_nas` in `tests/integration/test_idempotence.py`
cover a dry run on a fresh host, the refusals, drift, VMIDs in another order, Home Assistant's VM and the extra VMs,
and `test_a_built_cluster_cannot_switch_setups` runs it too. The fake's job shape (`fake_pve.py`'s
`pvesh get|create /cluster/backup` and `pvesh get|set /cluster/backup/<id>`) comes from Proxmox VE 9's API schema,
checked against a host's since 2026-10-21: the prune settings come back as an object and the enabled flag as a
number, and only the listing carries `next-run`. Ids follow Proxmox's `pve-configid` rule, and an option the schema
lacks fails as it does there.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
