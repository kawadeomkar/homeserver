# Time Machine backups of the Macs on the NAS

Each Mac listed in `local.yml`'s `timemachine_backups` backs up with Time Machine to a share of its own on the NAS, over
SMB on the LAN. `just deploy truenas` makes, per Mac, a dataset on the redundant pool (`persistent/timemachine/<name>`,
with the quota given and a refquota of three quarters of it), a weekly snapshot task kept eight weeks, an SMB-only login
`tm-<name>` with the uid given, its directory owned by that login alone (0700), and a Time Machine share `tm-<name>`
whose own option snapshots the dataset after completed backups (below), and which refuses SMB from the Proxmox host's
address. It turns SMB on with the Apple extensions and transport encryption required, announces the NAS over mDNS alone,
and takes the API key's own user out of SMB first: `roles/truenas/README.md` says why each. The role refuses to start
SMB while an SMB login it does not list exists (below).

The Mac encrypts its backup itself, with a password that never leaves it and your password manager, so whoever can
read the NAS cannot read the backup. The SMB login protects the backup's existence, not its contents.

Not from the tailnet or a tunnel: SMB answers on the LAN alone. Away from home a Mac keeps its hourly local snapshots
and backs up when it is home again.

## Before the first run

1. **The Mac in `local.yml`**, with its name (lower-case letters, digits and hyphens, up to 16), its dataset's quota
   in GiB and its login's uid (3000 or more, a number of its own, which a rebuilt NAS gives it again):

   ```yaml
   timemachine_backups:
     - {name: laptop, quota_gib: 2048, uid: 3101}
   ```

   `just init` asks for it with a NAS. The quota counts the disk image and its snapshots, and reserves nothing. The
   Mac sees a disk of three quarters of it, the refquota, which counts the image alone; give the Mac itself a limit of
   about half the quota (below), and the rest of the quota is the snapshots' room.
2. **Its SMB password in the vault**, 20 characters or more, long and random (`openssl rand -base64 30 | pbcopy`, which
   prints nothing, or your password manager's generator), and in your password manager: `just vault`, then

   ```yaml
   vault_timemachine_passwords:
     laptop: <password>
   ```

   The run refuses a Mac without one, and a password for a name `timemachine_backups` does not list.
3. **If TrueNAS requires two-factor authentication** for its logins (Credentials, Two-Factor Authentication), the API
   key's user needs it set up too: TrueNAS otherwise refuses the run's change to that user, taking it out of SMB, and a
   dry run cannot show that.

## The run

```bash
just check truenas      # what it would make, change and start
just deploy truenas
just deploy truenas     # changes nothing
```

The dry run shows, and nothing else: the announcements (mDNS on, NetBIOS and WS-Discovery off), the
`persistent/timemachine` dataset and the Mac's, the weekly snapshot task, the SMB settings, the API key's user out of
SMB, the login, its directory's owner, the share, and the SMB service enabled and started by the SMB stage.

## On the Mac

1. On mains power and, for the first backup, on Ethernet: the first backup copies everything.
2. System Settings, General, Time Machine, Add Backup Disk: choose `tm-<name>` on the NAS (it is announced over
   Bonjour) and log in as `tm-<name>` with the vault's password; let the keychain keep it. Turn **Encrypt Backup** on
   with a password of its own, and put that password in your password manager: without it nothing restores. Use the
   settings window, not `tmutil setdestination`: only the window offers encryption, and a network backup made without
   it must be erased to gain it.
3. Set the Mac's own limit, about half the quota, in the same dialog's "maximum space used for backups", or with
   `sudo tmutil setquota <destination id> <GB>` (the id from `tmutil destinationinfo`; `tmutil` needs a Terminal with
   Full Disk Access, in System Settings, Privacy & Security): 1000 GB for a 2048 GiB quota.
   Time Machine thins its history there. Should the image pass 80% of the refquota (1229 GiB for that quota), TrueNAS
   raises an alert: the Mac's limit is then missing or too high.
4. Let the first backup finish, then let one hourly backup run by itself: a known failure of macOS 26.4 shows only in
   the backups Time Machine starts on its own (below).

## Checking it

- On the Mac, in a Terminal with Full Disk Access: `tmutil destinationinfo` names the destination, and `sudo tmutil
  listbackups` lists the backups; bring one file back through Time Machine's own window. `tmutil destinationinfo` does
  not say whether the backup is encrypted: while a backup runs, `diskutil apfs list` shows its volume as FileVault: Yes.
- `just status nas` shows each Mac's backup on its `Macs` line: when the newest completed, its use against its quota,
  and what its snapshots hold. A backup ten days old or more is a thing to do, and so is one never completed or a Mac
  with no share yet; an active Mac whose share is disabled or whose dataset is missing is a problem.
- On the NAS, read-only: the dataset's snapshots (Datasets, the Mac's dataset, Snapshots) show an `aapltm-<epoch>`
  about a minute after a completed backup, though none within 15 minutes of the one before, and a `weekly-` one each
  Sunday. Samba deletes its `aapltm-` snapshots older than a week when the Mac next opens the backup, as long as more
  than three newer remain, so a Mac that stays connected for days keeps more; TrueNAS alerts at 512 snapshots of a
  share.
