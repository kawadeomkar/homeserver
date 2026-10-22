# status

Reads the homeserver's machines without changing anything, and reports what runs, where to reach it, and what needs
doing: what `make status` runs. With only its defaults it reads nothing.

Two entry points:

- **main** reads the host the play runs on, as `status_source` names it, and leaves what it found in the fact
  `status_found`.
  - On the Proxmox host (`proxmox`): the host itself (version, kernel, uptime, CPU over the last day, memory, root
    filesystem), each of `status_vms` (state, uptime, CPU over the last day, memory, settings waiting for a restart,
    and, where its guest agent reports them, its filesystems, OS and address), every other VM counted in one line,
    each storage's use, the disk budget of `status_budget_storages`, the upgrades apt would make and whether an
    upgrade asked for a reboot, and the backups: each of `status_backup_jobs` against the host's job of that id
    (there, enabled, writing to its storage, covering its VMs, and when it runs next), each VM's newest backup on its
    job's storage, and, from the logs of the newest runs that failed, which VMs failed and why. A host that does not
    answer is recorded as such, not a failure.
  - For the cluster (`cluster`), from this machine: the nodes (Ready, pressure, Talos and Kubernetes against
    `status_cluster_pins`, and from kubelet's statistics their memory and disks: `/var` and each Talos user volume of
    `status_cluster_volumes`), the apps, Flux (its version, the commit it applied against the one the branch points
    at now, every Kustomization and HelmRelease that is not ready), etcd's state, the LoadBalancers' addresses, the
    GPUs and who uses them, the Certificates, Traefik's allowed range, the credentials' expiry, Alertmanager's
    firing alerts, and the etcd snapshots in `status_cluster_snapshot_dir`, whose names say when each was taken,
    whether the cluster answers or not. The credentials are read under `no_log`, keeping only the server the kubeconfig names and when
    each client certificate expires; one naming another server than `status_cluster_server`, such as the home
    cluster's on a test host, is reported and never used. No Secret is ever read.
  - On the NAS (`nas`) it reads nothing itself: the play reads the NAS first, through roles/truenas's `status` entry
    point (`truenas_info` with its `status` option, one login), and the role works out what `status_nas_state` says:
    each pool's health, use, layout and last scrub, TrueNAS's alerts, each snapshot task's last run, the NFS service
    and its shares, and whether the NAS runs `status_nas_release`. A NAS that did not answer, refused the key or
    presented another certificate than the pinned one is reported, not a failure.
- **report** (`tasks_from: report`) puts together what each of `status_hosts` found, asks each app's addresses from
  this machine (unless `status_probe_apps` is off), ranks what needs attention (sources not read, problems, things to
  do, each with the command from `status_commands` that deals with it), writes the report to `status_report_file` as
  JSON, mode 0600, and shows it as text unless `status_quiet`. It writes in a dry run too, since `make status` runs
  as one and takes its exit code from the file: 2 when a source could not be read, else 1 for a problem, else 0 (make
  itself then exits 0 or 2, as for any recipe).

**An app** is each Ingress's backend Service, then the Deployment whose pods that Service selects, so a chart's app
such as Grafana is one too, and every other Deployment that a Flux Kustomization under `status_cluster_apps_path`
applies. A Deployment labelled `status_cluster_database_label` is listed under its app. Its state: running, starting,
waiting (for a Secret that does not exist yet), failing (a crash loop, an image that does not pull, a pod that cannot
be scheduled or stays pending, a rollout past its deadline), suspended (`make suspend` recorded its replicas in
`status_cluster_annotation`, and when), stopped (scaled to zero otherwise) or not applied (its Kustomization failed
before anything ran). Home Assistant, a VM of its own, is an app too, through `status_vms`' `app`.

