# opnsense

Configures an OPNsense router through its HTTPS API, from the controller: nothing goes over SSH, and no UI click is
part of the setup. It checks that the server is the router the certificate pin names, that the router accepts the API
key pair, and that it runs a release the role supports (`opnsense_min_version`, 26.7.2), and reports that release,
the kernel and base sets that carry FreeBSD's fixes, and the plugins installed. With only its defaults it connects
nowhere: the playbook ends before the role runs when there is no router address.

The API is `https://<router>/api/<module>/<controller>/<action>`, with the key pair as HTTP basic authentication. The
release comes from `GET /api/core/firmware/info` (`opnsense_info`), of which the role keeps the version, the kernel
and base sets and the plugins, never the product record, whose mirror and repository settings are the router's
business; the firmware information lists what the router already knows of, and runs no update check.

**Nothing goes to another certificate.** A default install's web certificate is self-signed, so certificate
validation is off and `opnsense_api_cert_sha256` pins its SHA-256. The client opens a connection for each request,
compares the certificate the server presents, and only then sends the request, with the key pair or, for a restore, a
password. Another certificate stops the run before anything is sent. With neither a pin nor validation, the input
checks and the client refuse before connecting. Replacing the router's certificate means updating the pin.

**An older router is refused, in a dry run too.** 26.7.2 is the first release with FreeBSD-SA-26:52's WireGuard fix,
and 26.7 the series whose API the role was written against. A development release has no release number to compare,
and is refused with the rest. The role never updates the router: that stays by hand (System > Firmware, a major
release at a time, `scripts/runbooks/opnsense.md`, section 3).

**The configuration file is the source of truth** for rebuilding the router (`scripts/runbooks/opnsense.md`, sections
4 and 6). Every save on the router is a revision, whose copy `/conf/backup/config-<microtime>.xml` the API lists and
downloads; the router keeps the last 100.
- **Backups.** With `opnsense_backup_dir`, `opnsense_backup` reads the router's configuration history
  (`api/core/backup/backups/this`), whose newest entry is the running configuration, and compares it with the newest
  backup by history id: none yet, or the newest backup's revision still in the router's history with a newer one
  after it, and it downloads the router's revision (`download/this/<id>`) and writes it encrypted in OPNsense's own
  format (AES-256-CBC, PBKDF2-SHA512, 100000 iterations, `module_utils/opnsense_crypt.py`) with
  `opnsense_backup_password`, mode 0600 in a 0700 directory, after proving it reads back. The same revision: nothing.
  Any other history (restored or reinstalled by hand, a new key on a fresh install, or more revisions than the router
  keeps): the run stops, unless `opnsense_backup_accept` keeps the router's configuration as the newest backup, so a
  stray configuration never becomes the newest backup, which a restore would then bring back instead of the backup's.
  A dry run writes nothing. No backup is ever deleted, and the configuration is never returned.