- **Over the first months**, until the disk image has grown to the Mac's limit and for eight weeks after: the dataset's
  space used by snapshots (`usedbysnapshots`: `just status nas`, or the dataset's page) stays well inside the room
  between the Mac's limit and the quota. Once the image is full, Time Machine writes each backup into the space it
  thins, and the snapshots keep every block it overwrites for up to eight weeks. Should they take the rest of the
  quota, writes fail inside a disk image that still shows free space, which can damage it. If the dataset nears its
  quota, raise `quota_gib` and `just deploy truenas`, or keep the weekly snapshots fewer weeks (`lifetime_value` in
  `group_vars/nas/timemachine.yml`, a change to the repo). Lowering the Mac's limit frees nothing the snapshots hold.
  TrueNAS raises no quota alert for these datasets, since their refquota is smaller: `just status` is the one that
  warns, a thing to do at 80% of the quota and a problem at 90%.

## Rotating the SMB password

The role sets a login's password when it makes the login, and after that only when told: it never reads the one
TrueNAS holds, whose hash is as good as the password for SMB.

1. Put the new password in the vault (`just vault`) and in your password manager.
2. `just deploy truenas -e '{"truenas_users_reset": ["tm-<name>"]}'`: that run sends it, and says so as
   `<secret, changed>`.
3. On the Mac, automatic backups now fail rather than ask: start one with Back Up Now, which asks for the new password,
   or update the NAS's entry in Keychain Access, which may be in the System keychain.

## A new or erased Mac taking over a backup

Restore it with Migration Assistant from the Time Machine backup, or, on a Mac set up by hand, add the same disk (step 2
above) and, when Time Machine offers, keep the existing backup; `sudo tmutil inheritbackup <the .sparsebundle's
path>` does the same from the command line, with the share mounted, and may need `tmutil setdestination` and
`tmutil associatedisk` too (`man tmutil`). A Mac that will not start restores from macOS Recovery: Restore from Time
Machine, the NAS's share, the `tm-<name>` login and its password, then the backup's own encryption password. The
share's volume id is derived from the Mac's name, so a NAS rebuilt from scratch announces the same volume.

## A lost or stolen Mac

Its keychain holds the SMB password. Retire it (below), or, to go on backing up from a replacement under the same
name, put a new password in the vault and reset the login (rotating, above). Never lock the login in TrueNAS's web UI
instead: an active Mac's login is wanted unlocked, so the next `just deploy truenas` would unlock it.

## Rolling back after a damaged disk image

Time Machine verifies a network backup now and then, and when it finds the disk image damaged it says it "must create a
new backup", which would throw the history away. Answer **Back Up Later**: the snapshots hold earlier states of the
image. A rollback to anything but the newest snapshot destroys every snapshot after it, so prove a snapshot good before
rolling back to it. The clone, its share and the rollback are changes made by hand in TrueNAS's web UI, which the rule
allows for what the role never does (CLAUDE.md): it never rolls back or deletes.

1. On the Mac, turn automatic backups off, wait for any backup to stop, and eject the share in Finder.
2. Pick a candidate (Datasets, the Mac's dataset, Snapshots): the newest `aapltm-` snapshot, taken with the image at
   rest; after that, older ones in turn, `aapltm-` or weekly. Damage is often older than its discovery.
3. Check it without touching the backup: clone it (the snapshot's Clone to New Dataset) to
   `<persistent>/timemachine/<name>-check`, add an SMB share of that path, `tm-<name>-check`, with the default purpose,
   and on the Mac connect to it (Finder, Go, Connect to Server, `smb://<nas>/tm-<name>-check`) as `tm-<name>`. Open the
   `.sparsebundle` with the backup's encryption password, run First Aid on its volume in Disk Utility, and eject it.
4. Delete the check share, then the clone. If First Aid found the image sound, roll the Mac's dataset back to that
   snapshot: the newest needs nothing more, an older one needs the option that destroys the newer snapshots. If not,
   go back to step 2 with the next older one.
5. Turn automatic backups on again, and back up.

## Restoring from a snapshot without a rollback

Files from a state Time Machine has since thinned come back the same way, with nothing rolled back: clone the snapshot
and share the clone as in step 3 above, open its `.sparsebundle`, and browse the backups' folders in Finder, or point
Migration Assistant at it for a whole Mac. Delete the share, then the clone, afterwards.

## When automatic backups fail with "error 80"

macOS 26.4 broke automatic backups to Samba servers: they fail with "incorrect username or password" while a backup
started by hand works. It was reported fixed in macOS 26.5, and reported back since. To see whether a Mac has it:

```bash
log show --last 2h --predicate 'process == "backupd" OR process == "NetAuthSysAgent"' | grep -i "error: 80"
```

The workaround the report describes, an entry for the server in root's `serverMarkers.plist`, is at
https://porotnikov.com/post/mac/2026-04-07-time-machine-backup-failure-linux-samba-known-server-marker/ ; use it only
when those log lines are there, and keep the local disk's backups going meanwhile.

A sporadic kernel panic in `smbfs` during backups was also reported, on macOS 26.6.2 against this TrueNAS release: if
one shows, report it to Apple, set Time Machine to back up daily, and lean on a local disk until an update.

## Retiring a Mac

1. Add `retired: true` to its entry in `local.yml` (`{name: laptop, quota_gib: 2048, uid: 3101, retired: true}`) and
   `just deploy truenas`. Its login is taken out of SMB, locked and its password disabled (TrueNAS keeps the hash,
   unused), its share and weekly task are disabled, and its dataset and snapshots stay as they are. With no Mac left
   backing up, the SMB stage stops SMB, and no login check stands in the way of that. Its password can then leave the
   vault.
2. The entry can stay, or go: once retired, its login is no SMB login, so deleting it is refused by nothing.
3. **Deleting the backup itself** is by hand, as `teardown.md` does for the cluster's datasets, since the role never
   deletes and the rule allows by hand what it never does (CLAUDE.md): once you no longer want it, remove in
   TrueNAS's web UI the share (Shares, SMB), the snapshot task (Data Protection), the login (Credentials, Users) and
   the dataset with its snapshots (Datasets), in that order. Remove the entry from `local.yml` first, or the next run
   makes them again.

## When the deploy refuses an SMB login

While a Mac backs up, `just deploy truenas` refuses to start or keep SMB while a local login it does not know has SMB
on, since that login could log in over the network; the message names it and says whether SMB runs already.

- **A `tm-<name>` login**: a Mac whose entry left `local.yml` while it still backed up. Put the entry back with
  `retired: true`, which closes its login.
- **Any other**, such as a user made in TrueNAS's web UI, which makes every new user an SMB login unless told: name it
  in `local.yml`'s `truenas_smb_closed_users: [<name>]` and deploy. It is taken out of SMB and otherwise left alone.
- **To stop SMB meanwhile**, retire every Mac: a run that stops SMB is never refused.
