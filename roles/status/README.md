# status

Reads the homeserver's machines without changing anything, and reports what runs, where to reach it, and what needs
doing: what `just status` runs. With only its defaults it reads nothing.

Two entry points:

- **main** reads the host the play runs on, as `status_source` names it, and leaves what it found in the fact
  `status_found`.
  - On the Proxmox host (`proxmox`): the host itself (version, kernel, uptime, CPU over the last day, memory, root
    filesystem), each of `status_vms` (state, uptime, CPU over the last day, memory, settings waiting for a restart,
    and, where its guest agent reports them, its filesystems, OS and address), every other VM as Proxmox sees it
    (state, uptime, CPU now and over the last day, memory, its boot disk's size: another project's, or made by hand,
    drawn with a diamond for its dot and never a problem),
    each storage's use, the disk budget of `status_budget_storages`, the upgrades apt would make and whether an
    upgrade asked for a reboot, and the backups: each of `status_backup_jobs` against the host's job of that id
    (there, enabled, writing to its storage, covering its VMs, and when it runs next), each VM's newest backup on its
    job's storage, and, from the logs of the newest runs that failed, which VMs failed and why. It reads in two
    steps. First the node's name (`hostname -s`, unless `status_proxmox_node` gives it), which the API's paths need,
    with `/cluster/resources` (the node, every VM and every storage in one call) when the VMs are shown, since the
    second step reads every running VM's history. Then everything else at once: `/cluster/resources` if not yet read,
    the node's status and day history, the day history of every running VM, each of `status_vms`' pending settings,
    and for a VM whose guest agent reports them (Home Assistant's) its filesystems and OS, and its address when it
    serves an app without a URL of its own, past the Docker bridges and other inner interfaces Home Assistant OS has.
    Talos's guest agent lists no filesystem and no OS version (seen on the NUC). Every other VM on the host, another
    project's or one made by hand, is a row after this setup's, its name in cyan, and never named as a problem. A
    host that does not answer (every read has `ignore_unreachable`) is recorded with Ansible's message
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
    point (`roles/truenas/tasks/status.yml`: `truenas_info` with its `status` option, one login), and the role works out
    what `status_nas_state` says: its ports, each pool's health, use, layout and last scrub, each disk's temperature,
    state and errors, TrueNAS's alerts, each snapshot task's last run, the NFS and SMB services and their shares, who
    has the NFS shares mounted (named by `status_nas_known_clients`), the services that run though `status_nas_services`
    keeps them stopped, the NAS's clock against this machine's, each Time Machine backup against the Macs of
    `status_nas_macs`, and whether the NAS runs `status_nas_release`. The `status` option adds `pool.scrub.query`,
    `alert.list` and `pool.snapshottask.query` to `truenas_info`'s reads, and one `pool.snapshot.query` for every
    enabled Time Machine share whose dataset exists, none without one: `[["dataset", "in", [<their datasets>]]]` with
    `{"select": ["name"]}`, which TrueNAS answers on its fast path (`plugins/zfs_/snapshot.py` at TS-25.10.7), listing
    those datasets' snapshots by name and reading no property, where any other filter or select reads every snapshot on
    the NAS with all its properties first. It picks each pool's health, root-dataset use, layout and last scan, the
    alerts not dismissed, each snapshot task's last run, and each Time Machine share's dataset (whether the NAS has one
    at its path), its use, quota and snapshots' share of it, and, for an enabled share, its newest backup: the time in
    the newest `aapltm-<epoch>` snapshot Samba takes after each completed one, split from each snapshot's name on this
    machine, so that it counts for its own dataset alone. A time more than an hour ahead of this machine's clock is
    ignored: the NAS's clock was wrong, or the name was made by hand, and it would keep the Mac from ever reading as
    late (one past the year 9999 would fail the read). The API's times (`{"$date": ms}`) are turned into RFC 3339. Last
    come three reads in the same login, each of which may fail without the NAS going unread: `disk.temperatures` with
    `[]` and `true` (every disk, each `[celsius, critical celsius]`, either null for a disk without a sensor TrueNAS can
    read), which TrueNAS answers from the kernel's sensors (hwmon: drivetemp, nvme), each read of which issues a command
    to the disk, its docstring says (`utils/disks_/disk_class.py` at TS-25.10.7), and keeps each reading for five
    minutes (`plugins/disk_/temperature.py`), so that a disk is asked at most once in five minutes whoever calls,
    TrueNAS's own temperature alert included, which calls it at every check (a few hundredths of a second on a real
    NAS); `nfs.get_nfs3_clients` (rmtab's `{ip, export}`, which can keep a client that has gone, NFSv3 having no way to
    tell, and tells no two clients of one address apart); and `nfs.get_nfs4_clients`, asked for `info.address` and
    `info.status` alone (`{"select": [...]}`, which TrueNAS's `filter_list` follows on dotted paths,
    `utils/filter_list.py`), so that a client's own name, which names its host, and every file it holds open stay on the
    NAS. Of an NFSv4 client only a live one counts, `confirmed`: the kernel keeps a client that stopped renewing its
    lease as `courtesy` for up to a day, then `expirable` (`plugins/nfs_/status.py`), and one that never confirmed
    itself as `unconfirmed`, none of which has a share mounted. Each address is kept once, without its port
    (`info.address` has it, `[...]:port` for IPv6), with how many clients it is: each live NFSv4 client one, an address
    rmtab alone names one. Each of the three may take 10 s (`OPTIONAL_TIMEOUT`, where every other call has the client's
    `truenas_api_timeout`, 120 s), and one that fails (a `TrueNASError`, which a timeout is too) or answers null leaves
    its part null, and why in the status's `unread`, which the report lists with the reads that failed, after the
    sections. Of what it read before, it keeps each pool's disks from `pool.query`'s topology (every kind of vdev, depth
    first), each by the device name `disk.temperatures` gives it (`disk`; a disk that is gone, marked `gone`, by the one
    TrueNAS last knew there, `unavail_disk`'s name, else its ZFS guid, never the vdev's own name, which is its path's
    and, on a pool made by hand on a `/dev/disk/by-id` path, would carry the disk's model and serial), with its state
    and ZFS's read, write and checksum error counts; each interface's type, MTU and `active_media_subtype` (`10000Mb/s
    Twisted Pair`, for example; the API's examples give `1000baseT <full-duplex>`), never its MAC; and `system.info`'s
    `datetime`, beside this machine's clock halfway through that call (`read_at`), within half its round trip. The boot
    pool's disks, which `pool.query` leaves out, are `disk.details`' disks whose `imported_zpool` is no pool
    `pool.query` lists. The entry point tries each address once (`api_connect_wait: 0`) and never fails (`failed_when:
    false`): a NAS that did not answer, refused the key or presented another certificate than the pinned one is reported
    as a source not read, not a failure, and the key goes to no other server. `system.info`'s serial and the disks'
    serials and models never reach the report: `truenas_info` keeps none of them, and TrueNAS's alerts, whose texts
    quote them (Disk Temperature Is Too Hot and the SMART alerts a disk's serial, Pool Status Is Not Healthy a gone
    disk's model and serial, at TS-25.10.7), come with the disk's device name in their place, or `(serial left out)` and
    `(model left out)` where none fits.
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
host's 15 reads take 25 s one after another and 8 s at once (reading the guests first, and every running VM's history,
took the home host from 5 s to 7 s with seven other VMs running, 2026-11-09), and `apt-get -s` takes 0.7 s where the
API's own update list takes 4.5 s; the cluster's reads take about 0.1 s each and `git ls-remote` about 1 s, so the
cluster is read in about a second.

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
an alert at ERROR or worse, NFS or SMB not running while it has shares, a service running that `status_nas_services`
keeps stopped (with `status_commands`' `nas`), an NFS share mounted from an address that is no machine of
`status_nas_known_clients` (with `nas_clients`), and TrueNAS not ready are problems; a pool 80% full, a scrub more than
eight days past its task's threshold, a pool no scrub task scrubs, a WARNING alert, a release other than
`status_nas_release`, interface changes never committed, a disk within 10 °C of its own critical temperature (with
`nas_disk_heat`), and the NAS's clock 60 s or more from this machine's either way (with `nas_clock`) are things to do.

**A disk's errors are its pool's problem.** ZFS calls a pool not healthy (`FAILING_DEV`, which TrueNAS words "One or
more devices has experienced an unrecoverable error…") as soon as any disk of it has a read, write or checksum error, a
cache disk too, and only a spare not (`find_vdev_problem` in `libzfs_status.c` at zfs-2.3.4), so the pool's problem
names each disk with errors and its counts, `the pool is ONLINE, with errors on nvme2n1 (3 checksum): …`, with
`nas_disk_errors`, and a disk's errors are never an item of their own. The counts stay until the pool is imported again,
when the NAS reboots, or a `zpool clear`: TrueNAS 25.10.7's API has no call that clears them (none in
`api/v25_10_5/pool.py` or `plugins/zfs_/pool_actions.py`), and a shell on the NAS is no way this repository changes it,
so a repaired error keeps the pool a problem until the NAS's next boot. A critical temperature of 0 or below, or none,
is no threshold, as TrueNAS's own alert takes it (`alert/source/disk_temp.py`): such a disk is never hot and never red.

The section's lines, in order: the header; `Network`, each port that is up by its speed (Mb/s or Gb/s, from `<n>Mb/s` or
`<n>base…`, else TrueNAS's own words), the MTU when it is not 1500, and how many of the ports (physical interfaces: a
bridge, VLAN or link aggregation is none) are up, never judged; `Pools`; `Disks °C` (`Disks` when the temperatures were
not read), a line per pool, then the boot pool's and a disk in no pool, each disk with its temperature in whole degrees
Celsius (a half rounded up), yellow within 10 °C of its critical one and red at or above it, `—` for no reading, and in
brackets `gone` and a state other than ONLINE (red) and its errors (yellow), as `nvme3n1 33 (3 checksum)`; `Snapshots`;
`NFS`, with who has its shares mounted, `mounted by the Proxmox host · homeserver-worker-1`, an address that is more
than one client with how many, `the Proxmox host (2 clients)`, each client a phrase of its own, so that many wrap the
line between them, an address no machine has in red, or `mounted by nothing`; `SMB` and `Macs`; `Services`, only when
one runs that is to be stopped, SMB by that name, where TrueNAS's id is `cifs`; `Alerts`. A disk that is gone (no disk
at its path and its state not ONLINE) goes by the name TrueNAS last knew at its place, as `nvme2n1 (gone, unavail)`,
with no reading: the kernel may since have given that name to another disk, its replacement even, which keeps its own
line and reading. A disk at or above its critical temperature is red and nothing more: TrueNAS's own alert (Disk
Temperature Is Too Hot, critical, its serial left out) is among the alerts already. A pool whose disks have errors reads
not healthy, its state red on the `Pools` line. A read of the temperatures or of the NFS clients that failed leaves them
out of the section and is listed with the reads that failed; the NAS is read all the same. **Tailnet clients reach the
LAN masqueraded as the Proxmox host's address** (roles/proxmox_tailscale), so the NAS sees a tailnet client that mounts
a share as `the Proxmox host`: that name can stand for traffic from the tailnet too. The VM and backup shares' hosts are
the Proxmox host's own address, the very one masqueraded traffic carries, so for those only the tailnet guard
(roles/proxmox_tailscale's nftables table) and the tailnet policy's grants keep the tailnet out; the shares' hosts keep
it out of the cluster's shares, which name the worker alone. A second NFSv4 client behind the Proxmox host's address
shows as `the Proxmox host (2 clients)`, and is only shown: an NFSv3 mount says nothing of how many clients share an
address. With no machine in `status_nas_known_clients`, every client is shown by its address and none is judged.
Whatever sections are asked for, the NAS's section, what it needs and the reads that failed are the same: `just status`
shows each section as `just status <section>` does.

**A Mac's Time Machine backup** is judged by the Macs of `status_nas_macs`, each its share's name and whether it is
retired, as the configuration says, never the share: TrueNAS disables a pool's shares when the pool is exported, and a
share can be turned off by hand, while the Mac goes on wanting its backups. For a Mac that backs up, a disabled share or
a missing dataset is a problem, since the Mac cannot back up (with `status_commands`' `nas`), and so is a backup at 90%
of its quota or more: should the snapshots fill the quota, writes fail inside a disk image that still shows free space.
A backup at 80% of its quota or more (`time_machine_quota`), one whose newest backup is ten days old or more and one
with no completed backup yet (`time_machine`), and a Mac the NAS has no share for yet (`nas`) are things to do. A late
backup is never a problem, since a laptop travels, and a disabled share's backup is neither late nor never completed:
the problem says why, and its time is not read. A retired Mac's share is never late or full, but one still enabled is a
thing to do (`nas`), since the NAS play disables it; a share no Mac names is listed and never judged. TrueNAS raises no
quota alert of its own for a dataset whose refquota is smaller than its quota, as each Mac's is in this repository, so
this is the one warning that the snapshots are filling their room. The report's `Macs` line shows each backup's age, its
use against its quota, in yellow from 80% and red from 90% as a pool's bar is, and what its snapshots hold, which the
first months after a Mac's backup fills up are watched for. A retired Mac's share reads `retired` (`retired, share
enabled` while it is), a Mac's share the NAS does not have `no share yet`, one without its dataset `dataset missing`, a
disabled one `share disabled` in place of its age, and a share no Mac names `not configured`, its name in cyan.

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
| `status_types` | `[]` | The report's sections to show (`admin`, `vms`, `nas`, `storage`, `cluster`, `apps`, `tailscale`, `backups`), in that order; empty means every one |
| `status_vms` | `[]` | This setup's VMs, each `{vmid, name, made_by, start, guest_agent, node, app}`; every other VM is shown as Proxmox sees it, never judged |
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
| `status_nas_macs` | `[]` | The Macs that back up to the NAS with Time Machine, each `{share, retired}`, which each Time Machine share is judged by; empty judges none |
| `status_nas_services` | `{}` | The NAS's services as its configuration keeps them, `{name: {enabled, state}}` as roles/truenas takes them; one running that is to be stopped is a problem; empty judges none |
| `status_nas_known_clients` | `{}` | The machines of this setup, `{address: name}`, which name who has the NFS shares mounted; any other address is a problem; empty judges none |
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
| `status_commands` | `{}` | report: the commands it suggests, by purpose (`upgrade`, `reboot`, `resume` with `{names}`, `talos_upgrade`, `credentials`, `allowlist`, `secrets`, `nas`, `nas_release`, `nas_network`, `nas_clock`, `nas_disk_heat`, `nas_disk_errors`, `nas_clients`, `time_machine`, `time_machine_quota`, `backup`, `backup_target`, `etcd_snapshot`, `tailscale`, `tailscale_login`, `split_dns`) |

## In this repository

This repository's `status.yml` reads the Proxmox host (and its Tailscale), the cluster and the NAS at once (`strategy:
free`), then reports, with `group_vars/all/status.yml` (the VMs this repo makes, the same the nightly backup covers; the
Macs `local.yml`'s `timemachine_backups` backs up to the NAS, as `status_nas_macs`; the Proxmox host and the cluster's
nodes by the addresses `local.yml` gives them, `proxmox_address` and `kubernetes_node_addresses`, as
`status_nas_known_clients`; the admin pages; the commands),
`group_vars/proxmox/status.yml` (the installer's storage, its headroom, Debian's reboot flag, the nightly backup job
roles/proxmox_backup keeps, the Tailscale state roles/proxmox_tailscale read), `group_vars/kubernetes/status.yml` (the
credentials roles/talos writes in `talos_config_dir`, the apps as roles/app_scale finds them, the worker's user
volumes, the settings roles/flux_bootstrap keeps, Alertmanager's pod, the pins, where roles/talos keeps the etcd
snapshots) and `group_vars/nas/status.yml` (what roles/truenas's `status` entry point read,
`truenas_supported_version`, the release that role is written for, in scope through `status.yml`'s import of it, and
`truenas_services` with the SMB service, `cifs`, from `truenas_smb_service`, which the SMB stage keeps and the services
stage refuses, as `status_nas_services`).
`status_source` is `proxmox` on the Proxmox host, `cluster` on `homeserver-k8s` and `nas` on `truenas`, the last two
empty without a cluster or a NAS. `status_vms` is the same list of VMIDs as `vm_backup_vmids` (the cluster's nodes, Home
Assistant's, and `local.yml`'s `vm_backup_extra_vmids`), which `test_status_reports_on_the_vms_the_backup_covers`
(`tests/policy/test_roles.py`) checks in each setup. Each of `status_nas_macs` is a Mac by the share `tm-<name>` that
`group_vars/nas/timemachine.yml` hands the NAS for it, retired as `local.yml` says, which
`test_status_reads_the_nas_as_its_play_reaches_it` checks against the shares the NAS is handed.

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
  source in turn and compares each section, which is what lets the callback print it early. The Time Machine tests judge
  each share by the Macs configured: a retired Mac's share never late or full whatever its state, and a thing to do
  while still enabled (`test_a_retired_mac_is_never_late_or_full_whatever_its_share`), an active Mac's disabled share
  and missing dataset problems, a Mac without a share a thing to do, and a share no Mac names listed and never judged,
  nearly full and never backed up (`test_each_share_is_judged_by_the_macs_configured`); each threshold at its edge,
  exactly 80% and 90% of the quota and exactly ten days a thing to do or a problem, 79.9%, nine days and 23 hours and no
  quota at all nothing; and the `Macs` line's colours, plain, yellow and red as a pool's bar. The NAS's details: the
  `Network` and `Disks` lines exactly, each disk by pool, the boot pool's and one in no pool, with no serial or model in
  the JSON; a reading exactly 10 °C below its critical temperature (and 70.5 against 81, shown as 71) a thing to do in
  yellow, 11 below nothing, at or above it red and nothing more, and none without a critical temperature or with one of
  0 or below (`test_a_disk_near_its_critical_temperature`, `test_a_disk_without_a_critical_temperature_is_never_hot`); a
  disk's errors and state on its line, wrapped under the first disk, and named in its pool's problem, never an item of
  their own, the pool not healthy as ZFS makes it: DEGRADED by a faulted disk, or ONLINE with errors ZFS repaired
  (`test_a_disks_errors_are_its_pools_problem`); a refused temperature read, the disks listed without readings and the
  read named among the reads that failed; a gone disk beside its replacement of the same name, each on its own
  (`test_a_gone_disk_hides_no_disk_of_its_name`); each port's speed from TrueNAS's words; no client, this setup's
  machines, an unknown client (a problem, in red), and no machine configured (shown, never judged); many clients
  wrapping the NFS line between them (`test_the_nfs_line_wraps_between_clients`); the NFS clients not read; a service
  running though configured stopped, alone or beside SMB, which the text calls SMB and the JSON `cifs`, and none without
  the configuration; a clock 60 s ahead or behind a thing to do, 59 s either way nothing; and the section's lines in
  order within 100 columns. `test_just_status_shows_the_nas_as_just_status_nas_does` holds `just status` to the union of
  its sections: the NAS's block, its attention items and counts, and the reads that failed are the same with `nas` alone
  and with every section.
- `tests/unit/test_truenas_modules.py`: `truenas_info`'s `status` read, against a scripted API that records each call's
  parameters and answers alike whatever they filter on: `sharing.smb.query` once, one `pool.snapshot.query` with exactly
  the fast path's filter and select, for the enabled Time Machine shares on a dataset, and none without one
  (`test_no_snapshot_is_read_without_an_enabled_time_machine_share_on_a_dataset`); each backup's time from its own
  dataset's snapshots, though another Mac's is newer, a disabled share's never read, a time in 2100 or in the year 10000
  ignored, and a share whose path names no dataset said so, without sizes
  (`test_each_time_machine_backup_is_its_own_datasets_newest`); each pool's disks, a gone one by the disk TrueNAS knew
  there or its guid, a cache disk too; every disk's temperature, `disk.temperatures` asked `[]` and `true`; the NFS
  clients' addresses once each, without their ports, with how many live clients each is, two NFSv4 clients behind one
  address, and none that is courtesy, expirable or unconfirmed, `nfs.get_nfs4_clients` asked for `info.address` and
  `info.status` alone; the clock with this machine's during the read; the three reads last, each given 10 s; and no
  model, vdev name, MAC, client's own name or open file returned. TrueNAS's alerts as its sources word them, each serial
  and model by the disk's device name, a gone disk's by the name TrueNAS last knew there, the NAS's serial and a model
  alone left out (`test_no_alert_keeps_a_disks_serial_or_model`), and a serial matched whole, never inside a longer one
  (`test_a_serial_is_matched_whole`). A refused `disk.temperatures`, `nfs.get_nfs3_clients` (after which
  `nfs.get_nfs4_clients` is not asked) or `nfs.get_nfs4_clients` leaves its part null and says why, the rest whole
  (`test_a_read_that_fails_on_its_own_leaves_its_part_out`), and so does one that times out
  (`test_a_read_that_times_out_leaves_its_part_out`) or answers null
  (`test_a_null_answer_is_said_among_the_reads_that_failed`).
- `tests/unit/test_status_callback.py`: the callback's `Progress`, which is pure: the sections in order whatever host
  answers first, the lines below them, and that what it prints in the end is the report play's own text.
- `tests/unit/test_status_read.py`: the read list, every form allowed and others refused, and the parallel runs.
- `tests/unit/test_status_probe.py`: the probe, against servers in the test's process.
- `tests/policy/test_roles.py`: `test_status_reads_only_through_status_read` keeps every other task of the role to a
  short list of modules, delegated to localhost alone, and the report entry point's writes to `status_report_file`;
  `test_status_reads_the_nas_as_its_play_reaches_it`, that `status_nas_macs` names the shares the NAS is handed for
  `local.yml`'s Macs, retired as it says, that `status_nas_services` is `truenas_services` with SMB's state as the SMB
  stage keeps it, without a Mac and with one, and that `status_nas_known_clients` names the Proxmox host and each
  node by `local.yml`'s addresses.
- `tests/integration/test_status.py` runs the real `status.yml`, callback and `just status` in a scratch tree against
  the fake Proxmox host, the fake cluster and, in `local.yml.example`'s setup, `fake_truenas.py` (`fake_kubectl.py`
  lists the kinds the role reads, makes pods up from the Deployments and answers kubelet's statistics and amtool,
  `fake_talos.py` answers `git ls-remote`), with HOME and the credentials the test's own. It checks the exit codes,
  `--json`, another cluster's kubeconfig, a stopped cluster, a host that refuses the connection, probes that get no
  answer, a NAS that does not answer or presents another certificate, one login to the NAS with nothing but reads and no
  secret in the report, the Macs' Time Machine backups judged by `timemachine_backups` (`sharing.smb.query` once, one
  `pool.snapshot.query` whose parameters the fake's call log shows, none without a Time Machine share, a retired Mac's
  share listed, and laptop's backup late while desk's is an hour old: each Mac's time from its own dataset), a the NAS
  section's details (`test_the_nas_sections_details`: the `Network`, `Disks`, `NFS` and `Services` lines exactly and in
  order after the header, matched whether or not it gives the address the NAS answered at, a hot disk, one with checksum
  errors and its pool's problem naming them, ssh running, two NFSv4 clients behind the Proxmox host's address, an NFS
  client of no machine of this setup and one gone quiet, courtesy, which no longer counts, and the clock 95 s behind,
  the three new reads once each with their parameters, and no disk's serial or model, NFS client's name or open file in
  the report; then the temperatures and the NFSv4 clients refused, the NAS read all the same without them, saying so),
  TrueNAS's alerts that quote a disk's serial or model, a disk at its critical temperature, a failed SMART self-test and
  a pool degraded by a gone disk, each by the disk's device name in the text, the report and `--json`
  (`test_no_disks_serial_or_model_reaches_the_report`), a failed and a not yet made backup with the reason from the
  run's log (`fake_pve.backup_run` seeds runs as vzdump leaves them), the host's Tailscale through `fake_tailscale.py`
  (after `fake_pve_bin` on PATH: a served and an unapproved route, `tailscaled` down, and no other device's or the
  tailnet's name in the report), each of the four setups' sections and attention list against what it should have, and
  that no fake's state changes. It also runs `just status` on a pseudo-terminal, replays what it wrote through
  `tests/vt.py`'s terminal and compares the result with the piped report, with no frame wrapped and no sequence a
  terminal would not know.
- The fake NAS's alerts, scans and topology follow TrueNAS 25.10's API reference, not a capture (the home NAS's key was
  not at hand). It reports the used and available space of any dataset its state's `space` names, and what a dataset's
  snapshots hold from `snapshot_space`. It answers `pool.snapshot.query` from the snapshot names in its state, with the
  `=`, `^` and `in` filters, and follows TrueNAS's fast path as `plugins/zfs_/snapshot.py` has it at TS-25.10.7: a
  select of `name` alone, filtered on name, pool or dataset alone, reads no property, so its entries have none, and
  asked nothing more than that select and `=` or `in` on pool or dataset, it returns libzfs's entries whole. Its disks,
  NFS clients, links and clock follow TrueNAS 25.10.7's middleware source: each pool's topology with the `disk` and
  `device` `pool.transform_topology` adds, a pool not healthy (`FAILING_DEV`) once ZFS counts errors on any of its disks
  but a spare, as libzfs works it out, `disk.details`' used disks by pool, the boot pool's included, with stand-in
  serials and models, the alerts that quote them as TrueNAS's alert sources word them (`fake_truenas.alert`),
  `disk.temperatures` from the state's `temperatures` (nulls for a disk it leaves out), rmtab's and the kernel's NFS
  clients from `nfs3_clients` and `nfs4_clients`, the latter selected on dotted paths as `filter_list` selects, each
  interface's `link`, `mtu` and `media`, and `system.info`'s `datetime` set apart from this machine's by `clock_offset`.
  A method in the state's `refused` is refused as TrueNAS refuses a call the key may not make. The shapes were checked
  against the middleware source and a read-only read of a real NAS (2026-11-18), not a capture written out here.

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