- **The format.** `opnsense_crypt.py` writes and reads what `openssl enc -aes-256-cbc -md sha512 -pbkdf2 -iter
  100000` makes, salted, base64 between the router's `---- BEGIN config.xml ----` lines and headers (opnsense/core's
  `OPNsense/Backup/Base.php`), so the router's Restore page and its installer open a backup with its password, and a
  backup the router made reads here. That is proven against the openssl command both ways. A backup is named by its
  history id, `config-<microtime>.xml`, with `_<nanoseconds>` when two saves shared a microsecond
  (`OPNsense/Core/Config.php`'s `backup()`).
- **Where they go.** `opnsense_backup_dir` must be outside the repository, which the input checks refuse otherwise: a
  backup holds every secret of the router, and a commit would publish it. A directory readable by others is refused.
  Only their password opens the backups, so keep it in a password manager too.
- **Restores**, only with `opnsense_restore` (`latest` or a backup's name). The API cannot upload a configuration, so
  `opnsense_restore` logs in to the web UI as `opnsense_gui_user` and restores the file, still encrypted with its
  password, on the Restore page (`diag_backup.php`: the CSRF token the legacy pages ask for, the whole file, the
  history flushed, the console settings kept, a reboot). The file is decrypted on the controller only to prove the
  password, and never written anywhere. Onto the router as it is known (its API answers with the pin and the key pair)
  it backs the router up first and does nothing when the router runs that backup already. Onto anything else, a fresh
  install at `opnsense_bootstrap_host`, the certificate is unknown: without `opnsense_bootstrap_cert_sha256` the role
  prints the fingerprint the router presents and stops, having sent nothing. The router reboots into the backup,
  which brings back its address, its certificate and the API user and key, so the role then waits for the router's
  API at its own address with the pin and the key pair (`opnsense_restore_wait`) and backs up what the restore left.
  A dry run logs in and proves the backup's password, restoring nothing. When the backup names interfaces the machine
  lacks, the router postpones its reboot and the run stops for the console: assign the interfaces there, reboot, and
  run again (section 6).

The modules are `opnsense_info`, `opnsense_backup` and `opnsense_restore`, with one client in
`module_utils/opnsense_api.py` (the API with a key pair, the web UI with a session; Python's standard library only),
shared by the modules later stages add, each under a tag of its own, as `roles/truenas`'s stages are. The plan for
those stages is in the gitignored `docs/WIREGUARD-PLAN.md` (§0.1). The input checks and the connection run under every
tag; a restore (`opnsense_restore`) runs first, when asked, so it brings a fresh install back before anything reads
it, and the backup (`opnsense_backup`) last, so it holds what the stages before it left. The filter
`opnsense_backup_pick` orders the backups by their revision's time.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `opnsense_api_host` | `""` | The router's address |
| `opnsense_api_port` | `443` | HTTPS port of its web server |
| `opnsense_api_key`, `opnsense_api_secret` | `""` | The API key pair of the router's user for this role; keep them in the vault |
| `opnsense_validate_certs` | `false` | Verify the certificate against the controller's trusted authorities instead of pinning it |
| `opnsense_api_cert_sha256` | `""` | The certificate's SHA-256, colons optional; required while `opnsense_validate_certs` is off |
| `opnsense_api_timeout` | `60` | Seconds to wait for the connection and each answer |
| `opnsense_min_version` | `"26.7.2"` | The oldest release the role runs against |
| `opnsense_backup_dir` | `""` | Where the backups go, outside any repository; empty, none are made |
| `opnsense_backup_password` | `""` | The backups' password; keep it in the vault |
| `opnsense_backup_accept` | `false` | Back up a router that does not descend from the newest backup, for one run |
| `opnsense_restore` | `""` | `latest` or a backup's name, for one run; empty, nothing is restored |
| `opnsense_gui_user`, `opnsense_gui_password` | `root`, `""` | The web UI login a restore uses; keep the password in the vault |
| `opnsense_bootstrap_host`, `opnsense_bootstrap_port` | `""`, `443` | Where a fresh install answers; empty, the router's own address |
| `opnsense_bootstrap_cert_sha256` | `""` | The fresh install's certificate, once checked, for one run |
| `opnsense_restore_wait`, `opnsense_restore_poll` | `900`, `15` | Seconds to wait for the router after a restore, and between questions |

## Example

```yaml
# group_vars/router/connection.yml
opnsense_api_host: 192.0.2.1
opnsense_api_key: "{{ vault_opnsense_api_key }}"
opnsense_api_secret: "{{ vault_opnsense_api_secret }}"
opnsense_api_cert_sha256: "AA:BB:...:99"
# group_vars/router/backup.yml
opnsense_backup_dir: ~/.opnsense/homeserver
opnsense_backup_password: "{{ vault_opnsense_backup_password }}"
opnsense_gui_password: "{{ vault_opnsense_root_password }}"
```

`just deploy router -e opnsense_restore=latest` restores the newest backup.

## This repo's router

`router.yml` runs the role, and `site.yml` leaves it out. `just check router` is its dry run: the router's release
against `opnsense_min_version`, its plugins, and its configuration against the newest backup; `just deploy router`
backs it up when it changed; `-e opnsense_restore=latest` (or a backup's name) restores, `-e
opnsense_backup_accept=true` keeps a router that does not descend from the newest backup, and `-e
opnsense_bootstrap_cert_sha256=…` pins a fresh install's certificate, each for one run.

- `group_vars/all/router.yml` holds `router_present`, worked out from `local.yml`'s `opnsense_address` and never set,
  and `router_settings_error`. That refuses `router_present` given with `-e` (a string, which counts as true even
  when it says false), an address or a fresh install's address that is not an IPv4 address, a restore, an accept or
  a bootstrap pin asked for without an address (the run would otherwise leave the router alone without a word about
  it), and the section's other settings without an address, which is what a misspelt address looks like.
- `router.yml`'s first play, on localhost, asserts `router_settings_error` first, so a `router_present` given with
  `-e` is refused for what it is, and refuses an inventory without the `router` group, which an inventory made before
  it lacks: the router play would match no host and end without a word. The router play asserts it again, and without
  `opnsense_address` says so and ends the host before the role runs: the router stays configured by hand.
- `group_vars/router/connection.yml` says how the role reaches the router: the address from `opnsense_address`, the
  key pair from the vault (`vault_opnsense_api_key`, `vault_opnsense_api_secret`); `local.yml` sets the pin
  `opnsense_api_cert_sha256` itself.
- `group_vars/router/backup.yml` holds the backups: `~/.opnsense/homeserver`, or `local.yml`'s `router_backup_dir` (an
  `opnsense_backup_dir` in `local.yml` would lose to this file), the vault's `vault_opnsense_backup_password`, and
  `vault_opnsense_root_password` for a restore's login as root. On a fresh install that is the password set in its
  installer, which should be the router's own root password, so that the one in the vault serves both. A fresh
  install's address is `local.yml`'s `opnsense_bootstrap_address`, or else the router's own.

## Tests

`tests/integration/test_opnsense.py` runs `router.yml`'s play (`tests/integration/router.yml`: this repo's group_vars
for the router, and the router play's pre-tasks and role, which `tests/policy/test_roles.py` holds to the real play's)
against
`tests/integration/fake_opnsense.py`, an HTTPS server written from OPNsense 26.7's source (opnsense/core
`stable/26.7`: `FirmwareController::infoAction`, `BackupController`'s backups and download, and `www/diag_backup.php`'s
restore behind `www/authgui.inc`'s login form and `www/csrf.inc`'s CSRF check). It listens on two ports, the router's
own address with the pinned certificate and a fresh install's with a certificate of its own, and serves the history,
the download, the login form, the CSRF check (403 without the session's token) and the Restore page, a wrong key pair
answered 401. It decrypts an upload with the openssl command, as the router does, answers anything else 404, so an
unexpected call fails the test, and logs whether each request carried credentials, so a test proves none reached a
server whose certificate was not the pinned one. The tests cover the read and the release floor, another certificate,
no pin, a refused key, no address, the backups (first, unchanged, a new revision, a router that does not descend from
the newest backup) and the restores (onto the router as it is known, onto one that runs the backup already, onto a
fresh install, a wrong root password, an interface mismatch); no password, key or secret the configuration holds may
reach Ansible's output.

`tests/unit/test_opnsense_client.py` covers the client, `opnsense_info`'s parsing, the encrypted format against the
openssl command both ways, and the web pages' parsing; `tests/unit/test_opnsense_filters.py` the backups' names and
order. `tests/policy/test_roles.py` runs the play's argument-spec pass and checks each of `router_settings_error`'s
refusals and the inventory without the `router` group.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
