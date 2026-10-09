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
| truenas | TrueNAS Community Edition; all Proxmox VM storage lives here. **Optional**: without one, VM disks stay on the Proxmox host (see [Without a NAS](#without-a-nas)) |
| opnsense | Router and firewall for the LAN |

Hardware details are in `docs/MACHINES.md`, which is **gitignored** (this repo is public, and it holds
addresses and serial numbers), so it is absent from a fresh clone.

## Layout

```
k8s/                     one directory per service (+ 00-namespaces/ for shared ones)
roles/truenas/           configures the NAS through its API: address, pools, datasets, NFS
roles/proxmox_storage/   adds the NAS's shares to Proxmox as qcow2 VM-disk storage, or checks its own storage
truenas.yml proxmox.yml  one playbook per role; site.yml runs both, NAS first
group_vars/all/vault.yml Ansible Vault file: the TrueNAS API key
group_vars/all/local.yml.example  template for local.yml, which holds every address and serial
group_vars/all/storage.yml        VM storage layout shared by both plays, and the switch for a setup without a NAS
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
make lint                         # yamllint, ansible-lint, ruff, module docs, GitHub config schemas, tests
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

### Without a NAS

A setup with only a Proxmox host works from the same files. Delete the TrueNAS section from
`group_vars/all/local.yml`, or leave every value in it blank: with no NAS address, `group_vars/all/storage.yml`
sets `nas_present` to false, the NAS play says so and ends before its role runs, and the Proxmox play adds no
NFS storage and sets no start-on-boot delay. There is then one VM storage, the one the Proxmox installer
created: `local-lvm`, or on a ZFS install the `local-zfs` you set as `proxmox_local_vm_storage` in
`group_vars/all/local.yml`. The role checks that it exists, is enabled and holds `images`, and on a real run
that it is active. It creates nothing. The persistent VM class exists only with a NAS: without one every VM
is ephemeral in the sense that its disk goes with the Proxmox boot disk, and no project pools (below) exist.

A TrueNAS section that keeps its other settings but gives no address stops both playbooks before anything
runs, rather than passing for a setup without a NAS: that is what a misspelt address looks like. The message
names the settings it found.

Two things still apply. The inventory keeps the `truenas` host: the addresses decide whether a NAS exists,
not the inventory. And `.vault_pass` must exist, because `ansible.cfg` names it, and the tracked vault is
decrypted on every run. Nothing in it is needed without a NAS, so replace it with an empty vault of your own:

```bash
make vault-init
```

That writes a new password to `.vault_pass`, readable by you alone, and an empty vault encrypted with it over
`group_vars/all/vault.yml`. It then tells git to leave that file out of your commits
(`git update-index --skip-worktree`), so your vault never reaches a commit or a pull request. A vault that
already opens with `.vault_pass` is left alone. When the tracked vault changes upstream, `git pull` refuses,
saying your local changes to `group_vars/all/vault.yml` would be overwritten. Put the tracked file back, pull,
and make your vault again, which keeps your password:

```bash
git update-index --no-skip-worktree group_vars/all/vault.yml
git checkout -- group_vars/all/vault.yml
git pull
make vault-init
```

This mode is covered by the fake-backed tests and the argument-spec pass in both modes; the maintainer's own
setup has a NAS.

### Storage for projects that create VMs

A project that creates VMs on the Proxmox host, such as `claude-on-proxmox`, names the storage its disks go to
and knows nothing else about it: not this repo, not whether a NAS exists. Proxmox has no host-wide default
storage, so the storage id is the whole interface, like the bridge name.

- **With a NAS**, this repo provisions a pool per project. An entry in `vm_storage_consumers`
  (`group_vars/all/storage.yml`) gives it a dataset of its own on the ephemeral pool, an NFS export, and a
  Proxmox storage under the project's id; the first is `claude-on-proxmox-ephemeral`. After `make site`, grant
  the project's token its pool with the line from that project's README (`pveum aclmod /storage/<id> …`) and
  set the id in the project's own configuration (`proxmox_storage`, for `claude-on-proxmox`). An optional
  `quota` on the entry caps the project; the parent dataset's 1 TiB quota applies regardless.
- **Without a NAS**, nothing: the project's default, the installer's `local-lvm`, is right, and no pool exists.

Ids say who owns a storage and what it is for, never what is behind it: `homeserver-persistent` and
`homeserver-ephemeral` for this repo's two VM classes, `<project>-ephemeral` for a project's pool. An id does
not change with the backend. Renaming one is a migration: the role never removes a storage, so the old id comes
out by hand first (`pvesm remove <id>`), or the two ids would share one export. This repo manages no Proxmox
users or permissions, and a project never gets `Datastore.Allocate`: in Proxmox that right, granted on
`/storage` for creating a storage, also allows changing or removing every other one. One cosmetic effect: the
GUI's "Create VM" wizard preselects the first storage by id, so with a NAS it preselects a project's pool.
Tools name their storage and are unaffected.

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
