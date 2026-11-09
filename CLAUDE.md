# CLAUDE.md

This file holds what every Claude Code session in this repository needs; the last section says where the rest is.

## Overview

A homelab's infrastructure: the Ansible that configures the machines and builds a Kubernetes cluster on them, and the
manifests Flux syncs from `k8s/clusters/<setup>` (the platform in `k8s/infrastructure/`, the apps in `k8s/apps/`). There
is no application code. Home Assistant is a VM of its own, Home Assistant OS.

**Run as test deployments** on the home machines with a NAS (2026-10-17, then rebuilt after `docs/SECURITY-REVIEW.md`,
gitignored) and without one on a second host (2026-10-20). Treat the cluster as disposable until it holds data worth
keeping (`scripts/runbooks/teardown.md`).

## Direction of travel

The repo targets exactly two setups, one host of each kind (hardware in `docs/MACHINES.md`, which is gitignored):

| Machine | Role |
|---|---|
| `homeserver` | **Proxmox** hypervisor; the cluster runs in **Talos Linux** VMs on it, a control plane and a worker in both setups. Also on the tailnet, the one way in from outside, when `local.yml` sets `tailscale_enabled`, and the LAN's Tailscale subnet router when it gives `tailscale_advertise_routes` |
| `truenas` | TrueNAS Community Edition; external storage for Proxmox's VM disks and backups and for the cluster's shared data. **Optional**: with no NAS address in `local.yml`, the NAS play ends by itself and everything stays on the Proxmox host |
| `opnsense` | router and firewall; the LAN's DHCP reservations and the cluster's wildcard DNS record, by hand. **Optional**: with `opnsense_address` in `local.yml`, `roles/opnsense` reads it through its API, backs its configuration up and restores it on request |

The decisions behind the cluster are `docs/KUBERNETES-PLAN.md`, its work packages and their status
`docs/KUBERNETES-IMPLEMENTATION.md` (both gitignored). Still open: the owner's steps in `TODO.md`, the running backlog:
DHCP reservations, a servers VLAN, the CA on each device.

**History.** It began as manifests for one Ubuntu host on k3s; `docs/ISSUES.md` and `docs/MIGRATION-TRIAGE.md`
(gitignored) audited that tree. A branch that still has hostPaths under `/opt`, NodePorts, k3s's bundled Traefik or
privileged media pods is dead weight.

## Commands

```bash
just                              # every recipe, by group; just help <recipe> shows one's arguments, options, defaults and allowed values
just install                      # the project's virtualenv synced to poetry.lock (Poetry from PATH: pipx install poetry); refuses a Python that is not a virtualenv
just init                         # hooks, inventory and local.yml from their examples, then each setting and secret asked for (--full: the less used ones too)
just lock                         # update poetry.lock after editing pyproject.toml; packages named after it (just lock ruff) move on too
just vault                        # edit the secrets; just vault new: what is missing; just vault list, switch <setup>, new <setup> [--from current], edit <setup>: the named setups
just lint                         # every linter, each a type: lock yaml ansible python docs github manifests charts talos (k8s: the last three)
just test                         # every test, each a type: unit policy gitleaks idempotence; --pytest-args='-m cluster --shard 1/2' for more
just fix                          # write what lint can fix: python (ruff), docs (every role's REFERENCE.md, written by docsible)
just check truenas                # dry run with --diff of one playbook (truenas proxmox router homeassistant kubernetes); just deploy <playbook> applies it
just check / just deploy          # every playbook in order, the NAS first (site.yml, which leaves the router out); name site to pass anything with it (just check site --tags truenas_nfs)
just talos-upgrade                # move the running cluster to the pins: etcd snapshot, nodes, Kubernetes; --check shows the plan; just etcd-snapshot takes a snapshot alone
just suspend                      # every app to zero, Flux told to leave it, then Home Assistant's VM shut down; just resume brings them back; --only, --all (the cluster's VMs too), --check
just status                       # what runs, where to reach it and what needs doing, read-only; sections named after it; --json; exit 0 ok, 1 problem, 2 unread, 3 failed
```

