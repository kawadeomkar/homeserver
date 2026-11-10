# haos_onboard

Gives a new Home Assistant its owner account before anyone else on the network can. Runs on the Proxmox host, which
reaches the VM over its own bridge, after the VM is created (`roles/proxmox_vm`). With only its defaults it does
nothing.

Until a Home Assistant has an owner account, its onboarding API answers without a login: whoever first reaches port
8123 can create that account, and so own the house, or upload and restore a backup (the path behind
CVE-2026-64825). Both close once any onboarding step is done: the onboarding backup views refuse from then on
(`homeassistant/components/backup/onboarding.py`). The role closes them within moments of Home Assistant answering,
on its first boot.

## What it does

- Reads the VM's state (`qm status`). A real run refuses a VM that is missing or stopped; a dry run on a host
  without the VM says what it would do.
- Finds the VM's IPv4 address through the QEMU guest agent (`qm guest cmd <vmid> network-get-interfaces`), on the
  interface with the VM's MAC (Home Assistant OS's Docker bridges never carry it), unless `haos_onboard_address`
  gives it.
- Waits for Home Assistant to answer `GET /api/onboarding` with its list of steps, following any redirect (Home
  Assistant OS 18 serves Home Assistant on port 80 and redirects 8123 there, seen on the first live run), and sends
  the account where it answered, since a POST is never redirected. While Home Assistant OS prepares on its first
  boot, its landing page answers 401 to every `/api/` request (`home-assistant/landingpage`'s `main.go`), so a 200
  with that list is Home Assistant itself; that first boot can take half an hour (`haos_onboard_retries` times
  `haos_onboard_delay`).
- While the user step is not done, creates the owner account (`POST /api/onboarding/users`, `no_log`), then checks
  that the step is done. The owner logs in afterwards and finishes onboarding, or restores a backup from Settings.
- Never prints the password, nor the authorization code Home Assistant returns. Does nothing once the account exists,
  so a Home Assistant restored from a backup, or set up by hand, is left alone.
- On the run that creates the account, records the first login as left to do (NEXT STEPS: `homeserver_next`,
  subject `home_assistant`, with the URL Home Assistant answered at).

A dry run asks the agent and Home Assistant once, without waiting.

Its reading of the agent's report and of the step list is a filter plugin (`filter_plugins/haos_onboard.py`): any
answer but the step list, such as the landing page's refusal, is an error, never a yes.

Home Assistant OS's landing page still forwards a few Supervisor calls without a login while it prepares (its
network, resolution and job status), on the first boot only: an upstream design, which a network that keeps other
devices away from the VM closes.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `haos_onboard_vmid` | `0` | The Home Assistant OS VM's VMID; 0 means none |
| `haos_onboard_mac` | `""` | Its MAC, which picks its address out of the guest agent's report |
| `haos_onboard_address` | `""` | Its address, when known: then the agent is not asked |
| `haos_onboard_port` | `8123` | Home Assistant's port; a redirect from it (Home Assistant OS 18 sends 8123 to 80) is followed, and the account is created where Home Assistant answered |
| `haos_onboard_name` | `""` | The owner's display name |
| `haos_onboard_username` | `""` | The owner's username, lower case |
| `haos_onboard_password` | `""` | The owner's password, at least 12 characters; never printed |
| `haos_onboard_language` | `en` | The language Home Assistant names its default areas in |
| `haos_onboard_agent_retries` | `40` | How many times a real run asks the agent for the address |
| `haos_onboard_retries` | `120` | How many times a real run asks the onboarding API |
| `haos_onboard_delay` | `15` | Seconds between those questions |

This repository's `homeassistant.yml` passes them from `group_vars/proxmox/haos.yml` and `group_vars/all/haos.yml`:
the password is the vault's `vault_haos_owner_password`.

## This repository's Home Assistant

`homeassistant.yml` runs `roles/proxmox_vm` with Home Assistant's VM as play vars (that role's README, "Home
Assistant's VM"), then this role, on the Proxmox host. Its pre-tasks refuse, in a dry run too:

- `nas_section_error` and `kubernetes_settings_error`, as every playbook does;
- `haos_settings_error` (`group_vars/all/haos.yml`, which `proxmox.yml`, `suspend.yml` and `resume.yml` assert too);
- a Home Assistant without `vault_haos_owner_password` of at least 12 characters, checked before the VM exists: a
  Home Assistant started without its owner account waits for whoever reaches it first.

