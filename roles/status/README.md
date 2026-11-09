# status

Reads the homeserver's machines without changing anything, and reports what runs, where to reach it, and what needs
doing: what `just status` runs. With only its defaults it reads nothing.

Two entry points:

- **main** reads the host the play runs on, as `status_source` names it, and leaves what it found in the fact
  `status_found`.
  - On the Proxmox host (`proxmox`): the host itself (version, kernel, uptime, CPU over the last day, memory, root
    filesystem), each of `status_vms` (state, uptime, CPU over the last day, memory, settings waiting for a restart,
    and, where its guest agent reports them, its filesystems, OS and address), every other VM counted in one line,
    each storage's use, the disk budget of `status_budget_storages`, the upgrades apt would make and whether an
    upgrade asked for a reboot, and the backups: each of `status_backup_jobs` against the host's job of that id
    (there, enabled, writing to its storage, covering its VMs, and when it runs next), each VM's newest backup on its
    job's storage, and, from the logs of the newest runs that failed, which VMs failed and why. It reads in two
    steps: the node's name (`hostname -s`, unless `status_proxmox_node` gives it), which the API's paths need, then
    everything else at once: `/cluster/resources` (the node, every VM and every storage in one call), the node's
    status and day history, each of `status_vms`' day history and pending settings, and for a VM whose guest agent
    reports them (Home Assistant's) its filesystems and OS, and its address when it serves an app without a URL of
    its own, past the Docker bridges and other inner interfaces Home Assistant OS has. Talos's guest agent lists no
    filesystem and no OS version (seen on the NUC). Every other VM on the host, another project's, is never named as
    a problem. A host that does not answer (every read has `ignore_unreachable`) is recorded with Ansible's message
    and reported, never a failure. Its Tailscale is read before, through roles/proxmox_tailscale's `status` entry
    point, when the setup turns it on, and passed in `status_tailscale_state`.
  - For the cluster (`cluster`), from this machine: the nodes (Ready, pressure, Talos and Kubernetes against
    `status_cluster_pins`, and from kubelet's statistics their memory and disks: `/var` and each Talos user volume of
    `status_cluster_volumes`), the apps, Flux (its version, the commit it applied against the one the branch points at
    now, every Kustomization and HelmRelease that is not ready), etcd's state, the LoadBalancers' addresses, the GPUs
    and who uses them, the Certificates, Traefik's allowed range, the credentials' expiry, Alertmanager's firing alerts,
    and the etcd snapshots in `status_cluster_snapshot_dir`, whose names say when each was taken, whether the cluster
    answers or not (a `fileglob` on this machine). The credentials are read first, under `no_log`, keeping only the
    server the kubeconfig names and when each client certificate expires (the filter `status_credentials`); one
    naming another server than `status_cluster_server`, such as the home cluster's on a test host, is reported as a
    source not read and never used. Then everything else at once. No Secret is ever read. A cluster that does not
    answer while the Proxmox host says its nodes' VMs are stopped is a note, not a source not read: the stopped VMs
    say why, and are a thing to do already.
  - On the NAS (`nas`) it reads nothing itself: the play reads the NAS first, through roles/truenas's `status` entry
    point (`roles/truenas/tasks/status.yml`: `truenas_info` with its `status` option, one login), and the role works
    out what `status_nas_state` says: each pool's health, use, layout and last scrub, TrueNAS's alerts, each snapshot
    task's last run, the NFS service and its shares, and whether the NAS runs `status_nas_release`. The `status`
    option adds `pool.scrub.query`, `alert.list` and `pool.snapshottask.query` to `truenas_info`'s reads and picks
    each pool's health, root-dataset use, layout and last scan, the alerts not dismissed and each snapshot task's
    last run, with the API's times (`{"$date": ms}`) turned into RFC 3339. The entry point tries each address once
    (`api_connect_wait: 0`) and never fails (`failed_when: false`): a NAS that did not answer, refused the key or
    presented another certificate than the pinned one is reported as a source not read, not a failure, and the key
    goes to no other server. `system.info`'s serial and the disks' serials never reach the report.
- **report** (`tasks_from: report`), on localhost, puts together what each of `status_hosts` found (`status_merge`,
  every host's probes together), ranks what needs attention (sources not read, problems, things to do, each with the
  command from `status_commands` that deals with it, so the role names none of this repository's recipes itself),
  writes the report to `status_report_file` as JSON, mode 0600 in a 0700 directory (it holds addresses), and shows it
  as text unless `status_quiet`: the sections, then what needs attention, after the sections it is worked out from,
  last, above the exit line, where a terminal leaves it in view. It writes in a dry run too (`check_mode: false`),
  since `just status` runs as one and takes its exit code from the file: 2 when a source could not be read, else 1
  for a problem, else 0.

**An app** is each Ingress's backend Service, then the Deployment whose pods that Service selects, so a chart's app
such as Grafana is one too, and every other Deployment that a Flux Kustomization under `status_cluster_apps_path`
applies. A Deployment labelled `status_cluster_database_label` (here `app.kubernetes.io/component: database`) is listed
under its app. Its state: running, starting, waiting (for a Secret that does not exist yet), failing (a crash loop, an
image that does not pull, a pod that cannot be scheduled or stays pending over ten minutes, a rollout past its
deadline), suspended (`just suspend` recorded its replicas in `status_cluster_annotation`, and when, which the
Deployment's managed fields say: it is read with `--show-managed-fields`), stopped (scaled to zero otherwise) or not
applied (its Kustomization failed before anything ran). An app a host leaves out (`kubernetes_apps`) is not listed,
since Flux deletes it. Home Assistant, a VM of its own, is an app too, through `status_vms`' `app`.

Each host reads only the sources the sections of `status_types` need (the filter's `section_sources`, and
`status_host_reads` for each host): the Proxmox host's own reads and its Tailscale, the cluster and its etcd snapshots,
the NAS. Shown the NAS alone, the role reads neither the Proxmox host nor the cluster, and a source no section shown
needs is never one not read. A host with nothing to read still sets `status_found`, empty, so that the report play
can tell it from one refused before its reads, which leaves none. Each host's play also asks from this machine the
addresses of the apps it found (unless `status_probe_apps` is off): the cluster's from the cluster's host, which is
this machine, Home Assistant's delegated to it from the Proxmox host's play, which first says its Tailscale and this
machine's view of the tailnet's DNS (`tailscale dns status --json`, delegated to localhost too) in the fact
`status_early`. Nothing is delegated anywhere but localhost.

Every command goes through the role's module `status_read`, which runs a list of them at once (`status_parallel`, 8
at a time, each within `status_timeout`, 30 s, which `group_vars/kubernetes/status.yml` lowers to 15 s for the
cluster, so that a cluster that does not answer does not hold the report up for long: kubectl gives up after its own
10 s, `status_cluster_request_timeout`) and refuses the whole task before running anything if any is not one of the
exact forms on its read list (`READS` in `library/status_read.py`, each argument matched whole). A tool's name alone
is never enough, since most tools also write or return secrets. The list: named `pvesh get` paths
(`/cluster/resources`, the node's status and day history, a VM's day history and pending settings, a storage's
content and its backups, the backup jobs, `schedule-analyze`, the `vzdump` runs and a `vzdump` run's log alone), `qm
guest cmd` with `get-fsinfo`, `get-osinfo` or `network-get-interfaces`, `apt-get -q -s dist-upgrade` (a simulation),
`hostname -s`, `test -e`; `kubectl get` of nodes, pods, services, ingresses, persistentvolumes, kustomizations,
helmreleases, fluxinstances, certificates and deployments (with `--show-managed-fields`), of one ConfigMap, and
`--raw` of a node's kubelet statistics alone, `kubectl exec` of `amtool alert query` in Alertmanager's container
alone; `talosctl service etcd`; `git ls-remote` over HTTPS or SSH, never `ext::`, which runs a command, or a local
path; and on this machine `tailscale dns status --json`. None of them writes or returns a secret: never `kubectl get
secrets`, never `talosctl get`, since the machine configuration holds every key of the cluster. Each command runs in
the C.UTF-8 locale, so no output it parses is translated, and git never asks for a password. Measured on a four-core
Proxmox host (2026-10-21): every `pvesh` or `qm` call takes 1.1 to 1.3 s, Proxmox's Perl API started afresh, so the
host's 15 reads take 25 s one after another and 8 s at once, and `apt-get -s` takes 0.7 s where the API's own update
list takes 4.5 s; the cluster's reads take about 0.1 s each and `git ls-remote` about 1 s, so the cluster is read in
about a second.

The module `status_probe` looks each app's name up in DNS as this machine does, compared with the address the Ingress
says Traefik answers on, and sends one `GET` per URL to that address, whatever DNS says, closing the connection once
the headers arrive. It checks no certificate and sends no credential, so it needs neither the cluster's CA, which
exists in the cluster only in Secrets that hold private keys, nor a Secret; it reads the served certificate's issuer
and expiry with `cryptography`, which the report matches with the Certificate objects. A URL's `http` twin is listed
too when it answers by itself rather than redirecting to `https` (Jellyfin's, for TV apps). Without DNS for the apps,
the text gives the `/etc/hosts` lines that reach them meanwhile.

**The Proxmox host's Tailscale**, when the setup turns it on: whether it is installed and `tailscaled` answers, the
backend state, the node's tailnet name, address, tags and key expiry, each route as configured, advertised and served
by this host, the guard's table as loaded and what it guards, IPv4 forwarding, the preferences, a Serve
configuration and Tailscale's health warnings, then how the apps answer from the tailnet: through a route this host
serves that holds Traefik's address, tailnet clients arriving as the host's own address (masqueraded; the admin page
marked `tailscale_node`), which Traefik's allowed range must let in, with the tailnet's split DNS for the apps' domain
as this machine's own Tailscale has it, when it runs one on the same tailnet. The routes are read from the host alone,
so a route another subnet router serves reads as not served. The admin pages gain a tailnet column: the node's own
page at its tailnet name, every other through the route, on the ports the guard allows. Not installed, not logged in,
preferences or a guard other than configured, no split DNS for the apps' domain, and what only the admin console can
fix (roles/proxmox_tailscale's own report, word for word, bar the health warnings, which are only shown) are things to
do; `tailscaled` not answering, Tailscale stopped, the guard not loaded and a router that does not forward are
problems. No other device, login or the tailnet's name is ever kept (a personal tailnet's name is its owner's login).
What `status_tailscale_state` holds comes from roles/proxmox_tailscale's read-only `status` entry point
(`roles/proxmox_tailscale/tasks/status.yml`), which `status.yml` runs on the Proxmox host before this role: its module
`tailscale_read` runs `tailscale status --json`, `get --json`, `serve status --json`, `nft list table inet
tailnet_guard` and `sysctl -n net.ipv4.ip_forward` at once under `no_log`, one round trip where five tasks took 1.1
to 1.4 s each on the home host; whether `tailscaled` runs is whether `tailscale status` answers, so no `systemctl`. It
keeps only the node's own entry and the admin-console report that role's `node.yml` prints, through the same filter
(`roles/proxmox_tailscale/filter_plugins/proxmox_tailscale.py`, `tests/unit/test_proxmox_tailscale_filters.py`). The
shapes were captured on the home host (Tailscale 1.104.2, nft 1.1.3) and on the workstation, a Mac (1.102.4), on
2026-10-22.

**A VM's backup** is ok, failed (a run since its newest backup failed it, with the reason its log gives, or the run's
own error when it failed before any VM, as a storage that cannot be activated does), missed (its newest backup is
older than the run its job was due to make, worked out from the job's next run and the period between its next two,
which Proxmox's `schedule-analyze` gives, so no calendar format is read here; never while a run is going), none yet,
left out of its job, or not read. A failed or missed backup is a problem; a job that is not on the host, is disabled,
writes elsewhere or leaves a VM out, a VM with no backup yet, and no job at all with VMs to back up are things to do
(with `status_commands`' `backup`, or `backup_target` for no job). The jobs are read from `/cluster/backup`, whose
listing alone carries each job's `next-run`; each VM's newest backup from its job's storage (`--content backup`); the
last 20 `vzdump` runs with `--source all`, so a running one too; and, in a second read, the logs of the newest three
that failed (a run with warnings made its backups), with `--limit 5000`, since a run's log defaults to 50 lines on
Proxmox. A log names each VM that failed in `ERROR: Backup of VM <id> failed - <why>`; a run that failed before any
VM started fails every VM it was to back up, with its own `TASK ERROR`. A backup may be up to two hours older than
the run due (or half the job's period, when that is shorter) before it is missed, since a daylight-saving change
between the next two runs moves the time worked out for the last one by an hour. These shapes were captured from the
home host's nightly job on 2026-10-21, the failure lines from vzdump's source. In this repository
`status_backup_jobs` is `proxmox_backup_jobs` (`group_vars/proxmox/status.yml`), so there is none without a NAS and
without `vm_backup_storage`, which the report says.

**The storage**: each storage the Proxmox host has, each Talos user volume the cluster's nodes report and each node's
`/var` is a thing to do at 80% full and a problem at 90%, and a storage that is not available is a problem. Each of
`status_budget_storages` (here the installer's storage) is shown against roles/proxmox_vm's disk budget: the disks
provisioned on it against its size, less `status_storage_headroom_gib` on ZFS, directory and BTRFS storages (here
`proxmox_vm_host_headroom_gib`), and provisioned beyond that is a thing to do.

**The NAS**: a pool not ONLINE or not healthy, 90% full, or whose last scrub found errors, a snapshot task in ERROR,
an alert at ERROR or worse, NFS not running while it has shares, and TrueNAS not ready are problems; a pool 80% full,
a scrub more than eight days past its task's threshold, a pool no scrub task scrubs, a WARNING alert, a release other
than `status_nas_release`, and interface changes never committed are things to do.

Memory is Proxmox's view, the host's, for a VM that is no cluster node: with ballooning off, as on every VM here, PVE 9
shows the host's PSS, every page the guest has touched, and the text marks it. A node's memory and disks are the
guest's own, from kubelet's statistics (`kubectl get --raw /api/v1/nodes/<node>/proxy/stats/summary`), since Talos's
guest agent lists no filesystem: its memory is the working set against the working set plus what is available, its
disks `/var` and each Talos user volume that a claim lies on, which reports the volume's whole filesystem, named by
its mount in `status_cluster_volumes`.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `status_source` | `""` | What the host is: `proxmox`, `cluster`, `nas`; empty reads nothing |
| `status_types` | `[]` | The report's sections to show (`apps`, `admin`, `tailscale`, `vms`, `storage`, `backups`, `cluster`, `nas`); empty means every one |
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
| `status_cluster_annotation` | `""` | The annotation in which `just suspend` records an app's replicas |
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
| `status_tailscale_state` | `{}` | The Proxmox host's Tailscale, as roles/proxmox_tailscale's `status` entry point records it; empty when it is off |
| `status_parallel` | `8` | How many reads run at once on a host |
| `status_timeout` | `30` | Seconds each read may take |
| `status_hosts` | `[]` | report: the hosts whose findings it puts together |
| `status_report_file` | `""` | report: where it writes the report as JSON; empty writes none |
| `status_color` | `false` | report: whether the text is in colour |
| `status_quiet` | `false` | report: whether it shows no text |
| `status_read_at` | `""` | report: when the report was read, as its first line says it; empty takes now |
| `status_now` | `0` | report: when the report was read, in seconds since the epoch, which its ages count from; 0 takes now |
| `status_probe_apps` | `true` | Whether each host's play asks the addresses of the apps it found from this machine |
| `status_probe_timeout` | `5` | Seconds each lookup and request may take |
| `status_admin` | `[]` | report: the admin pages it lists, each `{name, url}`; `tailscale_node: true` marks the page the Proxmox host serves itself |
| `status_commands` | `{}` | report: the commands it suggests, by purpose (`upgrade`, `reboot`, `resume` with `{names}`, `talos_upgrade`, `credentials`, `allowlist`, `secrets`, `nas`, `nas_release`, `nas_network`, `backup`, `backup_target`, `etcd_snapshot`, `tailscale`, `tailscale_login`, `split_dns`) |

## In this repository

This repository's `status.yml` reads the Proxmox host (and its Tailscale), the cluster and the NAS at once (`strategy:
free`), then reports, with `group_vars/all/status.yml` (the VMs this repo makes, the same the nightly backup covers; the
admin pages; the commands), `group_vars/proxmox/status.yml` (the installer's storage, its headroom, Debian's reboot
flag, the nightly backup job roles/proxmox_backup keeps, the Tailscale state roles/proxmox_tailscale read),
`group_vars/kubernetes/status.yml` (the credentials roles/talos writes in `talos_config_dir`, the apps as
roles/app_scale finds them, the worker's user volumes, the settings roles/flux_bootstrap keeps, Alertmanager's pod, the
pins, where roles/talos keeps the etcd snapshots) and `group_vars/nas/status.yml` (what roles/truenas's `status` entry
point read, and `truenas_supported_version`, the release that role is written for, in scope through `status.yml`'s
import of it). `status_source` is `proxmox` on the Proxmox host, `cluster` on `homeserver-k8s` and `nas` on `truenas`,
the last two empty without a cluster or a NAS. `status_vms` is the same list of VMIDs as `vm_backup_vmids` (the
cluster's nodes, Home Assistant's, and `local.yml`'s `vm_backup_extra_vmids`), which
`test_status_reports_on_the_vms_the_backup_covers` (`tests/policy/test_roles.py`) checks in each setup.

Its plays: the first, on localhost, says what the report is of in `status_settings` (the setup, the sections, when it
was read, the admin pages, the commands, the reading play's hosts by the report's label for each), tagged `always`,
since the report play reads them whatever tasks are chosen. The reading play asserts `nas_section_error`,
`kubernetes_settings_error` and `haos_settings_error` first, then imports roles/truenas's and roles/proxmox_tailscale's
`status` entry points, each only when a section shown needs its source (the filter `status_sources`), then runs this
role. It has no `any_errors_fatal`: Ansible takes a host that does not answer, which every read ignores
(`ignore_unreachable`), for an error under it and ends the whole run there (seen in the tests). A host refused before
or during its reads (an input check, or `status_read` refusing a command) leaves no `status_found` instead, and the
report play then ends without a report (`meta: end_play`), so there is never half a report. The report play writes to
`.cache/status/report.json` beside the playbook, with `status_read_at` and `status_now` from `status_settings`, so that
it prints the same text as the callback.

`just status` runs `status.yml` as a dry run (`--check`) with the stdout callback
`callback_plugins/homeserver_status.py` and keeps no log. Its sections come first (`just status vms storage`),
checked by the recipe against `status_sections` in the justfile, where a section it does not have is refused with 3
before anything runs, and again by the role, then passed as `-e '{"types": "<names>"}'`. ansible-playbook's arguments
follow `--` (`just status -- -e status_probe_apps=false`): before it, just reads a word that begins with `-` as one
of status's own options and refuses one it does not have. **The exit code comes from the report's file, not from
Ansible**, since `ansible-playbook` sets its own from task results and no callback can change it: the recipe removes
the file, runs the playbook, and exits with the file's `exit` (0, 1 or 2), or with 3 when the run itself failed or
wrote no report. just passes the recipe's code on as its own (`[no-exit-message]`, so it adds no line of its own), so
`just status` exits 0, 1, 2 or 3; the code also ends the report's last line and is `--json`'s `exit`. `--json` prints
the report's file alone on stdout. The recipe sets `STATUS_COLOR` on a terminal without `NO_COLOR`, `STATUS_LIVE` on
a terminal without `--json` whose `TERM` is not `dumb`, and `STATUS_QUIET` with `--json`.

The callback prints the report task's text with one line above its last, the exit line, saying for each host the
reading play ran on whether it answered and was read and how long it took (a host with nothing to read in this setup,
such as the NAS's without a NAS, is left out of it); every failure and unreachable host on stderr, and nothing else of
Ansible's; and nothing on stdout with `STATUS_QUIET`. **On a terminal** (`STATUS_LIVE`) it prints the report itself,
block by block (the filter's `blocks`, which its `text` joins), from the same filter, which it loads by path from
`roles/status/filter_plugins/status.py` beside the playbook, and the settings the first play says, which the report
play then uses too: the head at once, then each section in the report's fixed order as soon as every source it is
worked out from is read (`section_sources`; `SOURCE_HOSTS` says whose findings say each source, and the Proxmox host
says its Tailscale and this machine's tailnet DNS first, in `status_early`) and every section before it is printed,
then, once every host is read, the failed reads, what needs attention, the line on what was read and the exit line.
Below what is printed, a line per section still to come (a spinner and the hosts it waits for, or, for one already
read behind an unfinished one, its problems and things to do, from the report's per-section `counts`), one for what
needs attention and the line on what is read are redrawn in place by the live region every live output shares
(`filter_plugins/live_region.py`), until the report replaces them; on a terminal too short for them, the last alone.
The callback holds Display's lock while the findings change, so finishing the region never waits for its spinner's
thread, which may be waiting for it. A section printed early is the one the report play's own text has, since it
depends on nothing else. After a host fails (a refusal) nothing more is printed, and the run ends without a report.
Without the filter, the sections are shown at the end, and the callback says so on stderr.

## Tests

- `tests/unit/test_status_filters.py`: the filter (parsing, report and text are pure functions), with the shapes
  captured on 2026-10-21 from a Proxmox VE 9.2 host and the Talos cluster on it (the cluster's from the NUC), written
  out with made-up names, addresses and sizes. `test_each_section_depends_only_on_its_sources` removes every other
  source in turn and compares each section, which is what lets the callback print it early.
- `tests/unit/test_status_callback.py`: the callback's `Progress`, which is pure: the sections in order whatever host
  answers first, the lines below them, and that what it prints in the end is the report play's own text.
- `tests/unit/test_status_read.py`: the read list, every form allowed and others refused, and the parallel runs.
- `tests/unit/test_status_probe.py`: the probe, against servers in the test's process.
- `tests/policy/test_roles.py`: `test_status_reads_only_through_status_read` keeps every other task of the role to a
  short list of modules, delegated to localhost alone, and the report entry point's writes to `status_report_file`.
- `tests/integration/test_status.py` runs the real `status.yml`, callback and `just status` in a scratch tree against
  the fake Proxmox host, the fake cluster and, in `local.yml.example`'s setup, `fake_truenas.py` (`fake_kubectl.py`
  lists the kinds the role reads, makes pods up from the Deployments and answers kubelet's statistics and amtool,
  `fake_talos.py` answers `git ls-remote`), with HOME and the credentials the test's own. It checks the exit codes,
  `--json`, another cluster's kubeconfig, a stopped cluster, a host that refuses the connection, probes that get no
  answer, a NAS that does not answer or presents another certificate, one login to the NAS with nothing but reads and
  no secret in the report, a failed and a not yet made backup with the reason from the run's log (`fake_pve.backup_run`
  seeds runs as vzdump leaves them), the host's Tailscale through `fake_tailscale.py` (after `fake_pve_bin` on PATH: a
  served and an unapproved route, `tailscaled` down, and no other device's or the tailnet's name in the report), each
  of the four setups' sections and attention list against what it should have, and that no fake's state changes. It
  also runs `just status` on a pseudo-terminal, replays what it wrote through `tests/vt.py`'s terminal and compares the
  result with the piped report, with no frame wrapped and no sequence a terminal would not know.
- The fake NAS's alerts, scans, topology and root-dataset space follow TrueNAS 25.10's API reference, not a capture
  (the home NAS's key was not at hand).

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

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
