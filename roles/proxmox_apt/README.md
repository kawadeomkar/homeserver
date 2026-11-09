# proxmox_apt

Sets a Proxmox VE node's package repositories and, when asked, upgrades its packages. A fresh install
ships with the enterprise repositories enabled and no subscription, so `apt-get update` fails with
`401 Unauthorized` until the enterprise ones are turned off and the no-subscription one is added: what
the web UI's Updates → Repositories panel does by hand. With only its defaults the role changes nothing.

Repository files are written whole, as deb822 `.sources` files, the form Proxmox VE 9 ships. A file to
disable is not rewritten: it gets an `Enabled: false` line and keeps the rest, as the UI does, so
Proxmox's own URIs and Ceph release stay its own. The line goes first in the file: deb822 takes its fields in
any order, and a file may end in a blank line, after which a line added at the end would start a stanza of its
own and disable nothing. One that does not exist is left alone. The files are expected to hold one stanza each,
as Proxmox's do. A repository file named in neither list, such as the
`tailscale.sources` that `roles/proxmox_tailscale` writes, is left alone too, and the upgrade upgrades its packages
with the rest.

The Debian release each written file names is read from the node's `/etc/os-release` unless `proxmox_apt_suite`
names one, so a reinstall on a newer Proxmox gets that release's repositories rather than, silently, the old
release's. A node whose file gives no `VERSION_CODENAME` is refused, naming `proxmox_apt_suite`.

The upgrade is `apt-get dist-upgrade`, which Proxmox requires in place of a plain upgrade, run
non-interactively: configuration files changed on the node are kept, and the rest take the package's
version. In check mode nothing is fetched; the upgrade is simulated on the package lists as they were
last refreshed, so a dry run on a fresh install shows no pending upgrade until the first real run has
refreshed the lists.

Only what takes effect at boot needs a reboot: a kernel, and packages such as CPU microcode, which a
kernel without late loading applies only at boot, or dbus, whose system bus stays up until the node boots.
Other upgrades restart their own services and need no reboot. When the upgrade installs or upgrades a kernel
image package (`proxmox-kernel-<release>`, signed or not; `proxmox-kernel-helper` and the meta package share
the prefix and boot nothing) or a package in `proxmox_apt_reboot_packages`, the role writes Debian's flag file
for a pending reboot, `proxmox_apt_reboot_required_file`, `/run/reboot-required`, even when the upgrade fails
after unpacking it: the record is made in the `always` of the upgrade's block. dbus's own package writes it too.
Every run says whether the file exists, whoever wrote it, and every boot empties `/run`, so the node is told to
reboot on each run until it has. A dry run says so for what the simulated upgrade would change. A listed package
asks for a reboot on every upgrade of it, including a microcode or firmware release that brings this hardware
nothing new: when in doubt, reboot. The role never reboots the node: when to take it and its VMs down is decided
by hand.

What the role sees is what its own upgrade unpacked. A kernel installed some other way, by hand or from the
web UI, is not reported, and neither is a boot that came up on an older kernel (a stale boot entry, or a pinned
kernel): the boot empties the flag file. After rebooting, `uname -r` should name the kernel the run reported.