Without `haos_mac` in `local.yml` (`haos_present`, worked out from it, never set) the play says so and ends: no VM, no
download, and no backup covers the VM. `haos_settings_error` refuses `haos_present` given with `-e` (a string, which
counts as true whatever it says) and, without a MAC, the section's other settings, which is what a misspelt
`haos_mac` looks like. With one it refuses a MAC that is not locally administered lower case, or that a cluster node
has too; a VMID that is not a number from 100 to 999999999, or that a cluster node has; sizes that are not whole
numbers, or a disk under the image's 32 GiB; USB devices that are not a list of `{host}` or `{mapping}`; a username
Home Assistant would refuse; and the VM's VMID in `vm_backup_extra_vmids`, which the nightly backup now covers by
itself.

The VM (`haos_vms` in `group_vars/proxmox/haos.yml`; its switch, VMID and size in `group_vars/all/haos.yml`, which
`proxmox.yml` reads too, since the nightly backup covers it):

- VMID 310, after the cluster's 300 and 301 (`local.yml`'s `haos_vmid` changes it), named `homeassistant`, 2 cores,
  4 GiB and a 32 GiB disk, Home Assistant's own recommendation and the image's own size (`haos_size` changes any of
  them); its MAC is `local.yml`'s `haos_mac`, which DHCP must know before the first boot.
- UEFI without pre-enrolled keys (Home Assistant OS is not signed for Secure Boot), q35, the CPU type `host`,
  `virtio-scsi-pci`, booting from `scsi0`.
- Its disk is imported from the pinned OVA (`haos_version` with `haos_ova_sha256`, GitHub's digest of the release
  asset; every release's OVF names its disk `home-assistant.vmdk`), downloaded to `haos_import_storage` (the
  installer's `local`, or `local-btrfs` on a BTRFS install, which `local.yml` names), and grown to its size. It is on
  `homeserver-persistent` in qcow2 with a NAS (under `persistent/proxmox/vm`'s hourly and daily snapshots) and on the
  installer's storage without one. Proxmox extracts the disk first onto the disk's own storage with a NAS (NFS), and
  onto the import storage without one, whose images content the owner enables once on an LVM or ZFS install
  (`scripts/runbooks/haos.md`).
- The EFI disk is configured on the system disk's storage (`haos_efidisk_storage`), without which the VM cannot
  boot anyway, so it follows the system disk when that moves, and with a NAS adds nothing to the installer's thin
  pool, which other projects' VMs share and the disk budget guards.
- The `i6300esb` watchdog, reset on expiry: the OVA board's kernel has `CONFIG_I6300ESB_WDT=y` and its systemd sets
  `RuntimeWatchdogSec=300`, so a hung VM is reset.
- Started on boot, startup order 3, after the cluster's nodes; tag `haos`.
- The USB devices `local.yml`'s `haos_usb` names (a Zigbee or Z-Wave stick), which reach a running VM at once.

Home Assistant updates itself once it runs, so the OVA pin only ever makes a new VM: bump it with its checksum when
convenient, never to update a running one (`scripts/runbooks/haos.md`). `just suspend` and `just resume` shut the VM
down and start it through `roles/proxmox_vm`'s `power` entry point.

## Tests

`tests/unit/test_haos_onboard.py` covers the filter plugin, answers the fakes never give included.
`tests/integration/homeassistant.yml` runs `homeassistant.yml`'s own vars, pre-tasks and roles
(`tests/policy/test_roles.py` checks they stay the same) against the fake Proxmox host (`fake_pve.py`, whose guest
agent reports the VM's NIC) and a fake Home Assistant, the test's own HTTP server in
`tests/integration/test_idempotence.py`. That fake is written from Home Assistant's source
(`homeassistant/components/onboarding/views.py`): it answers 401 first, as the landing page does while it prepares,
and redirects every `/api/` request elsewhere, as Home Assistant OS 18's first live run showed. The tests
(`test_haos_with_a_nas`, `test_haos_without_a_nas`, `test_haos_refusals`) also check that neither the password nor
the authorization code reaches Ansible's output, and are marked `cluster`.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
