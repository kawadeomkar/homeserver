# The OPNsense router, through its API

`just deploy router` reaches the router through its HTTPS API from this machine (`roles/opnsense`). It checks the
router is the one the certificate pin names, accepts the key pair and runs a release the role supports, and reports
that release and its plugins. Then it backs the router's configuration up whenever it changed, and on request
restores a backup, onto the router or onto a fresh install: the configuration file is the source of truth for
rebuilding the router. This runbook covers what only the router's web UI or console can do (the role's user and its
key pair, the certificate pin, updating the router), and the backups and restores.

Values are named by their variable in `group_vars/all/local.yml`; examples use RFC 5737 placeholders, `192.0.2.1` for
the router (`opnsense_address`).

## 1. A user and key pair for the role

In the web UI, open System → Access → Users and add a user of its own for this project:

- **Username:** `homeserver`, or any name that says what it is for.
- **Password:** a long random one, kept nowhere: nobody logs in as this user. Tick "Generate a scrambled password" if
  the form offers it.
- **Login shell:** none.
- **Privileges:**
  - "System: Firmware" (`page-system-firmware-manualupdate`), which covers `api/core/firmware/*`: the release the
    role checks. OPNsense has no narrower one: the same privilege can also update and reboot the router.
  - "Diagnostics: Configuration History" (`page-diagnostics-configurationhistory`), which covers
    `api/core/backup/*`: the history the backups read and download. It can also revert to and delete history entries.

  So treat the key pair as an administrator's credential. Later stages of the role add the privileges they need here.

Save, then under the user's "API keys" add a key. The browser downloads a file holding `key=` and `secret=` lines,
once. Put both in the vault, `just vault` (or `just init`), as `vault_opnsense_api_key` and
`vault_opnsense_api_secret`, then delete the file.

To revoke the pair, delete the key under the same user; a new one goes in the vault the same way. The user and its key
are part of the router's configuration, so a restore brings them back with it (section 6).

## 2. The certificate pin

The key pair goes only to a server presenting the certificate `opnsense_api_cert_sha256` names. Read its SHA-256 from
this machine, at home:

```bash
openssl s_client -connect 192.0.2.1:443 </dev/null 2>/dev/null | openssl x509 -noout -fingerprint -sha256
```

Compare it with the web UI's certificate, System → Trust → Certificates (the one System → Settings → Administration
names as the web GUI's), and set it in `local.yml`, beside `opnsense_address`:

```yaml
opnsense_address: 192.0.2.1
opnsense_api_cert_sha256: "AA:BB:...:99"
```

Replacing the web certificate means updating the pin: until then every run stops with "presented certificate SHA-256
…, not the pinned one; the API key was not sent". A restore brings the backup's certificate back, so the pin holds
across a reinstall.

## 3. Updating the router

The role runs only against OPNsense 26.7.2 or later (`opnsense_min_version`): the first release with
FreeBSD-SA-26:52's WireGuard fix, in the series whose API the role was written against. It never updates the router;
that is done here, from home, at a quiet time, since a major upgrade reboots it and takes the internet down for a few
minutes.

1. System → Firmware → Status → "Check for updates". Install what it offers and reboot when it asks.
2. A major release is offered only from the latest minor release of the series before it (26.7 from 26.1.11, say).
   Once that is installed, the Status tab offers the major upgrade: read its notes, run it, and reboot.
3. Check for updates again, and install the latest minor release of the new series.
4. Check the router still does what it did: the firewall rules (a major upgrade can move them, as 26.7 moved the
   legacy rules page to the `os-firewall-legacy` plugin), DHCP and DNS.

Never trust the "latest version" the firmware page's status shows before a check: it was stale on this router
twice, naming an older release than the check then found.

## 4. The backups

Every save on the router makes a revision of its configuration, and its configuration history keeps the last 100
(Diagnostics → Configuration History). Every `just deploy router` compares the newest of them, the configuration the
router runs, with the newest backup on this machine, by its history id, and:

- backs it up when there is no backup yet, or when the router has changed since the newest backup (whose revision is
  still in its history);
- does nothing when the router runs the newest backup's revision;
- stops when the router's history lacks the newest backup's revision, changing nothing: the router was restored or
  reinstalled by hand since, or it made more than 100 revisions since the last run. `-e opnsense_restore=latest`
  puts the backup back on it (section 6); `-e opnsense_backup_accept=true` keeps the router's configuration as the
  newest backup instead.

