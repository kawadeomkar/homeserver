# Time Machine backups of the Macs on the NAS

Each Mac listed in `local.yml`'s `timemachine_backups` backs up with Time Machine to a share of its own on the NAS,
over SMB on the LAN. `just deploy truenas` makes, per Mac, a dataset on the redundant pool
(`persistent/timemachine/<name>`, with the quota given), a weekly snapshot task kept eight weeks, an SMB-only login
`tm-<name>` with the uid given, its directory owned by that login alone (0700), and a Time Machine share `tm-<name>`
whose own option snapshots the dataset a minute after each completed backup, kept a week. It turns SMB on with the
Apple extensions and transport encryption required, announces the NAS over mDNS alone, and takes the API key's own
user out of SMB first: `roles/truenas/README.md` says why each, and refuses to start SMB while an SMB login the role does
not list exists.

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

   `just init` asks for it with a NAS. The quota counts the snapshots and reserves nothing; give the Mac itself a limit
   of about half of it (below), so the room between is the snapshots'.
2. **Its SMB password in the vault**, 20 characters or more, long and random (`openssl rand -base64 30`), and in your
   password manager: `just vault`, then

   ```yaml
   vault_timemachine_passwords:
     laptop: <password>
   ```

   The run refuses a Mac without one, and a password for a name `timemachine_backups` does not list.

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
   `sudo tmutil setquota <destination id> <GB>` (the id from `tmutil destinationinfo`): 1000 GB for a 2048 GiB quota.
   Time Machine thins its history there, and the NAS's quota is the backstop.
4. Let the first backup finish, then let one hourly backup run by itself: a known failure of macOS 26.4 shows only in
   the backups Time Machine starts on its own (below).

## Checking it

- On the Mac: `tmutil destinationinfo` names the destination; `tmutil listbackups` lists the backups; bring one file
  back through Time Machine's own window.
- `just status nas` shows each Mac's backup on its `Macs` line: when the newest completed, its use against its quota,
  and what its snapshots hold. A backup ten days old or more is a thing to do, and so is one never completed.
- On the NAS, read-only: the dataset's snapshots (Datasets, the Mac's dataset, Snapshots) show an `aapltm-<epoch>`
  within two minutes of each completed backup, and a `weekly-` one each Sunday.
- **Over the first months**, until the disk image has grown to the Mac's limit and for eight weeks after: the dataset's
  space used by snapshots (`usedbysnapshots`: `just status nas`, or the dataset's page) stays well inside the room
  between the Mac's limit and the quota. Once the image is full, each backup overwrites blocks the snapshots keep for
  up to eight weeks. If it nears the quota, raise `quota_gib` and `just deploy truenas`, or lower the Mac's limit.
  TrueNAS's quota alerts come first, and `just status` shows them, and its own at 80% of the quota.

## Rotating the SMB password

The role sets a login's password when it makes the login, and after that only when told: it never reads the one
TrueNAS holds, whose hash is as good as the password for SMB.

1. Put the new password in the vault (`just vault`) and in your password manager.
2. `just deploy truenas -e '{"truenas_users_reset": ["tm-<name>"]}'`: that run sends it, and says so as
   `<secret, changed>`.
3. On the Mac, the next backup asks for the new password; or update the NAS's entry in Keychain Access.

## A new or erased Mac taking over a backup

Restore it with Migration Assistant from the Time Machine backup, or, on a Mac set up by hand, add the same disk (step 2
above) and, when Time Machine offers, keep the existing backup; `sudo tmutil inheritbackup <the .sparsebundle's
path>` does the same from the command line, with the share mounted. The share's volume id is derived from the Mac's
name, so a NAS rebuilt from scratch announces the same volume.

## Rolling back after a damaged disk image

Time Machine verifies a network backup now and then, and when it finds the disk image damaged it says it "must create a
new backup", which would throw the history away. The snapshots are the answer:

1. On the Mac, turn automatic backups off, and wait for any backup to stop.
2. In TrueNAS's web UI, roll the Mac's dataset back to its newest `aapltm-` snapshot, taken with the image at rest, or
   to a weekly one if the damage is older. This is the one change to the NAS made by hand: it is an operation on data,
   not configuration, and the role never destroys anything.
3. Turn automatic backups on again, and back up.

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
   `just deploy truenas`. Its login is taken out of SMB, locked and left without a password, its share and weekly task
   are disabled, and its dataset and snapshots stay as they are. With no Mac left backing up, SMB stops. Its password
   can then leave the vault.
2. The entry can stay, or go: once retired, its login is no SMB login, so deleting it is refused by nothing.
3. **Deleting the backup itself** is by hand, as `teardown.md` does for the cluster's datasets, since the role never
   deletes: once you no longer want it, remove in TrueNAS's web UI the share (Shares, SMB), the snapshot task (Data
   Protection), the login (Credentials, Users) and the dataset with its snapshots (Datasets), in that order. Remove the
   entry from `local.yml` first, or the next run makes them again.