Every command goes through the role's module `status_read`, which runs a list of them at once and refuses the whole
task if any is not on its read list: exact forms of `pvesh get`, `qm guest cmd` (`get-fsinfo`, `get-osinfo`,
`network-get-interfaces`), `apt-get -s dist-upgrade`, the backup jobs, the backup runs (`vzdump` tasks) and their
logs alone, a storage's backups, when a schedule fires next (`schedule-analyze`), `hostname -s`, `test -e`, `kubectl get` of kinds that hold no
secret (never `secrets`), `kubectl get --raw` of a node's kubelet statistics alone, `kubectl exec` of `amtool alert
query` in Alertmanager's container alone, `talosctl service etcd` (never `talosctl get`, which reads the machine
configuration) and `git ls-remote` over HTTPS or SSH. None of them writes or returns a secret. On a Proxmox host each
`pvesh` or `qm` call starts Proxmox's API afresh, about a second of CPU, so they run in parallel: on a four-core host
the whole set takes about eight seconds, against twenty-five one after another. The cluster's take about a second.

The report entry point's module `status_probe` looks each app's name up in DNS as this machine does and sends one
`GET` per URL to the address the Ingress says Traefik answers on, whatever DNS says, closing the connection once the
headers arrive. It checks no certificate and sends no credential, so it needs neither the cluster's CA nor a Secret;
it reads the served certificate's issuer and expiry, which the report matches with the Certificate objects. A URL's
`http` twin is listed too when it answers by itself rather than redirecting to `https` (Jellyfin's, for TV apps).
Without DNS for the apps, the text gives the `/etc/hosts` lines that reach them meanwhile.

**A VM's backup** is ok, failed (a run since its newest backup failed it, with the reason its log gives, or the
run's own error when it failed before any VM, as a storage that cannot be activated does), missed (its newest backup
is older than the run its job was due to make, worked out from the job's next run and the period between its next
two, which Proxmox's `schedule-analyze` gives, so no calendar format is read here; never while a run is going), none
yet, left out of its job, or not read. A failed or missed backup is a problem; a job that is not on the host, is
disabled, writes elsewhere or leaves a VM out, a VM with no backup yet, and no job at all with VMs to back up are
things to do.

Memory is Proxmox's view, the host's, for a VM that is no cluster node: with ballooning off, as on every VM here, it
counts every page the guest has touched, and the text marks it. A node's memory and disks are the guest's own, from
kubelet, since Talos's guest agent lists no filesystem.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `status_source` | `""` | What the host is: `proxmox`, `cluster`, `nas`; empty reads nothing |
| `status_types` | `[]` | The report's sections to show (`apps`, `admin`, `vms`, `storage`, `backups`, `cluster`, `nas`); empty means every one |
| `status_vms` | `[]` | This setup's VMs, each `{vmid, name, made_by, start, guest_agent, node, app}`; every other VM is only counted |
| `status_setup` | `{}` | What the setup has, `{nas, cluster, home_assistant}`, for the report's first line |
| `status_proxmox_node` | `""` | The Proxmox node's name; empty takes the host's short hostname |
| `status_budget_storages` | `[]` | Storages whose disk budget the report shows |
| `status_storage_headroom_gib` | `0` | GiB of a ZFS, directory or BTRFS storage kept out of its budget for the host |
| `status_reboot_required_file` | `""` | The file that says the host waits for a reboot; empty does not look |
| `status_backup_jobs` | `[]` | This setup's backup jobs, as roles/proxmox_backup takes them (`{id, schedule, storage, vmid}`), checked against the host's |
| `status_cluster_kubeconfig`, `status_cluster_talosconfig` | `""` | The credentials the cluster is read with |
| `status_cluster_server` | `""` | The API server the kubeconfig must name; empty checks nothing |
| `status_cluster_nodes` | `[]` | The nodes, each `{name, role}`, whose kubelet statistics are read |
| `status_cluster_apps_path` | `""` | The path under which Flux's Kustomizations apply the apps |
| `status_cluster_annotation` | `""` | The annotation in which `make suspend` records an app's replicas |
| `status_cluster_database_label` | `""` | The label (`key=value`) of an app's database |
| `status_cluster_volumes` | `{}` | The nodes' own volumes to show, `{mount path: name}` |
| `status_cluster_settings` | `{}` | The settings ConfigMap, `{namespace, name, allowlist}`; empty reads none |
| `status_cluster_alertmanager` | `{}` | Alertmanager's pod, `{namespace, pod}`; empty reads no alert |
| `status_cluster_flux_url`, `status_cluster_flux_ref` | `""` | The repository and branch Flux syncs |
| `status_cluster_pins` | `{}` | The versions pinned, `{talos, kubernetes}` |
| `status_cluster_request_timeout` | `10` | Seconds kubectl waits for the API server |
| `status_cluster_snapshot_dir` | `""` | The directory of the etcd snapshots, each `<anything>-<UTC time>.db`; empty shows none |
| `status_nas_state` | `{}` | What `truenas_info` returned with its `status` option, or its failure |
| `status_nas_release` | `""` | The TrueNAS release (major.minor) the NAS's configuration is written for; empty compares none |
| `status_parallel` | `8` | How many reads run at once on a host |
| `status_timeout` | `30` | Seconds each read may take |
| `status_hosts` | `[]` | report: the hosts whose findings it puts together |
| `status_report_file` | `""` | report: where it writes the report as JSON; empty writes none |
| `status_color` | `false` | report: whether the text is in colour |
| `status_quiet` | `false` | report: whether it shows no text |
| `status_probe_apps` | `true` | report: whether it asks each app's addresses from this machine |
| `status_probe_timeout` | `5` | report: seconds each lookup and request may take |
| `status_admin` | `[]` | report: the admin pages it lists, each `{name, url}` |
| `status_commands` | `{}` | report: the commands it suggests, by purpose (`upgrade`, `resume` with `{names}`, `talos_upgrade`, `credentials`, `allowlist`, `secrets`, `nas`, `nas_release`, `nas_network`, `backup`, `backup_target`, `etcd_snapshot`) |

This repository's `status.yml` reads the Proxmox host, the cluster and the NAS at once (`strategy: free`), then
reports, with `group_vars/all/status.yml` (the VMs this repo makes, the same the nightly backup covers; the admin
pages; the commands), `group_vars/proxmox/status.yml` (the installer's storage, its headroom, Debian's reboot flag,
the nightly backup job roles/proxmox_backup keeps), `group_vars/kubernetes/status.yml` (the credentials roles/talos
writes, the apps as roles/app_scale finds them, the worker's user volumes, the settings roles/flux_bootstrap keeps,
Alertmanager's pod, the pins, where roles/talos keeps the etcd snapshots) and `group_vars/nas/status.yml` (what
roles/truenas's `status` entry point read, and the release that role is written for). `make status` runs it as a dry
run with the stdout callback `callback_plugins/homeserver_status.py`, which prints one line on what was read and how
long the probes took, then the report.

## Example

```yaml
- name: Read the machines
  hosts: proxmox:kubernetes
  strategy: free
  roles:
    - role: status
      vars:
        status_vms:
          - {vmid: 310, name: homeassistant, start: qm start 310, guest_agent: true,
             app: {name: homeassistant, port: 8123}}
        status_budget_storages: [local-lvm]
        status_cluster_kubeconfig: /home/me/.talos/homeserver/kubeconfig
        status_cluster_apps_path: k8s/apps

- name: Report
  hosts: localhost
  tasks:
    - ansible.builtin.import_role:
        name: status
        tasks_from: report
      vars:
        status_hosts: "{{ groups['proxmox'] + groups['kubernetes'] }}"
        status_report_file: /tmp/status.json
```

(`status_source` is `proxmox` on the Proxmox host and `cluster` on the cluster's, set in each group's vars.)

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `make fix TYPE=docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
