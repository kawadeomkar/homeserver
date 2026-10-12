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

Only what takes effect at boot needs a reboot: a kernel, and packages such as CPU microcode, which a
kernel without late loading applies only at boot, or dbus, whose system bus stays up until the node boots.
Other upgrades restart their own services and reboot nothing. Every run says whether either is waiting for
a boot, which it reads from the node rather than from what this run did, so a reboot not done once is still
pending on the next run, even one with nothing left to upgrade. Only with `proxmox_apt_reboot` does the role
then reboot the node itself; it is off by default, for a node that a run cannot reboot safely, which is then
told on every run to be rebooted by hand. What counts as waiting:

- **A kernel** is waiting when the kernel the node runs is not the newest installed kernel image package
  (`uname -r` against `dpkg-query`): `proxmox-kernel-<release>`, with `-signed` for the Secure Boot build
  Proxmox installs by default on x86. One installed earlier, by hand or from the web UI, counts too; other
  packages that share the prefix, such as `proxmox-kernel-helper`, boot nothing. A running kernel that is
  none of these packages, as on a node whose kernels are named some other way, is never rebooted for, since
  the reboot would not change that.
- **Anything else** is waiting while Debian's flag file for a pending reboot exists,
  `proxmox_apt_reboot_required_file`, `/run/reboot-required`, whoever wrote it: dbus's own package writes
  it on every upgrade. The role writes it too, with the package's name in the `.pkgs` file beside it, for
  each package in `proxmox_apt_reboot_packages` the upgrade installed or upgraded, even when the upgrade
  fails after unpacking it. `/run` is emptied by every boot, so a request lasts until the node boots and
  never outlives it.

A listed package reboots the node on every upgrade of it, including a microcode release that brings this
CPU nothing new: when in doubt, the node reboots. A dry run predicts a reboot for a kernel, for a listed
package and for a flag file that already exists; a package that writes the flag file itself and is not
listed reboots the node without the dry run predicting it, which is why this setup lists dbus.

After the reboot the role waits for the node to come back and checks that it booted the newest kernel.
While VMs are running the reboot is refused and the run fails naming them,
unless `proxmox_apt_reboot_with_vms` allows it; Proxmox then stops them on the way down and starts the
ones marked for it after the boot delay.
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
| `proxmox_apt_reboot_packages` | `[]` | Packages besides the kernel whose upgrade needs a reboot, such as `dbus` or `intel-microcode`; an upgrade of one is recorded in the flag file, and a dry run predicts it |
| `proxmox_apt_reboot_required_file` | `/run/reboot-required` | Debian's flag file for a pending reboot, with a `.pkgs` file beside it; must be on a filesystem a boot empties |
| `proxmox_apt_reboot` | `false` | Reboot when the running kernel is not the newest installed or the flag file exists; report only in check mode |
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
proxmox_apt_reboot: true      # off by default; on only where a run may reboot the node
proxmox_apt_reboot_packages: [dbus, intel-microcode, amd64-microcode]
```

A dry run shows each file it would write or change, with a diff, and the upgrade apt-get would do with the
lists as last refreshed, and whether the node would reboot. The real run refreshes the lists after the
repository changes, so a fresh install's enterprise repository never answers 401.
