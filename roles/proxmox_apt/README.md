# proxmox_apt

Sets a Proxmox VE node's package repositories and, when asked, upgrades its packages. A fresh install
ships with the enterprise repositories enabled and no subscription, so `apt-get update` fails with
`401 Unauthorized` until the enterprise ones are turned off and the no-subscription one is added: what
the web UI's Updates → Repositories panel does by hand. With only its defaults the role changes nothing.

Repository files are written whole, as deb822 `.sources` files, the form Proxmox VE 9 ships. A file to
disable is not rewritten: it gets an `Enabled: false` line and keeps the rest, as the UI does, so
Proxmox's own URIs and Ceph release stay its own. One that does not exist is left alone. The files are
expected to hold one stanza each, as Proxmox's do.

The upgrade is `apt-get dist-upgrade`, which Proxmox requires in place of a plain upgrade, run
non-interactively: configuration files changed on the node are kept, and the rest take the package's
version. In check mode nothing is fetched; the upgrade is simulated on the package lists as they were
last refreshed, so a dry run on a fresh install shows no pending upgrade until the first real run has
refreshed the lists.

A kernel upgrade needs a reboot. With `proxmox_apt_reboot` the role reboots the node when the kernel it
runs is not the newest installed one, which is read from the node (`uname -r` against the signed kernel
packages), not inferred from the run: a kernel installed earlier and never booted counts too, and a run
that upgrades no kernel reboots nothing. It then waits for the node to come back and checks that it
booted the newest kernel. While VMs are running the reboot is refused and the run fails naming them,
unless `proxmox_apt_reboot_with_vms` allows it; Proxmox then stops them on the way down and starts the
ones marked for it after the boot delay. Without `proxmox_apt_reboot`, a pending reboot is only reported.
`ansible.builtin.reboot` is not used because it refuses a local connection, which is how the idempotence
tests run the role against a fake `systemctl`.

The apt-get steps run through `command`, not the `apt` module, so the idempotence tests can stand in
for them with a fake on `PATH`; the `apt` module needs python3-apt on the node.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `proxmox_apt_sources` | `[]` | Repository files to write: `name`, `uris`, `components`; optional `suites`, `signed_by` |
| `proxmox_apt_disabled` | `[]` | Names of repository files to mark `Enabled: false` when they exist |
| `proxmox_apt_upgrade` | `false` | Refresh the package lists and dist-upgrade; simulate only in check mode |
| `proxmox_apt_reboot` | `false` | Reboot when the running kernel is not the newest installed; report only in check mode |
| `proxmox_apt_reboot_with_vms` | `false` | Reboot even while VMs run, instead of failing with their names |
| `proxmox_apt_reboot_wait` | `60` | Seconds before the first reconnection attempt, so a connection made before the shutdown is not taken for the node being back |
| `proxmox_apt_reboot_timeout` | `600` | Seconds to keep trying to reconnect after that |
| `proxmox_apt_suite` | `""` | The Debian release for the repositories; empty reads it from the node's `/etc/os-release` |
| `proxmox_apt_signed_by` | `/usr/share/keyrings/proxmox-archive-keyring.gpg` | Signing key for entries without their own |
| `proxmox_apt_sources_dir` | `/etc/apt/sources.list.d` | Where apt keeps its deb822 files; the tests point it at a scratch directory |

Each entry of `proxmox_apt_sources` becomes `<name>.sources`, with `Types: deb`, its `uris` and
`components` joined by spaces, `Suites` from `suites` or the node's release, and `Signed-By` from
`signed_by` or the role-wide key. A name in both lists, an empty name or one with a slash is refused.

## Example

A node without a subscription, kept current:

```yaml
# group_vars/proxmox.yml
proxmox_apt_sources:
  - name: proxmox
    uris: [http://download.proxmox.com/debian/pve]
    components: [pve-no-subscription]
proxmox_apt_disabled: [pve-enterprise, ceph]
proxmox_apt_upgrade: true
proxmox_apt_reboot: true
```

A dry run shows each file it would write or change, with a diff, and the upgrade apt-get would do with the
lists as last refreshed, and whether the node would reboot. The real run refreshes the lists after the
repository changes, so a fresh install's enterprise repository never answers 401.
