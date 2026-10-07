# homeserver

Infrastructure repo for a small homelab: hand-maintained Kubernetes manifests for ~20 self-hosted
services, plus the Ansible that configures the machines they run on.

> **Status: no Kubernetes workload is deployed.** The repo used to provision a single Ubuntu host running
> k3s, with storage on a TrueNAS CORE box over NFS. That host layer was removed; it is in the git history
> if it is ever needed. The Ansible now configures the NAS and the Proxmox host's storage. See `CLAUDE.md`
> for the detailed architecture, the known landmines, and what survives the migration.

## Target stack

| Machine | Role |
|---|---|
| homeserver | Proxmox hypervisor; Kubernetes runs in VMs on it (Talos Linux is the plan) |
| truenas | TrueNAS Community Edition; all Proxmox VM storage lives here |
| opnsense | Router and firewall for the LAN |

Hardware details are in `docs/MACHINES.md`, which is **gitignored** (this repo is public, and it holds
addresses and serial numbers), so it is absent from a fresh clone.

## Layout

```
k8s/                     one directory per service (+ 00-namespaces/ for shared ones)
roles/truenas/           configures the NAS through its API: address, pools, datasets, NFS
roles/proxmox_storage/   adds the NAS's shares to Proxmox as qcow2 VM-disk storage
truenas.yml proxmox.yml  one playbook per role; site.yml runs both, NAS first
group_vars/all/vault.yml Ansible Vault file: the TrueNAS API key
group_vars/all/local.yml.example  template for local.yml, which holds every address and serial
group_vars/all/storage.yml        VM storage layout shared by both plays
group_vars/nas/, group_vars/proxmox/  connection and per-machine configuration
inventory.example        template for inventory (no addresses in it)
.gitleaks.toml           gitleaks rules for the identifiers this public repo must never hold
requirements.in          the Python tooling's pins; requirements.txt is its hash-locked lock (make lock)
.github/                 CI (workflows/ci.yml), Dependabot, and the reviewed body of the main-branch ruleset
tests/unit/              tests for the TrueNAS modules' comparison logic
tests/policy/            tests of the repo's own rules: the lock, the vault, the ruleset, the leak checks
scripts/                 the leak checks CI and the git hooks run
tests/integration/       idempotence tests against fake TrueNAS and Proxmox
REBUILD.md               what to do before re-running the playbooks after a hardware failure
SECURITY.md              how to report a vulnerability privately
```

`inventory`, `group_vars/all/local.yml` and `./.vault_pass` are gitignored and must be created locally
(`make init` copies the first two from their examples).
No address is ever written in a tracked file: tasks and role defaults read them from variables whose real
values live only in `local.yml`. Kubernetes Secrets are **not**
kept in the vault — see `k8s/README-secrets.md`.

## Ansible

```bash
make venv PYTHON=<python 3.12+>   # hash-locked tooling in .venv (requirements.txt, from requirements.in)
make init                         # git hooks, plus inventory and local.yml from their examples
make lint                         # yamllint, ansible-lint, module docs, GitHub config schemas, tests
make test-idempotence             # each role run repeatedly against local fakes
make ci                           # what the CI gate runs, on one Python, apart from the workflow linters
make check-truenas && make truenas
make check-proxmox && make proxmox
```

Each TrueNAS stage can be run on its own with `TAGS=truenas_network`, `truenas_pools`, `truenas_system`,
`truenas_datasets`, `truenas_nfs` or `truenas_services`. Always run the check first: the idempotence tests use
fakes, which cannot prove the real machines behave the same.

The roles are generic and change nothing by default; this setup's configuration is in `group_vars/`
(`all/storage.yml`, `nas/truenas.yml`, `proxmox/storage.yml`), with addresses and serials in the git-ignored
`all/local.yml`. Each role's `README.md` describes its variables.

### After a hardware failure

`REBUILD.md` is the recovery procedure. A reinstalled NAS needs a new API user and key and an updated certificate
pin before Ansible can reach it, and a reinstalled Proxmox host needs root SSH access. Keep a copy of
`group_vars/all/local.yml` and `.vault_pass` in your password manager: they are the only files not in this repo.

## Manifests

Nothing in this repo applies the manifests any more. The removed `k8s` role ran one `kubectl apply -f`
per directory, in this order:

`00-namespaces`, `homeassistant`, `prometheus`, `grafana`, `node-exporter`, `dashdot`, `homepage`,
`homarr`, `dashy`, `jellyfin`, `sonarr`, `radarr`, `prowlarr`, `jellyseerr`, `nextcloud`, `filebrowser`

`frigate`, `photoprism`, `sabnzbd` and `kube-state-metrics` were never wired in. `00-namespaces` must
still go first: it owns the namespaces that more than one service directory shares.

### Linting

```bash
make lint-kubeconform   # kubeconform -strict against Kubernetes 1.37.1, the version Talos ships
```

## CI

Every pull request and every push to `main` runs `.github/workflows/ci.yml`: every linter, the unit, policy
and idempotence tests on Python 3.12, 3.13 and 3.14, `make lint-kubeconform`, a gitleaks scan of the commits
the change adds (secrets and network identifiers), and actionlint, zizmor and shellcheck
over the workflows and scripts. `ci-success` aggregates the jobs and is the one check for the "Protect main"
ruleset to require; `.github/rulesets/main.json` holds that ruleset's body, applied through the GitHub API. No
job gets a secret, and none reaches the home network: the roles are tested only against the fakes in
`tests/integration`. Dependabot proposes weekly updates to the SHA-pinned actions and every package in the
Python lock. The actionlint, kubeconform and gitleaks versions and the schema pins are bumped by hand;
CLAUDE.md lists them.
