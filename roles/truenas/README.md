# truenas

Configures TrueNAS Community Edition 25.10 through its JSON-RPC 2.0 WebSocket API (`wss://<nas>/api/current`), from
the controller, with an API key. The REST API is deprecated and gone in TrueNAS 26, so it is not used, and the role
never uses SSH. The modules are in `library/`, with one client in `module_utils/truenas_api.py` that they share. With
only its defaults it connects, reports the NAS's state and changes nothing. Each part of the NAS is managed only when
its variable is set.

It never destroys, wipes, exports or upgrades a pool. It never deletes a dataset, a share or a user. It refuses an
NFS share that names no hosts or networks, since NFS with `sec=sys` has no authentication, and refuses to start SMB
while a local SMB login it does not list exists.

## Requirements

- The controller runs the modules locally: `ansible_connection: local` and an interpreter with
  `websocket-client` installed (`ansible_python_interpreter: "{{ ansible_playbook_python }}"` when
  Ansible runs from a virtualenv that has it).
- An API key for a user with full admin rights, kept in a vault. Use HTTPS only: TrueNAS revokes a key
  sent over plain HTTP, which is why the client has no `ws://` fallback.
- The SHA-256 fingerprint of the NAS's web certificate in `truenas_api_cert_sha256`, unless
  `truenas_validate_certs` is on with a certificate the controller trusts. With certificate validation off (the
  default certificate is self-signed), it is what stops the API key being sent to another device that answers on
  one of the addresses, so the role refuses to run with neither, and so does its client.

## On a fresh install

The role cannot create its own way in. After installing TrueNAS, and before the first run:

1. Create a local user in the `builtin_administrators` group (the Full Admin role) and an API key for it with no
   expiry, over HTTPS. Put the key where `truenas_api_key` reads it, ideally a vault.
2. Read the new install's certificate fingerprint and update `truenas_api_cert_sha256`; a reinstall generates a
   new self-signed certificate.
3. Make sure one of `truenas_api_hosts` reaches the NAS: a DHCP reservation for its MAC makes the install come up
   on the expected address.

Then run with `--check` first. Pools are imported with their datasets; shares, services and settings are
re-applied, since TrueNAS keeps those on the boot drive. This repo's full procedure, for both machines, is in
`scripts/runbooks/REBUILD.md`.

## Stages and tags

| Tag | Does | Driven by |
|---|---|---|
| `truenas_info` (always) | Connects, checks the release, reports state | `truenas_api_hosts`, `truenas_api_key` |
| `truenas_network` | Network settings; DHCP to a static address, with TrueNAS's rollback safety net | `truenas_network_settings`, `truenas_network_mode`, `truenas_static_address`, `truenas_gateway`, `truenas_nameservers` |
| `truenas_pools` | Pins the system dataset, imports (or creates on blank disks) pools, sets auto-trim and scrub | `truenas_pools`, `truenas_pool_*`, `truenas_system_dataset_pool` |
| `truenas_system` | General settings (UI, timezone, ...), NTP servers | `truenas_general_settings`, `truenas_ntp_servers` |
| `truenas_datasets` | Creates datasets or corrects their properties | `truenas_datasets` |
| `truenas_snapshots` | Creates periodic snapshot tasks or corrects them, never deletes one | `truenas_snapshot_tasks` |
| `truenas_nfs` | NFS service settings and shares | `truenas_nfs_settings`, `truenas_nfs_shares` |
| `truenas_smb` | When SMB is to run, refuses an SMB login it does not list; then SMB service settings, local users, each share's directory owner, SMB shares, the login check again, and last the SMB service, started or stopped | `truenas_smb_settings`, `truenas_users`, `truenas_user_passwords`, `truenas_users_reset`, `truenas_smb_closed_users`, `truenas_api_user_smb`, `truenas_permissions`, `truenas_smb_shares`, `truenas_smb_service` |
| `truenas_alerts` | The outgoing mail server (its password compared but never printed), and alert services | `truenas_mail`, `truenas_mail_password`, `truenas_alert_services` |
| `truenas_services` | Enables, disables, starts and stops services, last | `truenas_services` |

