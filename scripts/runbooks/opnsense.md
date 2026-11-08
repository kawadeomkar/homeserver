# The OPNsense router, through its API

`just deploy router` configures the router through its HTTPS API from this machine (`roles/opnsense`). For now it
only reads: it checks the router is the one the certificate pin names, accepts the key pair and runs a release the
role supports, and reports that release and its plugins. This runbook covers what only the router's web UI can do:
the role's user and its key pair, the certificate pin, and updating the router.

Values are named by their variable in `group_vars/all/local.yml`; examples use RFC 5737 placeholders, `192.0.2.1` for
the router (`opnsense_address`).

## 1. A user and key pair for the role

In the web UI, open System → Access → Users and add a user of its own for this project:

- **Username:** `homeserver`, or any name that says what it is for.
- **Password:** a long random one, kept nowhere: nobody logs in as this user. Tick "Generate a scrambled password" if
  the form offers it.
- **Login shell:** none.
- **Privileges:** "System: Firmware" (`page-system-firmware-manualupdate`), which covers `api/core/firmware/*`.
  OPNsense has no narrower one for reading the release: the same privilege can also update and reboot the router, so
  treat the key pair as an administrator's credential. Later stages of the role add the privileges they need here.

Save, then under the user's "API keys" add a key. The browser downloads a file holding `key=` and `secret=` lines,
once. Put both in the vault, `just vault` (or `just init`), as `vault_opnsense_api_key` and
`vault_opnsense_api_secret`, then delete the file.

To revoke the pair, delete the key under the same user; a new one goes in the vault the same way.

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

Replacing the web certificate, or reinstalling the router, means updating the pin: until then every run stops with
"presented certificate SHA-256 …, not the pinned one; the API key was not sent".

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

## 4. The run

1. Set `opnsense_address` and `opnsense_api_cert_sha256` in `local.yml`, and the key pair in the vault.
2. `just check router`. Expect `OPNsense <version> at 192.0.2.1`, its kernel and base sets and its plugins, and no
   change. An older router stops with "The router runs OPNsense 26.1.2; this role needs 26.7.2 or later": update it
   (section 3) and run it again.
3. `just deploy router` does the same today, since the role only reads.

An inventory made before the router group was added has none, and the run says to add `[router]` and `opnsense` to
`inventory`, as in `inventory.example`.
