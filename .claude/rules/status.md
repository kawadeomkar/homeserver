---
paths:
  - "roles/status/**"
  - "group_vars/*/status.yml"
  - "group_vars/proxmox/connection.yml"
  - "status.yml"
  - "callback_plugins/homeserver_status.py"
  - "scripts/runbooks/proxmox-token.md"
  - "tests/unit/test_status_*.py"
  - "tests/integration/test_status.py"
  - "tests/integration/fake_pve_api.py"
---
Before changing this, read `roles/status/README.md`: it holds the design of what you are changing, what it refuses and
why, and what was seen on the real machines. The Proxmox API token in `group_vars/proxmox/connection.yml` and its
runbook are `just status`'s alone so far; the README says where the token may go and what it may read.
