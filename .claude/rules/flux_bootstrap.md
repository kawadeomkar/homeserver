---
paths:
  - "roles/flux_bootstrap/**"
  - "group_vars/kubernetes/flux.yml"
  - "tests/integration/flux.yml"
  - "tests/integration/fake_kubectl.py"
---
Before changing this, read `roles/flux_bootstrap/README.md`: it holds the design of what you are changing, what it refuses and
why, and what was seen on the real machines.
