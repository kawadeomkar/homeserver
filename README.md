# homeserver

Ansible playbook for provisioning and configuring a personal homeserver running Ubuntu. Manages system setup, SSH hardening, Docker, Kubernetes (k3s), and optional services.

## Requirements

- Ansible >= 2.12
- Python 3
- SSH key at `~/.ssh/hs` with access to the target host
- Ansible Vault password (for secrets)

Install role and collection dependencies:

```bash
ansible-galaxy install -r requirements.yml
```

## Project Structure

```
.
├── ansible.cfg
├── inventory
├── playbook.yml
├── requirements.yml
├── group_vars/
│   └── all/
│       ├── vars.yml        # plain variables
│       └── vault.yml       # ansible-vault encrypted secrets
├── roles/
│   ├── main/               # base system configuration
│   ├── containers/         # docker container management
│   └── k8s/                # k3s + kubectl setup
├── docker/
│   └── docker-compose.yml  # Home Assistant
└── k8s/                    # kubernetes manifests
```

## Roles

### `main`

Base system provisioning. Runs on all hosts.

| Task file        | Description                                      |
|------------------|--------------------------------------------------|
| `setup.yml`      | Creates user, installs packages, clones repo     |
| `ssh.yml`        | Hardens sshd\_config, restricts allowed users    |
| `env.yml`        | Writes secrets to `/etc/environment`             |
| `fail2ban.yml`   | Installs and enables fail2ban (Debian only)      |
| `ubuntu-ufw.yml` | Configures UFW firewall (currently disabled)     |

### `containers`

Manages Docker container filesystem layout and starts services via `docker-compose`.

> **Note:** This role is currently commented out in `playbook.yml`.

### `k8s`

Installs k3s and configures `kubectl` for the provisioned user. Also adds bash completion.

## Configuration

All configurable variables are listed below, grouped by their source file.

---

### `ansible.cfg`

| Setting              | Section          | Value              | Description                                          |
|----------------------|------------------|--------------------|------------------------------------------------------|
| `INVENTORY`          | `[defaults]`     | `inventory`        | Path to the inventory file                           |
| `roles_path`         | `[defaults]`     | `./roles`          | Directory Ansible looks for roles in                 |
| `private_key_file`   | `[defaults]`     | `~/.ssh/hs`        | SSH private key used to connect to hosts             |
| `ansible_user`       | `[defaults]`     | `omkar`            | Remote user for SSH connections                      |
| `pipelining`         | `[ssh_connections]` | `true`          | Reduces SSH operations by reusing connections        |

---

### `group_vars/all/vars.yml`

Plain variables applied to all hosts. Override per-host in `host_vars/<host>/` or per-group in additional `group_vars/` files.

| Variable                    | Default                            | Description                                        |
|-----------------------------|------------------------------------|----------------------------------------------------|
| `shell`                     | `/usr/bin/bash`                    | Login shell for the created user                   |
| `username`                  | `omkar`                            | Non-root user to create                            |
| `password`                  | `TODO`                             | User password (hashed with sha512 at apply time)   |
| `security_ssh_allowed_users`| `["omkar", "vagrant"]`             | Users permitted to connect over SSH                |
| `lan_subnet`                | `10.0.0.0/24`                      | LAN subnet CIDR                                    |
| `lan_base`                  | Derived from `lan_subnet`          | Base IP address, computed via regex (do not set manually) |
| `discord_token`             | `"{{ vault_discord_token }}"`      | Discord bot token, sourced from vault              |

---

### `group_vars/all/vault.yml` — Ansible Vault

Encrypted secrets. Edit with `ansible-vault edit group_vars/all/vault.yml`.

| Variable              | Description                                           |
|-----------------------|-------------------------------------------------------|
| `vault_discord_token` | Discord bot token, referenced as `discord_token`      |

To encrypt a new vault file:

```bash
ansible-vault encrypt group_vars/all/vault.yml
```

Optionally, store your vault password in a file and reference it in `ansible.cfg`:

```ini
vault_password_file = ~/.vault_pass
```

---

### `roles/main/defaults/main.yml`

Defaults for the `main` role. Override in `group_vars/`, `host_vars/`, or by passing `-e` at runtime.

**`setup.yml`**

| Variable   | Default                                                                                         | Description              |
|------------|-------------------------------------------------------------------------------------------------|--------------------------|
| `packages` | `vim`, `htop`, `curl`, `tmux`, `neofetch`, `speedtest-cli`, `git-all`, `snapd`, `python3-pip`, `net-tools`, `wireshark`, `nmap` | APT packages to install |

**`env.yml`**

| Variable               | Default | Description                                           |
|------------------------|---------|-------------------------------------------------------|
| `is_hosting_music_bot` | `false` | Set to `true` to write `DISCORD_TOKEN` to `/etc/environment` |

**`ssh.yml`**

| Variable   | Default | Description |
|------------|---------|-------------|
| `ssh_port` | `22`    | Port sshd listens on |

**`ubuntu-ufw.yml`**

| Variable    | Default                                  | Description                  |
|-------------|------------------------------------------|------------------------------|
| `ufw_rules` | Allow TCP port 22 on default interface   | List of UFW rules to apply   |

## Usage

Run the full playbook:

```bash
ansible-playbook playbook.yml --ask-vault-pass
```

Run against a specific host:

```bash
ansible-playbook playbook.yml -l homeserver --ask-vault-pass
```

Run with a specific tag (if defined):

```bash
ansible-playbook playbook.yml --tags ssh --ask-vault-pass
```

Dry run (check mode):

```bash
ansible-playbook playbook.yml --check --ask-vault-pass
```

## Local Testing (Vagrant)

A `Vagrantfile` is included for local testing against an Ubuntu 22.04 VM:

```bash
vagrant up
```

This provisions the VM using the same `playbook.yml`.

## Linting

```bash
./run_tests.sh
```

This runs `yamllint`, `ansible-lint`, and a syntax check. The [geerlingguy.docker](roles/geerlingguy.docker/) role and cert-manager manifests are excluded from linting (see [.ansible-lint](.ansible-lint)).