`just check router` says what it would back up and writes nothing.

The backups go to `~/.opnsense/homeserver` (`router_backup_dir` in `local.yml` for another place, outside the
repository), mode 0700, each named after its revision's history id (`config-<time>.xml`) and never deleted. Each is
encrypted in OPNsense's own format (AES-256-CBC, PBKDF2-SHA512, 100000 iterations), with the password in the vault as
`vault_opnsense_backup_password`: the router's own Restore page opens them with it, and so does `openssl enc -d
-aes-256-cbc -md sha512 -pbkdf2 -iter 100000` on the base64 between the `----` lines. Make it long and random, and
keep it, and a copy of the backups directory, in your password manager: nothing else opens them, and a backup that
lives only on this machine dies with it.

A run backs up only what it finds, so a change made in the web UI reaches a backup at the next `just deploy router`.
Run it after changing the router by hand.

## 5. The run

1. Set `opnsense_address` and `opnsense_api_cert_sha256` in `local.yml`, and the key pair and the backups' password in
   the vault.
2. `just check router`. Expect `OPNsense <version> at 192.0.2.1`, its kernel and base sets and its plugins, and "Would
   back up config-<time>.xml". An older router stops with "The router runs OPNsense 26.1.2; this role needs 26.7.2 or
   later": update it (section 3) and run it again.
3. `just deploy router` makes the first backup. A second run says "The newest backup is the revision the router runs".

An inventory made before the router group was added has none, and the run says to add `[router]` and `opnsense` to
`inventory`, as in `inventory.example`.

## 6. Restoring

A restore is never automatic: `-e opnsense_restore=latest` restores the newest backup, `-e
opnsense_restore=config-<time>.xml` a given one. OPNsense's API cannot upload a configuration, so the role logs in to
the web UI as root, with `vault_opnsense_root_password`, and restores it on the Restore page (System → Configuration →
Backups) as a person would: the whole file, the history flushed, the console settings kept, then a reboot, which takes
the internet down for a minute or two. It uploads the backup still encrypted, with its password. `just check router
-e opnsense_restore=latest` logs in, checks the backup's password and says what it would restore, restoring nothing.

**Onto the router** (it answers at `opnsense_address` with the pin and the key pair), to undo changes: the role backs
up what the router runs first, so nothing is lost, and restores nothing when the router runs that backup already.

**Onto a fresh install**, after a disk or the whole router died:

1. Install the same release as the backup or a newer one (the backup's first lines name its release: `head -2
   ~/.opnsense/homeserver/<newest>`), on the same hardware or one with the same network interfaces. In the installer,
   give root the password in the vault (`vault_opnsense_root_password`).
2. Connect this machine to the fresh router's LAN port. It answers on its factory LAN address (the console shows it)
   with a new certificate of its own and hands out addresses by DHCP. Set `opnsense_bootstrap_address` in `local.yml`
   to that address if it is not the router's usual one.
3. `just deploy router -e opnsense_restore=latest`. The router does not answer as the one known at `opnsense_address`,
   so the run reads the certificate the fresh install presents, prints its SHA-256 and stops, having sent nothing.
   Compare it with the one a browser shows at `https://<its address>`.
4. `just deploy router -e opnsense_restore=latest -e opnsense_bootstrap_cert_sha256=<it>`. The run logs in as root,
   restores the backup, and waits up to 15 minutes for the router to come back at `opnsense_address` with its old
   certificate and API key, which the backup holds. This machine may need its DHCP lease renewed meanwhile, since the
   LAN changes back to the usual one (reconnect the cable, or turn Wi-Fi off and on). The run then backs up what the
   restore left.

If the backup's interfaces are not all on the new machine, the router restores it but does not reboot, and the run
stops: at the router's console choose 1) Assign interfaces, give each a device, then 6) Reboot, and run `just deploy
router` again.

**Without this repository**, the same backup restores by hand: the router's System → Configuration → Backups →
Restore, ticking "Decrypt" with the backups' password; or the installer's "Import configuration" from a USB stick
holding `conf/config.xml`, the backup, with `conf/config.secret`, its password (`opnsense-importer(8)`), both wiped
afterwards.
