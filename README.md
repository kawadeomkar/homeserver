# homeserver

Infrastructure repo for a small homelab: hand-maintained Kubernetes manifests for ~20 self-hosted
services, plus (soon) the Ansible that configures the machines they run on.

> **Status: nothing here is currently deployed, and no Ansible roles exist yet.** The repo used to
> provision a single Ubuntu host running k3s, with storage on a TrueNAS CORE box over NFS. That host layer
> was removed; it is in the git history if it is ever needed. See `CLAUDE.md` for the detailed
> architecture of the manifests, the known landmines, and what survives the migration.

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
group_vars/all/vault.yml Ansible Vault file, kept for the roles that will replace the old ones
ansible.cfg              inventory path and vault password file
```

`inventory` and `./.vault_pass` are gitignored and must be created locally. Kubernetes Secrets are **not**
kept in the vault — see `k8s/README-secrets.md`.

## Manifests

Nothing in this repo applies the manifests any more. The removed `k8s` role ran one `kubectl apply -f`
per directory, in this order:

`00-namespaces`, `homeassistant`, `prometheus`, `grafana`, `node-exporter`, `dashdot`, `homepage`,
`homarr`, `dashy`, `jellyfin`, `sonarr`, `radarr`, `prowlarr`, `jellyseerr`, `nextcloud`, `filebrowser`

`frigate`, `photoprism`, `sabnzbd` and `kube-state-metrics` were never wired in. `00-namespaces` must
still go first: it owns the namespaces that more than one service directory shares.

### Linting

```bash
kubeconform -summary -ignore-missing-schemas $(find k8s -name '*.yaml')
```

There is no CI, so this does not run automatically.