The apt-get steps run through `command`, not the `apt` module, so the idempotence tests can stand in
for them with a fake on `PATH`; the `apt` module needs python3-apt on the node.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `proxmox_apt_sources` | `[]` | Repository files to write: `name`, `uris`, `components`; optional `suites`, `signed_by` |
| `proxmox_apt_disabled` | `[]` | Names of repository files to mark `Enabled: false` when they exist |
| `proxmox_apt_upgrade` | `false` | Refresh the package lists and dist-upgrade; simulate only in check mode |
| `proxmox_apt_reboot_packages` | `[]` | Packages besides the kernel whose upgrade needs a reboot, such as `dbus` or `intel-microcode`; an upgrade of one is recorded in the flag file |
| `proxmox_apt_reboot_required_file` | `/run/reboot-required` | Debian's flag file for a pending reboot; must be on a filesystem a boot empties |
| `proxmox_apt_reboot_command` | `""` | What to do about a pending reboot, recorded under `homeserver_next` as left to do beside the reason, which the stdout callback prints as NEXT STEPS; empty, the reason alone |
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
proxmox_apt_reboot_packages: [dbus, intel-microcode, amd64-microcode, pve-firmware]
```

A dry run shows each file it would write or change, with a diff, and the upgrade apt-get would do with the
lists as last refreshed, and whether it would need a reboot. The real run refreshes the lists after the
repository changes, so a fresh install's enterprise repository never answers 401.

## In this repo

`proxmox.yml` runs the role first, on the Proxmox host over the same root SSH as the roles after it
(`group_vars/proxmox/connection.yml`). It must come first: `roles/proxmox_tailscale`, next in the play, refreshes the
package lists and installs Tailscale through apt, which on a fresh install fails with 401 until this role has turned
the enterprise repositories off, and `roles/proxmox_node_exporter`, last, installs its exporter from the lists this
role refreshed.

The example above is this repo's configuration, `group_vars/proxmox/apt.yml`:

- the no-subscription Proxmox repository on, and `pve-enterprise` and `ceph` disabled, the enterprise repositories the
  installer leaves enabled. There is no Ceph here, so no no-subscription Ceph repository replaces the second.
- the upgrade on: every `just deploy proxmox` refreshes the lists and upgrades the host, Tailscale included, since
  `roles/proxmox_tailscale` only installs it (its own `proxmox_tailscale_upgrade` is off, so this role's switch is the
  only one). One command for everything is easier than a separate one, and an upgrade with nothing to do is quick.
- the packages besides the kernel that ask for a reboot: dbus, whose package itself asks Debian's way for one on every
  upgrade, since its system bus cannot be restarted (listing it lets a dry run predict that); CPU microcode, which
  the kernel loads only at boot, both Intel's and AMD's, so other hardware needs no change; and `pve-firmware`, the
  kernel's device firmware, which drivers load as they start.
- `proxmox_apt_reboot_command` set to `just status`'s words for a reboot (`status_commands.reboot`,
  `group_vars/all/status.yml`), so NEXT STEPS and `just status` say the same. `just status` reads the same flag file
  (`status_reboot_required_file`, `group_vars/proxmox/status.yml`).

## Tests

`tests/integration/test_idempotence.py` runs the role with this repo's own configuration
(`tests/integration/proxmox.yml`, which loads `group_vars/proxmox/apt.yml` and the status words its reboot command
is) against `fake_pve.py`'s `apt-get` behind `fake_pve_bin/`. The fake reads the scratch repository directory the test
passes as `proxmox_apt_sources_dir`, and the flag file is a scratch path too. Its lines follow the real ones, the
simulation's `Inst` and dpkg's `Unpacking`, which the role reads; without `-y` and `DEBIAN_FRONTEND=noninteractive` its
`dist-upgrade` fails, as the real one would stop to ask.

- The fake answers a refresh with `401 Unauthorized` while an enterprise repository is still enabled, as the real
  one does, so the role's order, the files first and then the refresh, is what the test proves, and a configuration
  that forgets to disable one cannot pass.
- With the state's `dpkg_fails`, `dist-upgrade` unpacks everything and then fails as dpkg does when a package's
  script fails, which proves the record survives a failed upgrade.

`test_proxmox_apt_role_is_idempotent` starts from a fresh Proxmox VE 9 install's repository files: a dry run, a run, a
second run, drift corrected, and each input refusal.
`test_proxmox_apt_role_asks_for_a_reboot_only_for_what_takes_effect_at_boot` covers the kernel helper, an unsigned
kernel, dbus and microcode, a failed upgrade and a flag file someone else wrote.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