The input checks and the read-only connection (`truenas_info`) run under every tag, so one stage can be checked or
applied on its own (`just check truenas --tags truenas_nfs`), and a stage whose variables are empty does nothing. The
order matters: the snapshot tasks follow the datasets they name, which must exist, and the services come last, so that
NFS starts only once its shares exist. SMB is the exception: the SMB stage starts its own service as its last step,
after its login check (below), so that no tag can start SMB without that check, and stops it as its first, so that no
later failure keeps it running; the role refuses `cifs` in `truenas_services`. Every stage leaves alone what it is not
given: no dataset, share, snapshot task, alert service or service that is not listed is touched, and none is deleted.
The users the SMB stage touches without listing them in `truenas_users` are the ones it is told to take out of SMB,
`truenas_smb_closed_users` and the API key's own user, and of those it changes `smb` alone.

A second entry point, `status` (`tasks_from: status`), is what `just status` runs on the NAS: `truenas_info` with its
`status` option, which adds each pool's health, capacity, layout and last scrub, TrueNAS's alerts that are not
dismissed, each snapshot task's last run, the uptime, and each Time Machine share's backup (its dataset's use, quota
and snapshots' share, and its newest `aapltm-<epoch>` snapshot) to what it reads (`pool.scrub.query`, `alert.list`,
`pool.snapshottask.query`, and one `pool.snapshot.query` for every enabled Time Machine share whose dataset exists,
`[["dataset", "in", [...]]]` with `select: ["name"]`, TrueNAS's fast path, which reads no property per snapshot, and
none without such a share; times turned from the API's `{"$date": <milliseconds>}` into RFC 3339), in the same login.
A share's `dataset_found` is false when its path names no dataset, and an `aapltm-` time more than an hour ahead of
the controller's clock is ignored.
It changes nothing, tries each address once (`api_connect_wait: 0`), and never fails: a NAS that does not answer,
refuses the key or presents another certificate than the pinned one leaves `truenas_status` without an `api_host`, with
why in its `msg`, and the key goes to no other server. `roles/status` reports what it says.

`defaults/main.yml` and `meta/argument_specs.yml` describe every variable. Run with `--check --diff`
first: `tests/integration` runs the role against a fake NAS, which cannot prove the real one behaves the
same.

## Example

```yaml
# group_vars/nas.yml
ansible_connection: local
ansible_python_interpreter: "{{ ansible_playbook_python }}"
truenas_api_key: "{{ vault_truenas_api_key }}"
truenas_api_hosts: ["192.0.2.10", "192.0.2.50"]   # static address first, then the install-time one

truenas_network_mode: static
truenas_static_address: 192.0.2.10/24
truenas_gateway: 192.0.2.1
truenas_nameservers: [192.0.2.1]

truenas_pools:
  - name: tank
    layout: RAIDZ1
truenas_pool_autotrim: "ON"

truenas_general_settings: {ui_httpsredirect: true, timezone: Etc/UTC}

truenas_datasets:
  - name: tank/vm
    properties: {sync: STANDARD, recordsize: 64K, compression: LZ4, acltype: POSIX, aclmode: DISCARD}

truenas_snapshot_tasks:
  - dataset: tank/vm
    naming_schema: daily-%Y-%m-%d_%H-%M
    lifetime_value: 7
    lifetime_unit: DAY
    schedule: {minute: "00", hour: "00", dom: "*", month: "*", dow: "*"}

truenas_mail: {outgoingserver: smtp.example.org, port: 587, security: TLS, smtp: true, user: nas, fromemail: nas@example.org}
truenas_mail_password: "{{ vault_truenas_mail_password }}"
truenas_alert_services:
  - {name: Email, attributes: {type: Mail, email: admin@example.org}, level: WARNING, enabled: true}

truenas_nfs_shares:
  - path: /mnt/tank/vm
    hosts: ["192.0.2.20"]
    maproot_user: root
    maproot_group: root

truenas_services:
  nfs: {enabled: true, state: started}
  ssh: {enabled: false, state: stopped}
```

## Notes

The design facts a change most often breaks:

- **Every change to the NAS goes through this role.** No UI clicks, ad-hoc API calls or SSH: a fresh install plus one
  run must rebuild it. Read-only API queries are fine. What the role never does, deleting, rolling back and a snapshot's
  check clone with its temporary share, is the owner's alone, made in TrueNAS's web UI only where a runbook says how;
  never an agent's. The runbooks are `scripts/runbooks/teardown.md` and `scripts/runbooks/timemachine.md`.
- **TrueNAS rate-limits logins** to 20 per minute from one client address, counted in fixed 60-second windows, and
  each module run logs in once. The modules therefore take whole lists (every dataset, every service) rather than
  being looped over, so a full run logs in about 16 times, and the client waits out the window when it is hit
  (`truenas_api_login_wait`, one window plus slack).
- **The static address uses TrueNAS's commit-and-check-in.** `truenas_interface` commits the change with a rollback
  timer (`truenas_checkin_timeout`, 45 seconds or more), reconnects on the new address and checks in; if it cannot,
  the NAS reverts by itself when the timer runs out, so a wrong value locks nobody out. A commit TrueNAS refuses has
  its saved change discarded. The play reaches the NAS on the static address first and falls back to the one it was
  installed with (`truenas_api_hosts`). Interface changes left uncommitted, perhaps by an interrupted run, stop every
  run until they are committed or rolled back on the UI's Network page.
- **The system dataset is pinned before any pool import.** On a fresh install it reads as `boot-pool` but counts as
  unset (`pool_set` false), and importing a pool would move it onto that pool. The role pins it to
  `truenas_system_dataset_pool` (`boot-pool`) first, compared through `truenas_config`'s `match`, and again after the
  import, in case it moved all the same.
- **Pools are imported or created, never destroyed, wiped, exported, expanded or upgraded.** Creation needs
  `truenas_pool_allow_create` and every listed disk blank, named by serial (`truenas_pool_disks`), and is untested.
- **Modules return only the fields they manage.** `system.general.config` includes the UI certificate's private key;
  `tests/unit/test_truenas_client.py` checks that `truenas_config` never returns it, in check mode, applying, or
  failing.
- **The API key goes only to the NAS's own certificate.** Certificate validation is off (self-signed), so
  `truenas_api_cert_sha256` pins the certificate's SHA-256 (in `local.yml` here); a server presenting anything else
  gets no login. Replacing the certificate means updating the pin.
- **An NFS share must name its hosts or networks.** NFS with `sec=sys` has no authentication, so the input checks
  refuse a share open to every host before anything is sent.
- **A secret setting is compared, never printed.** `mail.config` returns the mail server's password, so
  `truenas_config` takes it in `secret_settings` (a `no_log` option): compared and sent like the other fields, so a
  rotated password reaches the NAS on the next run, and shown only as `<secret>` or `<secret, changed>`. The
  idempotence test rotates it and checks that neither password appears in any run's output.
- **An existing dataset's `acltype` or `aclmode` is changed only when its entry sets `allow_acl_change`**, since that
  changes who can read the files already in it. An entry with `create_only` makes a dataset the role must not manage
  but a share needs (here the media library) only when it is missing, as after the ephemeral pool's disk is lost,
  since its share cannot be made without it, and otherwise leaves it exactly as it is.
- **SMB is never left to run with an SMB login the role does not list.** Every local user with `smb` on becomes a
  network login the moment the service starts, and TrueNAS makes every new user an SMB login unless told otherwise,
  so a hand-made full administrator (an API key's user, say) may be one without anyone having noticed while SMB was
  off. Whenever SMB will run after the run, now or at the next boot, as `truenas_smb_service` asks or, for what it
  leaves out, as the connection found it, and the stage manages any of SMB, its first task, in a dry run and under the
  stage's own tag too, refuses to go on while the connection found such a login: it names the login, names
  `truenas_api_user_smb` when it is the API key's own user (`auth.me`), and says whether SMB already runs, in which
  case the login can log in until it is closed. The remedy is `truenas_smb_closed_users`, which takes the named logins
  out of SMB and changes nothing else about them; `truenas_api_user_smb: false` takes the API key's user out. A
  connection whose reading of the logins is missing (its tag skipped) is refused too, never taken as none. Just before
  a start, the check runs again on the logins as `truenas_user` left them, which it returns, or predicts in a dry run.
  A run that stops SMB is never refused, and the stop is the stage's first step: retire what uses SMB and the run
  stops it. `truenas_smb_service` takes `enabled` and `state` alone, so a misspelt key is refused rather than read as
  none. TrueNAS allows taking a user out of SMB for any but its built-in users and its first administrator, and
  refuses the API key's user's update of itself when two-factor authentication is on and that user has none set up;
  a dry run cannot show that, so check it before the first run that changes the API user.
- **A user's password is set, never compared.** `user.query` returns each user's password hashes, the NT hash among
  them, which is as good as the password for an SMB login. `truenas_user` asks for named fields alone and never reads
  a hash, so it cannot see whether a password changed: it sets one when it creates the user, when TrueNAS needs it
  again (an update that turns `smb` on, or `password_disabled` off), and when `truenas_users_reset` names the user,
  a one-run switch for a rotation, and it needs one only then, so a login it only closes needs none. With password
  history on, TrueNAS refuses a reset to a password the user had before. Neither the password nor the echo of it
  that `user.create` and `user.update` return comes back from the module; a run that fails part-way says what it had
  already done. A user's `uid` is pinned at creation and an existing user with another one is
  refused, since TrueNAS cannot change it: a reinstalled NAS that imports the pools gives the files back to the same
  number.
- **A share's directory gets one owner and a mode, no ACL.** A dataset made with the SMB preset (`share_type: SMB`)
  gets TrueNAS's template ACL, in which every SMB login may modify it. `truenas_permission` reads the directory
  (`filesystem.stat`) and, when its owner, group or mode differs or it carries an ACL beyond its mode, sets the three
  with the ACL stripped (`filesystem.setperm`, `stripacl`), on the directory alone, never what is below it. Before
  connecting it refuses a path that is not a directory inside a pool, and a mode that is not a string from `001` to
  `777`, as TrueNAS does (YAML would read an unquoted `0770` as 504); in a dry run, a directory that does not exist yet
  must be the mount point of a dataset the run creates, or it is refused as a mistyped path.
- **An SMB share's purpose and options go to an update together, or not at all.** `sharing.smb.update` refuses the
  options without the purpose, and fills the options in from their defaults when the purpose comes alone
  (`normalize_legacy_fields`), which would quietly reset them; so `truenas_resource`'s `update_together` sends both
  when one differs, the options as they are with the wanted ones laid over them, and neither when only another field
  does (a share disabled by hand). Laying them over keeps the Time Machine volume's `vuid`, which names the volume in
  its Bonjour record and must not change. A new purpose takes the entry's options alone: the current ones are the old
  purpose's, and TrueNAS refuses keys the new one does not have.
- **Snapshots only where data cannot be re-created.** Here, `persistent/proxmox/vm` keeps hourly snapshots for a day
  and daily for a week (it holds the worker's data disk, whose scratch data longer-lived snapshots would pin, and the
  control plane's system disk; the nightly VM backup keeps the history); with a cluster, `persistent/nextcloud` and
  `persistent/photoprism` keep hourly for 2 days, daily for 30, weekly for 12 weeks. The daily and weekly tasks allow
  empty snapshots: they fire with an hourly one, and TrueNAS destroys a batch of snapshots that are all empty unless
  one of its tasks allows them, so an idle dataset would otherwise keep no daily or weekly snapshot at all; the hourly
  tasks still skip them. Never a snapshot task on the ephemeral pool, nor on the backups' dataset, which holds
  backups already.

## This repo's NAS

`truenas.yml` runs the role on the `nas` host. `group_vars/nas/connection.yml` says how it is reached (the key from the
vault's `vault_truenas_api_key`; the static address, then `truenas_bootstrap_address`), `group_vars/nas/truenas.yml`
holds the NAS's configuration, and `group_vars/all/storage.yml` the VM storage layout, which the Proxmox play reads too,
so a share and the storage that mounts it cannot drift apart. Addresses, the certificate pin and the pools' disk serials
are `local.yml`'s. The play's pre-tasks, tagged `always`, refuse `nas_section_error`, `kubernetes_settings_error` and
`timemachine_settings_error`, and when `local.yml` gives the NAS neither address they say so and end the host before the
role runs (`always`, since `meta` tasks honour tags; the `debug` that says why is what gives the host a recap line). SSH
stays off on the NAS, as does every sharing service but NFS, and SMB while no Mac backs up to it (below).

**Pools.** The two pools are named by class, never by hardware (`nas_pools` in `group_vars/all/storage.yml`;
`local.yml` can name them otherwise with `truenas_pool_persistent` and `truenas_pool_ephemeral`): `persistent` is
redundant (RAIDZ1), for data that cannot be re-created, and `ephemeral` has no redundancy (a single disk), for data
that can. Both have auto-trim on and TrueNAS's default scrub made explicit (Sundays at midnight, threshold 35 days),
so a rebuild matches, and the system dataset stays on the boot pool, off the single-disk one.

**Datasets.**

| Dataset | Holds | Properties |
|---|---|---|
| `persistent/proxmox` | the persistent VM storage's parent | LZ4, `atime` off, POSIX ACLs (`aclmode` DISCARD) |
| `persistent/proxmox/vm` | the persistent VMs' disks (`homeserver-persistent`) | `sync=standard`, 64K records, quota `vm_storage_persistent_quota_gib` (1 TiB) |
| `persistent/proxmox/backup` | the nightly VM backups (`homeserver-backup`) | `sync=standard`, 1M records |
| `ephemeral/proxmox` | the ephemeral VMs and the project pools | LZ4, `atime` off, POSIX ACLs, quota 1 TiB, reservation `vm_storage_ephemeral_reservation` |
| `ephemeral/proxmox/vm` | the ephemeral VMs' disks (`homeserver-ephemeral`) | `sync=disabled`, 64K records, refreservation `kubernetes_ephemeral_disks_gib` |
| `ephemeral/proxmox/<project>` | one project's VMs (`vm_storage_consumers`) | `sync=disabled`, 64K records, an optional quota |
| `persistent/nextcloud`, `persistent/photoprism` | with a cluster: Nextcloud's files, PhotoPrism's originals | `sync=standard`, 1M records |
| `ephemeral/frigate` | with a cluster: camera recordings | `sync=disabled`, 1M records, quota 500G |
| `ephemeral/jellyfin` | with a cluster: the media library, from before this repo | `create_only`: properties and NFSv4 ACLs left alone |
| `persistent/timemachine` | with a Mac: the Macs' Time Machine backups' parent | SMB preset, quota every Mac's `quota_gib` together |
| `persistent/timemachine/<name>` | each Mac's backup (`timemachine_backups`) | SMB preset, `sync=standard`, quota `quota_gib`, refquota three quarters of it |

- The persistent VMs keep synchronous writes synchronous. Their quota counts their snapshots, so a disk that fills
  never fills the pool under the backups, which share it; a thin qcow2 disk only grows.
- The ephemeral VMs accept losing the last seconds of writes in a crash. The 1 TiB quota sits on `ephemeral/proxmox`
  because the pool also holds the media dataset: it stops a runaway VM filling the pool. Never a snapshot task there:
  snapshots would keep deleted VMs' data.
- `vm_storage_ephemeral_reservation` puts a ZFS `reservation` on `ephemeral/proxmox`, which counts its descendants, so
  the media can never fill the pool under the VMs; a `refreservation` would not, since it excludes descendants.
- With a NAS and a cluster, `ephemeral/proxmox/vm` itself carries a `refreservation` of the cluster's disks on it (the
  worker's system disk), which neither the media nor the project pools can then take, since a worker whose disk
  cannot grow pauses with every app.
- The reservation, the refreservation and every quota of a dataset the role manages are always sent, 0 for none,
  so one removed from the configuration is removed on the NAS too; the `create_only` media dataset is left alone.
- 64K records match qcow2's cluster size and waste no padding on the RAIDZ1; POSIX ACLs, which TrueNAS pairs with
  `aclmode` DISCARD, suit root-owned disk images served to a Linux client. The cluster's datasets hold large files,
  hence 1M records.

**A pool per project.** A project that creates VMs on the Proxmox host gets a pool of its own: an entry in
`vm_storage_consumers` makes a dataset under `ephemeral/proxmox` with the ephemeral class's properties and an optional
quota, exported and mounted under the project's id. The project is told that id and nothing else, and its token is
granted `/storage/<id>` by hand with its own README's line; this repo manages no Proxmox users or ACLs, and never
grants `Datastore.Allocate`, which Proxmox checks on `/storage` for creating, changing and removing any storage alike.
Proxmox has no host-wide default storage, so a project always names its storage; its default, `local-lvm`, is right
without a NAS. The projects stay independent: nothing here reads another's configuration, and nothing there reads this
repo's.

**NFS.** NFSv3 is on beside NFSv4, since Proxmox checks an NFSv4 storage with `rpcinfo`, which needs the port mapper
NFSv3 brings. Only requests from privileged ports are taken (`allow_nonroot: false`, TrueNAS's default), so no
unprivileged process on a client can speak NFS. The VM and backup shares go to the Proxmox host alone, the cluster's
(`kubernetes_nas_datasets`, `group_vars/all/kubernetes.yml`) to the worker alone, all with root mapped to root: each
app's pod makes its own mount point uid 1000.

**Time Machine.** Each Mac in `local.yml`'s `timemachine_backups` (`{name, quota_gib, uid}`, optionally `retired`) gets,
from `group_vars/nas/timemachine.yml`, a dataset under `persistent/timemachine` made with the SMB preset (the only
moment case-insensitivity can be chosen) with its quota, a refquota of three quarters of it and `sync=standard`, under a
parent whose quota is every Mac's together (TrueNAS's mailed quota alert watches that one, since a Mac's own dataset has
a smaller refquota), a weekly snapshot task kept eight weeks, an SMB-only login `tm-<name>` with its pinned uid and the
vault's password (`vault_timemachine_passwords`), its directory owned by that login alone with the preset's template ACL
stripped, and a Time Machine share `tm-<name>` with `auto_snapshot` (Samba snapshots the dataset a minute after a
completed backup, but not within 15 minutes of its last, and deletes them after a week when the Mac next connects, as
long as more than three newer remain) and a volume id derived from the Mac's name, so a rebuilt NAS announces the volume
the Mac knows. With a Mac, SMB requires transport encryption and has the Apple extensions on, SMB1, NTLMv1, multichannel
and the master-browser election off, and its auxiliary parameters and admin group pinned to TrueNAS's defaults, since
either set by hand would undo the rest; each share refuses the Proxmox host's address, which every tailnet client
reaches the LAN as, and pins its allow list empty, which would override that; the NAS announces itself over mDNS alone;
the API key's own user is taken out of SMB; and the SMB stage starts the service, which the services stage keeps stopped
otherwise. The Mac's own Time Machine limit, about half the quota, is the working limit. The refquota counts the disk
image alone and is the disk the Mac sees; the quota counts the image and its snapshots, whose room is the rest. Should
the snapshots fill it, writes fail inside an image that still shows free space, and a smaller limit on the Mac frees
nothing they hold: `group_vars/nas/timemachine.yml` says why, and the runbook what to do. Samba reports the dataset's
own statvfs here: its ZFS space hook (`zfs_core:zfs_space_enabled`) is off by default and TrueNAS never sets it. A
retired Mac keeps its dataset and snapshots, its login closed (out of SMB, locked, its password disabled), its share and
task disabled; with no Mac left backing up, the SMB stage stops SMB, and no login check stands in the way of that.
`group_vars/all/timemachine.yml`'s `timemachine_settings_error`, which `truenas.yml` asserts before the role runs,
refuses a bad name, quota or uid, a uid or name given twice, a key an entry does not have (a misspelt `retired`), an
active Mac without a password of 20 characters, a password for a Mac not listed, a vault key that is no Mac's name,
which it does not print since it may be a password typed as a key, and `timemachine_present` given with `-e`;
`nas_section_error` refuses the setting without a NAS. The Mac's side is by hand: `scripts/runbooks/timemachine.md`.

**Alerts.** When `local.yml` names a mail server, the NAS mails warnings and worse (a degraded pool, a pool filling
up, a failed snapshot task) to `truenas_alert_email`, the password from the vault's `vault_truenas_mail_password`.
`nas_section_error` refuses what TrueNAS would refuse: a mail server without a sender address, and an alert address
without a mail server.

## Tests

`tests/integration/test_idempotence.py` runs `truenas.yml` with this repo's configuration against
`tests/integration/fake_truenas.py`, a stateful TLS JSON-RPC server for TrueNAS 25.10. Its shapes were checked against
TrueNAS 25.10.7 and its middleware source; the periodic snapshot tasks, mail and alert services, `alert.list` and the
pools' health, capacity and scan follow the API v25.10 reference (api.truenas.com) rather than a capture. It refuses
what the real API refuses where a non-idempotent role would trip over it (creating what exists, importing an imported
pool, POSIX ACLs without `aclmode` DISCARD, non-consecutive name servers, a snapshot task for a missing dataset,
`alertservice.update` without the whole service), and answers any other method with an error, so a new API method
needs the fake to learn it or the test fails. The test runs a dry run that changes nothing, an apply, a second apply
and a dry run that change nothing, then drift in every stage corrected once, a new alert address, the media dataset
lost and made again, a reservation removed, and a rotated mail password; neither the private key, the API key nor a
mail password may appear in any run's output. Other tests run it without a cluster, with only the role's defaults
(nothing changes), and with a wrong pin (no login is tried) or none (nothing is asked, in a dry run too).

`test_truenas_role_backs_up_a_mac` runs `truenas.yml` with a Mac in `timemachine_backups`: the services stage alone
starts nothing, then a dry run, an apply, a second apply and a dry run, the Mac retired (SMB stopped, nothing deleted),
and its entry deleted, which nothing refuses. `tests/policy/test_roles.py` checks each of the section's refusals and
what the NAS is handed per Mac, retired or not.

The SMB stage runs from `tests/integration/truenas_smb.yml`, the role with variables of its own: a Time Machine share
for one Mac on a pool the run imports. The fake's SMB service, users, SMB shares and directory permissions follow
TrueNAS 25.10.7's middleware source rather than a capture, with its refusals (an update's options without its purpose, a
Time Machine share without the Apple extensions or mDNS, an SMB user without a password, SMB turned on without one, a
mode outside 001 to 777), and it is stricter in two places: an update's purpose without its options is refused, where
TrueNAS fills them in from their defaults, and an update whose Time Machine options name no `vuid` gets a new one. On
the real NAS the stage has run as a dry run only, so far (#142's, against the home NAS). The test runs a dry run, an
apply, a second apply and a dry run, then drift corrected once (a share disabled, an option changed, the directory's
owner and ACL, the API key's user an SMB login again), a password changed in the vault alone (nothing sent) and then
reset, and the login and share closed and opened again; no password, password hash or API key may appear in any run's
output. Other tests show that an SMB login the role does not list stops the stage before it changes anything, in a dry
run and under the stage's tag too, that `truenas_smb_closed_users` closes it and SMB then starts, that a login made
while SMB runs is refused saying so while a run that stops SMB goes through, that a missing reading of the logins is
refused, that a login the users task could not take out of SMB is caught by the check just before the service, that a
string `false` for `truenas_api_user_smb` is refused, and that the services stage alone never starts SMB and naming
`cifs` there is refused before the NAS is asked anything. `tests/unit/test_truenas_client.py` shows that `truenas_user`
never returns a password or a hash, even from a NAS that ignored its `select`, needs a password only when it sends one,
closes a named login, returns the SMB logins after its changes, says what it had done when it fails part-way, and
refuses every case it cannot manage before or after connecting; and that `truenas_permission` refuses a dry run's
missing directory that no dataset of the run makes, and a `setperm` that did not take.

The fake always answers, so `tests/unit/test_truenas_client.py` covers in process, with a scripted socket and a clock
that never waits, what it never does: TrueNAS refusing a login or a call, rate-limiting logins, dropping the
connection, failing or never finishing a job, and refusing an interface commit, which must discard the saved change,
or never seeing the check-in, after which TrueNAS rolls the address back by itself. `tests/integration/test_status.py`
reads the NAS through the `status` entry point: one login, nothing changed, no secret in the report. The fake cannot
prove the real NAS behaves the same, so run `just check truenas` against it first.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