A recipe takes what it acts on as arguments and reads nothing from the environment but `VENV`, `VIRTUAL_ENV` and
`POETRY`; it refuses a make-style `NAME=value`. just's own flags (`--dry-run`) go before the recipe, and
ansible-playbook's after the playbook (`just check truenas --tags truenas_nfs`, `-e var=value` for one run) or, for
`status`, `suspend` and `resume`, after `--`. The listing writes an optional argument as `playbook='site'`, which is
never typed: `just deploy proxmox`.

Run tools from the project's virtualenv, never from `PATH`: on this machine the pyenv virtualenv `homeserver` (Python
3.14.6), chosen by `pyenv local homeserver` (for a worktree, the main checkout's `.python-version`). `VENV=<path>`
overrides it, and the top of the justfile says how just finds one. `just install`'s `poetry sync` empties whatever it is
pointed at, so never pyenv's global version, and just never creates an environment. Poetry (pipx) and just (Homebrew,
1.55 or later) come from `PATH`, as do kubeconform, kube-linter, kustomize, helm, talosctl, gitleaks and `openssl`.
Outside the checkout `python3` is a broken pyenv build (3.13.1, `libintl` dyld error), and
`~/.local/bin/ansible-playbook` and `~/.local/bin/yamllint` have a dead shebang. ansible-core 2.21 treats a non-boolean
conditional as a fatal error. When the shell's standard handles are non-blocking, ansible commands exit with "Ansible
requires blocking IO"; redirect stdin from `/dev/null` and stdout/stderr to a file.

**The vault** is `group_vars/all/local.vault.yml`, git-ignored like `local.yml` and committed nowhere, since this public
repo would publish even its ciphertext; every setup needs it and `.vault_pass` (`just vault new`).
`local.vault.yml.example` is its template: every key, each `""` under a comment saying what it is for
(`vault_discord_token`, in an older vault, is left over from the deleted roles). The Talos secrets bundle, every key of
the cluster, lives in `talos_config_dir` alone, and a Kubernetes Secret is a SOPS file. **Never print the vault's
contents; check it by listing variable names only.**

**Setups** (`scripts/init/setups.py`'s docstring, README's "Setups and the vault"): a checkout keeps its own
`local.yml`, vault and `.vault_pass`, or links all three from a named setup, and `setups.py check` runs before every
playbook, since Ansible skips a link to a missing file without a word.

## Architecture

Thirteen roles, run by the playbooks at the top of the tree. The inventory has four groups and no addresses.

| Role | What it does | What it never does |
|---|---|---|
| `truenas` | the NAS through its JSON-RPC WebSocket API, with custom modules, in tagged stages | destroy, wipe, export or upgrade a pool |
| `opnsense` | the router through its API: its release checked, its configuration backed up, restored on request | update the router, delete a backup, restore unless asked |
| `proxmox_apt` | the host's deb822 sources and `dist-upgrade`; says when a reboot is needed | reboot the host |
| `proxmox_tailscale` | the host on the tailnet, and the LAN's subnet router, behind a fail-closed nftables guard | log out, retag or restart tailscaled |
| `proxmox_storage` | the NFS storages (`pvesm`), the boot delay and the wait for them at boot | remove a storage |
| `proxmox_backup` | the nightly vzdump job | delete a job |
| `proxmox_node_exporter` | the host's node exporter, which only the cluster's nodes may scrape | remove it |
| `proxmox_vm` | the cluster's VMs and Home Assistant's (`qm`, `pvesh`), within a disk budget | delete a VM or an ISO, shrink or detach a disk, eject the CD-ROM, restart a VM, move a disk unless told |
| `haos_onboard` | a new Home Assistant's owner account, through its onboarding API | touch one that has an owner |
| `talos` | Talos on the nodes, from the controller: install, configuration drift, bootstrap once | reboot a node to apply, upgrade outside `just talos-upgrade`, bootstrap twice |
| `flux_bootstrap` | the Flux Operator, the age key, the cluster's settings and the FluxInstance | change the synced path unless told |
| `app_scale` | `just suspend` and `just resume`: the apps to zero and back, out of Flux's hands meanwhile | touch the platform, claims, Services or Ingresses |
| `status` | `just status`: what runs and what needs doing, through an exact list of reads | change what it reads, or read a secret |

