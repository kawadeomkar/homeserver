# homeserver

Ansible control-node repo that provisions one Ubuntu homeserver and runs ~20 self-hosted services on
single-node k3s. Two layers: Ansible for host configuration, hand-maintained Kubernetes manifests for the
workloads. Bulk storage lives on a separate TrueNAS box over NFS; the host is reached over Tailscale.

> **Status: nothing here is currently deployed.** The cluster has been through minikube → microk8s → k3s
> and is heading to **Proxmox + Talos Linux** next, so the Ansible layer is expected to be replaced rather
> than extended. See `CLAUDE.md` for the detailed architecture, the known landmines, and what survives
> that migration.

## Requirements

- Ansible >= 2.12 and Python 3
- SSH key at `~/.ssh/hs` (public half installed by the `main` role from `~/.ssh/hs.pub`)
- The Ansible Vault password in `./.vault_pass` — `ansible.cfg` points at it, so `--ask-vault-pass` is
  never needed locally
- An `inventory` file. It is **gitignored** (it holds a Tailscale address, and this repo is public), so a
  fresh clone must recreate it:

  ```ini
  [homeserver]
  <tailscale-ip>
  ```

```bash
ansible-galaxy install -r requirements.yml   # geerlingguy.docker + community.general, ansible.posix, kubernetes.core
```

## Usage

```bash
ansible-playbook playbook.yaml                 # full run: main → geerlingguy.docker → k8s
ansible-playbook playbook.yaml --tags setup    # base system + docker only
ansible-playbook playbook.yaml --tags k8s      # k3s install + provision only
ansible-playbook playbook.yaml --check         # dry run
ansible-playbook playbook.yaml --syntax-check
ansible-playbook setup-playbook.yml            # the k8s role on its own
```

`setup` and `k8s` are the only tags that exist (`playbook.yaml:6-12`). There is no per-task tagging, so a
narrower run needs `--start-at-task` or a temporary playbook.

Secrets: `ansible-vault edit group_vars/all/vault.yml`. Kubernetes Secrets are **not** kept there — see
`k8s/README-secrets.md`.

### Linting

```bash
yamllint .
ansible-lint playbook.yaml
kubeconform -summary -ignore-missing-schemas $(find k8s -name '*.yaml')
```

There is no CI, so none of this runs automatically. `kubeconform` is the only one of the three that
currently works on a typical checkout — see the toolchain note in `CLAUDE.md`.

The helper scripts (`run_tests.sh`, `run_vagrant.sh`, `run_homeserver.sh`) are **broken**: they reference
`playbook.yml` (the file is `playbook.yaml`), and `run_tests.sh` passes `--check-syntax` rather than
`--syntax-check`. Use the commands above instead.

## Layout

```
playbook.yaml            main → geerlingguy.docker → k8s
setup-playbook.yml       k8s role alone
group_vars/all/          vars.yml (plain) + vault.yml (encrypted)
roles/main/              host configuration — one task file per concern
roles/k8s/               k3s install + `kubectl apply` of the manifests
roles/geerlingguy.docker galaxy-installed, gitignored — never edit
roles/containers/        legacy docker-compose, unreferenced by playbook.yaml
k8s/                     one directory per service (+ 00-namespaces/ for shared ones)
docker/certbot/          leftover; the docker-compose.yml it belonged to is gone
```

## Roles

### `main` — host configuration

`tasks/main.yml` dispatches one include per concern, gated on variables rather than tags:

| Task file | Gate | Notes |
|---|---|---|
| `setup.yml` | always | user + groups, passwordless sudo, apt upgrade, packages, pip k8s libs |
| `ssh.yml` | always | hardens `sshd_config`, `AllowUsers`, validates with `sshd -T` |
| `env.yml` | `is_hosting_music_bot` | writes `DISCORD_TOKEN` to `/etc/environment` |
| `tailscale.yml` | `tailscale_auth_key is defined` | subnet router + exit node; IP forwarding sysctls |
| `filesystem.yml` | always | creates `/opt/<service_dirs>` |
| `nfs.yml` | `is_nfs` (default false) | mounts the NAS and mirrors `service_dirs` onto it |
| `fail2ban.yml` | Debian | installs only; jail config is a TODO |

Not wired in: `ubuntu-ufw.yml` (deferred until ingress ports settle), `setup-pihole.yml` (needs router
DNS), `acl.yml` (unreferenced).

`tailscale_auth_key` and `tailscale_advertise_routes` are undefined in plain vars — they must come from
vault or `-e`, and the whole include is skipped silently if the key is absent.

**This role does not clone the repo onto the target.** Nothing does; see below.

### `k8s` — workloads

`install_k3s` → `setup` → `provision`. `provision.yml` applies `k8s/00-namespaces/` first, then one
`kubectl apply -f` per service directory, then waits for all pods.

**`k8s_manifests_path` is `/homeserver/k8s` on the target host, not this working copy**, and no task syncs
it. Local manifest edits therefore have no effect until that server-side checkout is updated by hand. This
is the most common reason a manifest change appears to do nothing.

Adding a directory under `k8s/` does not deploy it — it needs a `provision.yml` entry too.
`install_helm.yml` and `teardown.yml` exist but are deliberately not included by `main.yml`.

### `containers`

Legacy docker-compose role, unreferenced by `playbook.yaml`. The `docker-compose.yml` it invokes no longer
exists.

## Configuration

Variables live in three places, deliberately:

- `group_vars/all/vars.yml` — cross-role values: paths, NFS settings, `service_dirs`, k8s settings
- `group_vars/all/vault.yml` — encrypted secrets, surfaced by indirection
  (`discord_token: "{{ vault_discord_token }}"`)
- `roles/<role>/defaults/main.yml` — role tunables (`packages`, `ssh_port`, `is_nfs`, `ufw_rules`, …)

Rather than duplicating those tables here (they drifted last time), read the files — they are short and
commented. `ansible.cfg` pins `ansible_user = omkar`, `private_key_file = ~/.ssh/hs`, and
`vault_password_file = ./.vault_pass`.

Note several declared variables are inert: `metallb_cidr`, `lan_subnet`/`lan_base`, and `is_prod_run` are
referenced by no task.

## Local testing

```bash
vagrant up    # Ubuntu 22.04, provisioned with playbook.yaml
```

`Vagrantfile` expects an extra vault file `@vm-vault.enc` that is gitignored and not present in a fresh
clone, so this needs setup before it will run.
