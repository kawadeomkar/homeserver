---
paths:
  - "roles/status/**"
  - "group_vars/*/status.yml"
  - "status.yml"
  - "callback_plugins/homeserver_status.py"
  - "tests/unit/test_status_*.py"
  - "tests/integration/test_status.py"
---
Before changing this, read `roles/status/README.md`: it holds the design of what you are changing, what it refuses and
why, and what was seen on the real machines.