**Each role's design is in its `README.md`**: what it refuses and why, what was seen on the real machines, and the tests
and fakes that cover it. Read it before changing the role: a rule in `.claude/rules/` says so as Claude reaches the
role's files, its group_vars or its tests.

**One engine draws every run and report** (`filter_plugins/`): a new playbook, or a renamed role or task file, may need
its declaration in `filter_plugins/live_recipes.py` changed.

**Configuration.** Role defaults change nothing; everything that describes these machines is in tracked group_vars,
whose comments say what each file holds, apart from addresses and serials, in the git-ignored
`group_vars/all/local.yml`. `group_vars/all/` is read alphabetically and the last file wins, so a tracked file there
only reads `local.yml`'s settings. A host sees only its own groups' files, so whatever two plays read lives in
`group_vars/all/`.

**The switches.** `nas_present` (a NAS address in `local.yml`), `kubernetes_present` (node addresses), `router_present`
(`opnsense_address`) and `haos_present` (`haos_mac`) are worked out, never set: each guard refuses one given with `-e`,
which would be a string that counts as true even when it says `false`. A misspelt address would look exactly like no NAS
or no cluster, so every playbook first asserts the guards it depends on (`nas_section_error` and the `*_settings_error`
of what it reaches), whose messages are the list of what each refuses. Without a NAS every VM disk is on the installer's
storage. In both setups the cluster is a control plane, which runs Talos, etcd and the API alone and keeps Talos's
taint, and a worker that runs the apps; until 2026-10-19 a setup without a NAS had a single schedulable control plane,
and one shape keeps the two setups from drifting apart.

**Storage.** The NAS's two pools are named by class, `persistent` and `ephemeral`, never by hardware, and storage ids
say who owns a storage and what it is for, never the backend (`roles/truenas/README.md`,
`roles/proxmox_storage/README.md`).

## Landmines

What is easy to break wherever the change is; `docs/` (gitignored) holds the audits behind them.

- **Every change to the NAS goes through `roles/truenas`.** No UI clicks, ad-hoc API calls or SSH; a fresh install plus
  one run must rebuild it. Read-only API queries are fine.
- **Flux holds cluster-admin, so merge rights on `main` are the cluster's admin boundary.** It applies whatever the
  synced branch holds. Whoever can merge to `main` controls the cluster, which is why `flux_sync_url` has no default and
  a fork syncs its own repository; another branch is unprotected, so syncing one takes `flux_sync_ref_unprotected: true`
  (`scripts/runbooks/talos.md`). Read the live ruleset (`gh api repos/kawadeomkar/homeserver/rulesets/24555579`) before
  relying on it.
- **Two secret systems, split by layer:** Ansible Vault for host and playbook variables, SOPS+age for Kubernetes
  Secrets, each a `k8s/**/*.sops.yaml` (`k8s/README-secrets.md`). No plaintext `Secret` manifest may be committed.
- **The NAS's NFS shares and the VM backups trust addresses, on a flat LAN.** NFS with `sec=sys` trusts the client's
  address, and the shares map root to root. The nightly backups hold every key of the cluster and its etcd, so treat
  `homeserver-backup` as secret-level. A device that leases a node's address or ARP-spoofs it reads them as root; the
  owner's answer, decided 2026-10-17, is DHCP reservations and a servers VLAN (`TODO.md`), not Proxmox Backup Server's
  encryption.
- **The Tailscale subnet router masquerades every tailnet client to the Proxmox host's address**, the very address the
  NAS exports VM disks and backups to. The tailnet policy's grants and the role's fail-closed nftables guard each keep
  tailnet clients to the NAS's web UI, but any proxy on the host itself connects unseen by the guard. Reload
  `tailnet-guard.service`; never restart it, which restarts Tailscale.
- **The nodes' firewall drops what no rule lets in** (`group_vars/kubernetes/talos.yml`), and **NetworkPolicies are
  enforced** (`k8s/CLAUDE.md`): a new port on a node, or a new path between pods or to the NAS, times out until a rule
  names it. Try a changed node rule first with `talosctl apply-config --mode try`: one that closes 50000 locks the role
  out of the node, leaving only its console in Proxmox and a reinstall.

## Conventions

