# status

Reads the homeserver's machines without changing anything, and reports what runs and what needs doing: what
`make status` runs. With only its defaults it reads nothing.

Two entry points:

- **main** reads the host the play runs on, as `status_source` names it, and leaves what it found in the fact
  `status_found`. On the Proxmox host (`proxmox`) that is the host itself (version, kernel, uptime, CPU over the last
  day, memory, root filesystem), each of `status_vms` (state, uptime, CPU over the last day, memory, settings waiting
  for a restart, and, where its guest agent reports them, its filesystems and OS), every other VM counted in one
  line, each storage's use, the disk budget of `status_budget_storages`, the upgrades apt would make and whether an
  upgrade asked for a reboot. A host that does not answer is recorded as such, not a failure.
- **report** (`tasks_from: report`) puts together what each of `status_hosts` found, ranks what needs attention
  (sources not read, problems, things to do, each with the command that deals with it), writes the report to
  `status_report_file` as JSON, mode 0600, and shows it as text unless `status_quiet`. It writes in a dry run too,
  since `make status` runs as one and takes its exit code from the file: 2 when a source could not be read, else 1
  for a problem, else 0 (make itself then exits 0 or 2, as for any recipe).

Every command goes through the role's module, `status_read`, which runs a list of them at once and refuses the
whole task if any is not on its read list: exact forms of `pvesh get`, `qm guest cmd` (`get-fsinfo`, `get-osinfo`,
`network-get-interfaces`), `apt-get -s dist-upgrade`, `hostname -s` and `test -e`, none of which writes or returns a
secret. On a Proxmox host each `pvesh` or `qm` call starts Proxmox's API afresh, about a second of CPU, so they run
in parallel: on a four-core host the whole set takes about eight seconds, against twenty-five one after another.

Memory is Proxmox's view, the host's: with ballooning off, as on every VM here, it counts every page the guest has
touched, and the text marks it. A Talos node's guest agent lists no filesystem, so its disks come from elsewhere.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `status_source` | `""` | What the host is: `proxmox`; empty reads nothing |
| `status_types` | `[]` | The report's sections to read and show (`vms`, `storage`); empty means every one |
| `status_vms` | `[]` | This setup's VMs, each `{vmid, name, made_by, start, guest_agent}`; every other VM is only counted |
| `status_setup` | `{}` | What the setup has, `{nas, cluster, home_assistant}`, for the report's first line |
| `status_proxmox_node` | `""` | The Proxmox node's name; empty takes the host's short hostname |
| `status_budget_storages` | `[]` | Storages whose disk budget the report shows |
| `status_storage_headroom_gib` | `0` | GiB of a ZFS, directory or BTRFS storage kept out of its budget for the host |
| `status_reboot_required_file` | `""` | The file that says the host waits for a reboot; empty does not look |
| `status_parallel` | `8` | How many reads run at once on a host |
| `status_timeout` | `30` | Seconds each read may take |
| `status_hosts` | `[]` | report: the hosts whose findings it puts together |
| `status_report_file` | `""` | report: where it writes the report as JSON; empty writes none |
| `status_color` | `false` | report: whether the text is in colour |
| `status_quiet` | `false` | report: whether it shows no text |

This repository's `status.yml` reads the Proxmox host and then reports, with `group_vars/all/status.yml` (the VMs
this repo makes, the same the nightly backup covers) and `group_vars/proxmox/status.yml` (the installer's storage,
its headroom, Debian's reboot flag). `make status` runs it as a dry run with the stdout callback
`callback_plugins/homeserver_status.py`, which prints one line on what was read, then the report.

## Example

```yaml
- name: Read the Proxmox host
  hosts: proxmox
  roles:
    - role: status
      vars:
        status_source: proxmox
        status_vms:
          - {vmid: 310, name: homeassistant, start: qm start 310, guest_agent: true}
        status_budget_storages: [local-lvm]

- name: Report
  hosts: localhost
  tasks:
    - ansible.builtin.import_role:
        name: status
        tasks_from: report
      vars:
        status_hosts: "{{ groups['proxmox'] }}"
        status_report_file: /tmp/status.json
```

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `make docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
