# opnsense

Configures an OPNsense router through its HTTPS API, from the controller: nothing goes over SSH, and no UI click is
part of the setup. For now it only reads. It checks that the server is the router the certificate pin names, that
the router accepts the API key pair, and that it runs a release the role supports (`opnsense_min_version`, 26.7.2).
Then it reports that release, the kernel and base sets that carry FreeBSD's fixes, and the plugins installed. With
only its defaults it connects nowhere: the playbook ends before the role runs when there is no router address.

**The key pair goes only to the pinned certificate.** A default install's web certificate is self-signed, so
certificate validation is off and `opnsense_api_cert_sha256` pins its SHA-256. The client connects, compares the
certificate the server presents, and sends the request, with the key pair as HTTP basic authentication, only when
it matches. Another certificate stops the run before anything is sent. Replacing the router's certificate means
updating the pin.

**An older router is refused, in a dry run too.** 26.7.2 is the first release with FreeBSD-SA-26:52's WireGuard fix,
and 26.7 the series whose API the role was written against. The role never updates the router: that stays by hand
(`scripts/runbooks/opnsense.md`).

The module, `opnsense_info`, reads `GET /api/core/firmware/info` and returns only the fields named above, never the
product record with its mirror settings. Its client, `module_utils/opnsense_api.py`, is shared by the modules later
stages add, each under a tag of its own, as `roles/truenas`'s stages are.

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

## Example

```yaml
# group_vars/router/connection.yml
opnsense_api_host: 192.0.2.1
opnsense_api_key: "{{ vault_opnsense_api_key }}"
opnsense_api_secret: "{{ vault_opnsense_api_secret }}"
opnsense_api_cert_sha256: "AA:BB:...:99"
```

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