- **No address in any tracked file.** IP addresses, and anything else that identifies this network, are never written in
  a task, a role default, a template or an example: tasks reference a variable, and the real value lives only in
  `group_vars/all/local.yml`. `group_vars/all/local.yml.example` is its committed counterpart, with RFC 5737
  placeholders (`192.0.2.x`). Adding such a variable means adding it to both files and to the list `just init` asks from
  (`scripts/init/settings.py`; `tests/policy/test_make_init_settings.py` checks the three agree). `.gitleaks.toml`
  enforces it, MACs and tailnet names included.
- `inventory`, `docs/` and `.claude/` (bar `.claude/rules/`) are gitignored because **this repo is public**: they would
  publish host addresses, MACs and drive serials. `just init` copies `inventory` from `inventory.example`.
- **CI** (`.github/workflows/ci.yml`): `ci-success` is the one check the "Protect main" ruleset is to require. No job
  gets a secret or reaches the home network, so `just check` stays local. Read each Dependabot or Renovate PR before
  merging it: Flux deploys `main` as soon as it lands.
- **What CI cannot vouch for.** The gate is written by the pull request it judges: `ci-success` comes from the PR's own
  `.github/**`, `justfile`, `scripts/**`, `tests/**`, `pytest.ini`, Poetry's files and the linters' configuration, so a
  green check proves nothing about a change to those files; review them as the gate itself. They, with `ansible.cfg`,
  `roles/**`, `group_vars/**`, `vars_plugins/**` and the playbooks, also run with the vault password, the TrueNAS API
  key or root SSH whenever a recipe that runs a playbook runs on the workstation, so never run `just` on an unmerged
  outside PR in the clone that holds `.vault_pass` and `local.yml`; use a separate clone without them.
- **When a leak check fires**, do not merge; a pushed commit is published, even on a pull-request branch.
  `scripts/runbooks/leaks.md` says what to rotate and how to remove it.
- **Git hooks**: a commit from a tool without pyenv on `PATH` fails in `role-references`, which says how to fix it.
- **Pins no bot can see** (CI's tools, the schema refs, the Flux Operator, the Talos pins, Home Assistant's OVA, and,
  until the Renovate app is installed, the images and charts under `k8s/`) are bumped by hand each quarter, each with
  its checksum or digest: `scripts/runbooks/dependency-bumps.md`.
- **Runbooks and helpers live in `scripts/`**, and other docs link to a runbook rather than repeat it. CI's shellcheck
  covers only `scripts/ci/*.sh`, so a shell helper elsewhere means widening that glob. `scripts/runbooks/REBUILD.md` is
  the recovery after a hardware failure: when a role gains or loses a prerequisite, or the run order changes, update it
  in the same change.
- **Commit style**: Conventional Commits with an explanatory body.
- **Pull request descriptions show the change by example**: the command a reader would run and what it prints, real and
  never written by hand, saying where it came from (the machines, a testing VM, or the test suite's fakes), trimmed to
  the lines that matter, with no addresses or secrets: a new refusal shows the playbook's message, a new setting the
  `local.yml` lines and what they render, a role change the lines of its dry run.

## Where the rest is written

This file loads into every session, so a change writes what it adds where the work is; `tests/policy/test_claude_md.py`
keeps this file to 200 lines and 40,000 characters, and every other `CLAUDE.md` and rule under 35,000.

| Subject | Where |
|---|---|
| a role's design, refusals, history and tests | `roles/<role>/README.md`; the conventions every role keeps, `roles/CLAUDE.md` |
| the manifests, Flux, the apps, storage, networking, monitoring, the GPU, Renovate | `k8s/CLAUDE.md` |
| the output engine, NEXT STEPS, the live region | `filter_plugins/CLAUDE.md` |
| the test types, the fakes, the policy tests | `tests/CLAUDE.md` |
| CI's jobs, the ruleset, Dependabot | `.github/CLAUDE.md` |
| what to read or know on paths a directory cannot cover (a role's group_vars, Poetry, the hooks, the linters) | `.claude/rules/`, a small file with `paths:`, in words and never an `@import`, which loads in every session |
| a procedure followed by hand | `scripts/runbooks/` |
| the owner's view of the whole, setups and switching between them | `README.md` |
