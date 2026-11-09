# opnsense

Configures an OPNsense router through its HTTPS API, from the controller: nothing goes over SSH, and no UI click is
part of the setup. It checks that the server is the router the certificate pin names, that the router accepts the API
key pair, and that it runs a release the role supports (`opnsense_min_version`, 26.7.2), and reports that release,
the kernel and base sets that carry FreeBSD's fixes, and the plugins installed. With only its defaults it connects
nowhere: the playbook ends before the role runs when there is no router address.

**Nothing goes to another certificate.** A default install's web certificate is self-signed, so certificate
validation is off and `opnsense_api_cert_sha256` pins its SHA-256. The client connects, compares the certificate the
server presents, and only then sends a request, with the key pair or, for a restore, a password. Another certificate
stops the run before anything is sent. Replacing the router's certificate means updating the pin.

**An older router is refused, in a dry run too.** 26.7.2 is the first release with FreeBSD-SA-26:52's WireGuard fix,
and 26.7 the series whose API the role was written against. The role never updates the router.

**The configuration file is the source of truth** for rebuilding the router (`scripts/runbooks/opnsense.md`, sections
4 and 6).
- **Backups.** With `opnsense_backup_dir`, `opnsense_backup` reads the router's configuration history
  (`api/core/backup/backups/this`), whose newest entry is the running configuration, and compares it with the newest
  backup by history id: none yet, or the newest backup's revision still in the router's history with a newer one
  after it, and it downloads the router's revision (`download/this/<id>`) and writes it encrypted in OPNsense's own
  format (AES-256-CBC, PBKDF2-SHA512, 100000 iterations, `module_utils/opnsense_crypt.py`) with
  `opnsense_backup_password`, mode 0600 in a 0700 directory, after proving it reads back. The same revision: nothing.
  Any other history (restored or reinstalled by hand, or more revisions than the router keeps): the run stops, unless
  `opnsense_backup_accept` keeps the router's configuration as the newest backup. A dry run writes nothing. No backup
  is ever deleted, and the configuration is never returned.
- **Restores**, only with `opnsense_restore` (`latest` or a backup's name). The API cannot upload a configuration, so
  `opnsense_restore` logs in to the web UI as `opnsense_gui_user` and restores the file, still encrypted with its
  password, on the Restore page (`diag_backup.php`: the CSRF token the legacy pages ask for, the whole file, the
  history flushed, the console settings kept, a reboot). Onto the router as it is known (its API answers with the
  pin and the key pair) it backs the router up first and does nothing when the router runs that backup already. Onto
  anything else, a fresh install at `opnsense_bootstrap_host`, the certificate is unknown: without
  `opnsense_bootstrap_cert_sha256` the role prints the fingerprint the router presents and stops, having sent
  nothing. It then waits for the router's API at its own address (`opnsense_restore_wait`) and backs up what the
  restore left. A dry run logs in and proves the backup's password, restoring nothing; an interface mismatch stops
  the run for the console.

The modules are `opnsense_info`, `opnsense_backup` and `opnsense_restore`, with one client in
`module_utils/opnsense_api.py` (the API with a key pair, the web UI with a session), shared by the modules later stages
add, each under a tag of its own, as `roles/truenas`'s stages are. The filter `opnsense_backup_pick` orders the backups
by their revision's time.

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

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
