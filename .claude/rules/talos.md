---
paths:
  - "roles/talos/**"
  - "group_vars/kubernetes/talos.yml"
  - "group_vars/all/kubernetes.yml"
  - "talos-upgrade.yml"
  - "etcd-snapshot.yml"
  - "tests/unit/test_talos_filters.py"
  - "tests/talos_validate*.yml"
  - "tests/integration/talos*.yml"
  - "tests/integration/fake_talos.py"
  - "tests/integration/fake_talos_bin/**"
---
Before changing this, read `roles/talos/README.md`: it holds the design of what you are changing, what it refuses and
why, and what was seen on the real machines.
