# truenas

Configures TrueNAS Community Edition 25.10 through its JSON-RPC WebSocket API, from the controller.
It never uses SSH. With only its defaults it connects, reports the NAS's state and changes nothing.
Each part of the NAS is managed only when its variable is set.

It never destroys, wipes, exports or upgrades a pool. It never deletes a dataset. It refuses an NFS
share that names no hosts or networks, since NFS with `sec=sys` has no authentication.

## Requirements

- The controller runs the modules locally: `ansible_connection: local` and an interpreter with
  `websocket-client` installed (`ansible_python_interpreter: "{{ ansible_playbook_python }}"` when
  Ansible runs from a virtualenv that has it).
- An API key for a user with full admin rights, kept in a vault. Use HTTPS only: TrueNAS revokes a key
  sent over plain HTTP.
- Optionally, the SHA-256 fingerprint of the NAS's web certificate in `truenas_api_cert_sha256`. With
  certificate validation off (the default certificate is self-signed), it is what stops the API key
  being sent to another device that answers on one of the addresses.

## On a fresh install

The role cannot create its own way in. After installing TrueNAS, and before the first run:

1. Create a local user in the `builtin_administrators` group (the Full Admin role) and an API key for it with no
   expiry, over HTTPS. Put the key where `truenas_api_key` reads it, ideally a vault.
2. If you pin the certificate, read the new install's fingerprint and update `truenas_api_cert_sha256`; a
   reinstall generates a new self-signed certificate.
3. Make sure one of `truenas_api_hosts` reaches the NAS: a DHCP reservation for its MAC makes the install come up
   on the expected address.

Then run with `--check` first. Pools are imported with their datasets; shares, services and settings are
re-applied, since TrueNAS keeps those on the boot drive. This repo's full procedure, for both machines, is in
`REBUILD.md` at the repo root.

## Stages and tags

| Tag | Does | Driven by |
|---|---|---|
| `truenas_info` (always) | Connects, checks the release, reports state | `truenas_api_hosts`, `truenas_api_key` |
| `truenas_network` | Network settings; DHCP to a static address, with TrueNAS's rollback safety net | `truenas_network_settings`, `truenas_network_mode`, `truenas_static_address`, `truenas_gateway`, `truenas_nameservers` |
| `truenas_pools` | Pins the system dataset, imports (or creates on blank disks) pools, sets auto-trim and scrub | `truenas_pools`, `truenas_pool_*`, `truenas_system_dataset_pool` |
| `truenas_system` | General settings (UI, timezone, ...), NTP servers | `truenas_general_settings`, `truenas_ntp_servers` |
| `truenas_datasets` | Creates datasets or corrects their properties | `truenas_datasets` |
| `truenas_nfs` | NFS service settings and shares | `truenas_nfs_settings`, `truenas_nfs_shares` |
| `truenas_services` | Enables, disables, starts and stops services, last | `truenas_services` |

`defaults/main.yml` and `meta/argument_specs.yml` describe every variable. Run with `--check --diff`
first; there is no test double.

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

- TrueNAS allows 20 logins per minute from one address, and each module run logs in once. The modules
  take whole lists, so a full run logs in about 13 times. The client waits out the limit when it is hit.
- On a fresh install, importing a pool moves the system dataset onto that pool unless it was chosen
  explicitly. The role pins it to `truenas_system_dataset_pool` (`boot-pool`) before importing.
- An existing dataset's `acltype` or `aclmode` is changed only when its entry sets `allow_acl_change`.
- Pool creation (`truenas_pool_allow_create`) needs every listed disk blank, and is untested.
