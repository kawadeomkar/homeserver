# haos_onboard

Gives a new Home Assistant its owner account before anyone else on the network can. Runs on the Proxmox host, which
reaches the VM over its own bridge, after the VM is created (`roles/proxmox_vm`). With only its defaults it does
nothing.

Until a Home Assistant has an owner account, its onboarding API answers without a login: whoever first reaches port
8123 can create that account, and so own the house, or upload and restore a backup (the path behind
CVE-2026-64825). Both close once any onboarding step is done. The role closes them within moments of Home Assistant
answering, on its first boot.

## What it does

- Reads the VM's state (`qm status`). A real run refuses a VM that is missing or stopped; a dry run on a host
  without the VM says what it would do.
- Finds the VM's IPv4 address through the QEMU guest agent (`qm guest cmd <vmid> network-get-interfaces`), on the
  interface with the VM's MAC (Home Assistant OS's Docker bridges never carry it), unless `haos_onboard_address`
  gives it.
- Waits for Home Assistant to answer `GET /api/onboarding` with its list of steps, following any redirect (Home
  Assistant OS 18 serves Home Assistant on port 80 and redirects 8123 there), and sends the account where it
  answered, since a POST is never redirected. While Home Assistant OS prepares on its first boot, its landing page
  refuses every `/api/` request, so a 200 with that list is Home Assistant itself; that first boot can take half an
  hour (`haos_onboard_retries` times `haos_onboard_delay`).
- While the user step is not done, creates the owner account (`POST /api/onboarding/users`), then checks that the
  step is done. The owner logs in afterwards and finishes onboarding, or restores a backup from Settings.
- Never prints the password, nor the authorization code Home Assistant returns. Does nothing once the account exists,
  so a Home Assistant restored from a backup, or set up by hand, is left alone.

A dry run asks the agent and Home Assistant once, without waiting.

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

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
