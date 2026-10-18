# homeserver

Infrastructure repo for a small homelab: the Kubernetes manifests of a dozen self-hosted services, which Flux
applies, and the Ansible that builds the machines and the cluster they run on.

> **Status: built, not yet run.** The Ansible configures the NAS and the Proxmox host, builds the cluster's VMs,
> installs Talos Linux on them and hands the cluster to Flux, which applies `k8s/`. Every role is tested against
> local fakes; the cluster has not been built on the real machines yet. See `CLAUDE.md` for the detailed
> architecture and the known landmines.

## Target stack

| Machine | Role |
|---|---|
| homeserver | Proxmox hypervisor; Kubernetes runs in Talos Linux VMs on it |
| truenas | TrueNAS Community Edition; all Proxmox VM storage lives here. **Optional**: without one, VM disks stay on the Proxmox host (see [Without a NAS](#without-a-nas)) |
| opnsense | Router and firewall for the LAN |

Hardware details are in `docs/MACHINES.md`, which is **gitignored** (this repo is public, and it holds
addresses and serial numbers), so it is absent from a fresh clone.

## Layout

```
k8s/clusters/            what Flux syncs per setup (nas, local): Flux Kustomizations, one per path below
k8s/infrastructure/      the platform: controllers (MetalLB, Traefik, local-path, csi-driver-nfs, the GPU plugin), configs, static volumes, monitoring
k8s/apps/                the apps, one directory and namespace per group: media, nextcloud, photoprism, frigate, homepage
roles/truenas/           configures the NAS through its API: address, pools, datasets, NFS
roles/proxmox_apt/       sets Proxmox's package repositories (no subscription), upgrades it on every run, and says when it needs a reboot
roles/proxmox_storage/   adds the NAS's shares to Proxmox as qcow2 VM-disk storage, or checks its own storage
roles/proxmox_backup/    keeps the nightly vzdump job of the VMs
roles/proxmox_vm/        creates the cluster's VMs and keeps the settings it manages
roles/talos/             installs Talos on the VMs, keeps their configuration, bootstraps etcd once, upgrades on request
roles/flux_bootstrap/    installs the Flux Operator and hands the cluster to Flux, which syncs k8s/clusters/<setup>
truenas.yml proxmox.yml  one playbook per machine; kubernetes.yml builds the cluster; site.yml runs all three, NAS first
talos-upgrade.yml etcd-snapshot.yml  move the cluster to new pins; snapshot etcd
group_vars/all/vault.yml Ansible Vault file: the TrueNAS API key
group_vars/all/local.yml.example  template for local.yml, which holds every address and serial
group_vars/all/storage.yml        VM storage layout shared by both plays, and the switch for a setup without a NAS
group_vars/all/kubernetes.yml     the Kubernetes cluster's shape, its NAS datasets, and the checks on local.yml's Kubernetes section
group_vars/nas/, group_vars/proxmox/  connection and per-machine configuration
inventory.example        template for inventory (no addresses in it)
.gitleaks.toml           gitleaks rules for the identifiers this public repo must never hold
pyproject.toml           the Python tooling's pins, for Poetry; poetry.lock is its lock (make lock)
poetry.toml              Poetry's settings: a 7-day cooldown on new releases, wheels only, ./.venv
.github/                 CI (workflows/ci.yml), Dependabot, and the reviewed body of the main-branch ruleset
tests/unit/              tests for the TrueNAS modules' comparison logic and the cluster roles' planning filters
tests/policy/            tests of the repo's own rules: the lock, the vault, the ruleset, the leak checks
scripts/ci/              the leak checks CI and the git hooks run
scripts/runbooks/        procedures to follow by hand; REBUILD.md is what to do after a hardware failure
tests/integration/       idempotence tests against fake TrueNAS, Proxmox, talosctl and kubectl
SECURITY.md              how to report a vulnerability privately
```

`inventory`, `group_vars/all/local.yml` and `./.vault_pass` are gitignored and must be created locally
(`make init` copies the first two from their examples).
No address is ever written in a tracked file: tasks and role defaults read them from variables whose real
values live only in `local.yml`. Kubernetes Secrets are **not**
kept in the vault — see `k8s/README-secrets.md`.

## Ansible

```bash
make venv PYTHON=<python 3.12+>   # the tooling in .venv, synced from poetry.lock (needs Poetry: pipx install poetry)
make init                         # git hooks, plus inventory and local.yml from their examples
make lint                         # the lock, yamllint, ansible-lint, ruff, module docs, GitHub config schemas, tests
make lock                         # update poetry.lock after editing pyproject.toml
make test-idempotence             # each role run repeatedly against local fakes
make ci                           # what the CI gate runs, on one Python, apart from the workflow linters
make check-truenas && make truenas
make check-proxmox && make proxmox
```

Each TrueNAS stage can be run on its own with `TAGS=truenas_network`, `truenas_pools`, `truenas_system`,
`truenas_datasets`, `truenas_snapshots`, `truenas_nfs`, `truenas_alerts` or `truenas_services`. Always run the check first: the idempotence tests use
fakes, which cannot prove the real machines behave the same. Every `make proxmox` also refreshes the package lists
and upgrades the Proxmox host, and says when an upgrade of the kernel, its firmware, the CPU's microcode or dbus waits for a reboot,
which is then done by hand.

To give a playbook a variable for one run, put Ansible's own flags in `ANSIBLE_ARGS`:
`make kubernetes ANSIBLE_ARGS='-e talos_allow_bootstrap=true'`, or `ANSIBLE_ARGS='--limit homeserver'` for one
host's play. `make kubernetes -e …` passes nothing to Ansible: make reads `-e` as its own option.

Alert e-mail is optional: with `truenas_mail_server` and `truenas_alert_email` in `local.yml`, and the server's
password in the vault as `vault_truenas_mail_password` (`make vault-edit`), the NAS mails its warnings, a
degraded or filling pool among them. Send a test from the TrueNAS UI (Alerts → the e-mail service → Send Test
Alert) after the first run. Changing the password in the vault is enough: the next run sends it.

The roles are generic and change nothing by default; this setup's configuration is in `group_vars/`
(`all/storage.yml`, `nas/truenas.yml`, `proxmox/apt.yml`, `proxmox/storage.yml`), with addresses and serials in the git-ignored
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
decrypted on every run. The tracked vault holds the maintainer's NAS key, which is no use without a NAS, so
replace it with a vault of your own:

```bash
make vault-init
```

That writes a new password to `.vault_pass`, readable by you alone, and an empty vault encrypted with it over
`group_vars/all/vault.yml`. It then tells git to leave that file out of your commits
(`git update-index --skip-worktree`), so your vault never reaches a commit or a pull request. A vault that
already opens with `.vault_pass` is left alone, and one that does not but is not the tracked vault either is
refused, since it may be yours with its password lost.

Your vault holds whatever secrets you put in it and exists nowhere else: keep a copy of it, with `.vault_pass`,
in your password manager, and never replace it with a new one. (A cluster's secrets bundle is never in a vault: it
lives in `~/.talos/homeserver`, `scripts/runbooks/talos.md`.) When the tracked vault changes upstream, `git pull`
refuses, saying your local changes to `group_vars/all/vault.yml` would be overwritten. Set your vault aside, put
the tracked file back, pull, and put yours back:

```bash
cp -p group_vars/all/vault.yml ~/homeserver-vault.yml
git update-index --no-skip-worktree group_vars/all/vault.yml
git checkout -- group_vars/all/vault.yml
git pull
cp -p ~/homeserver-vault.yml group_vars/all/vault.yml
git update-index --skip-worktree group_vars/all/vault.yml
make vault-init && rm ~/homeserver-vault.yml
```

The last `make vault-init` only checks: it says `nothing to do` when your vault opens with `.vault_pass`. Remove
the copy only then.

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
  `quota` on the entry, in bytes or with a binary suffix such as `200G`, caps the project; the parent dataset's
  1 TiB quota applies regardless.
- **Without a NAS**, nothing: the project's default, the installer's `local-lvm`, is right, and no pool exists.

Ids say who owns a storage and what it is for, never what is behind it: `homeserver-persistent` and
`homeserver-ephemeral` for this repo's two VM classes, `<project>-ephemeral` for a project's pool. An id does
not change with the backend. Renaming one is a migration: the role never removes a storage and refuses to
mount an export under a second id, so the old id comes out by hand first (`pvesm remove <id>`, once
`pvesm list <id>` shows nothing on it); until then a dry run refuses and names both ids. This repo manages
no Proxmox users or permissions, and a project never gets `Datastore.Allocate`: in Proxmox that right,
granted on `/storage` for creating a storage, also allows changing or removing every other one. One cosmetic
effect: the GUI's "Create VM" wizard preselects the first storage by id, so with a NAS it preselects a
project's pool. Tools name their storage and are unaffected.

### A Kubernetes cluster

`local.yml`'s Kubernetes section switches the cluster on, as the NAS addresses switch the NAS on: with no
`kubernetes_node_addresses` there is no cluster, and nothing below happens. With them, every playbook first
checks the section and names any setting it lacks. Fill in, from `local.yml.example`:

- an address per node, reserved in the router's DHCP outside its dynamic range: `homeserver-cp-1` and
  `homeserver-worker-1` with a NAS, `homeserver-cp-1` alone without one;
- a MAC per node, chosen by you (locally administered: lower case, the second hex digit 2, 6, a or e) and
  reserved in DHCP for that address before the first run;
- MetalLB's range and the two addresses Traefik and Frigate keep, outside the DHCP range;
- `flux_sync_url`, the repository the cluster syncs. It has no default: Flux applies whatever is there with
  cluster-admin rights, so a fork names its own;
- without a NAS, the node's size (`kubernetes_node_sizes`), with a media disk or
  `kubernetes_media_on_data_volume: true`.

Choose the MACs and reserve the addresses in the router before the first run: the VMs lease their address the
moment they start. If your `inventory` predates the cluster, add `[kubernetes]` with `homeserver-k8s` to it, as in
`inventory.example`; the cluster's playbook refuses to run without it. Then `make check-kubernetes` and
`make kubernetes` create the VMs on the Proxmox host, start them, report each one's MAC, install Talos on them,
bootstrap the cluster and write `~/.talos/homeserver/talosconfig` and `kubeconfig` (back that directory up).
It needs `talosctl` of the pinned minor release on this machine and the cluster's secrets bundle, made once with
`talosctl gen secrets` into `~/.talos/homeserver/secrets.yaml` and kept in your password manager, never in a
repository; `scripts/runbooks/talos.md` has the whole procedure, and upgrades, which only `make talos-upgrade`
makes. Last, it installs the Flux Operator and hands the cluster to Flux, which from then on
applies `k8s/clusters/nas` (or `local`) from the repository `flux_sync_url` names: Flux holds cluster-admin, so
whoever can merge to that branch controls the cluster.

Flux decrypts the cluster's secrets with an age key of its own, which the playbook uploads whole: make one for this
cluster alone, never a keyring shared with other projects, and keep a copy in your password manager:

```bash
mkdir -p ~/.config/sops/age && chmod 700 ~/.config/sops/age
age-keygen -o ~/.config/sops/age/homeserver.agekey && chmod 600 ~/.config/sops/age/homeserver.agekey
export SOPS_AGE_KEY_FILE=~/.config/sops/age/homeserver.agekey    # in your shell profile: sops on macOS reads no ~/.config by default
```

A node that stays down: `scripts/runbooks/dead-worker.md`. Their disks are never
moved, shrunk or deleted by the playbook, and it refuses to create a VM over the disks or backups an earlier one
left under the same VMID: restore it instead (`scripts/runbooks/vm-restore.md`). On thin LVM it also refuses disks
that would add up to more than the pool holds, other VMs' included. Taking a cluster down again, a test deployment
say, and what it brought with it: `scripts/runbooks/teardown.md`.

With a NAS, `make truenas` then creates the datasets the cluster mounts (`persistent/nextcloud`,
`persistent/photoprism`, `ephemeral/frigate`) and exports them, with the existing media dataset, to the worker
alone. Whenever `local.yml` changes, update its copy in your password manager.

### Backups

Every night at 02:00 the `vm-nightly` job backs up the cluster's VMs, and any other VM listed in `local.yml`'s
`vm_backup_extra_vmids` (Home Assistant's, say), keeping seven daily and four weekly backups. With a NAS they go
to the NAS (`homeserver-backup`, on the redundant pool). Without one, name a Proxmox storage that holds backups
in `vm_backup_storage`: a USB disk added as a directory storage, set up as a checked mount point
(`pvesm set <id> --is_mountpoint yes --create-base-path 0`, which the role insists on), or a Proxmox Backup
Server. Without it nothing is backed up, and every `make proxmox` says so. Restoring, and the drill that
proves a backup restores without disturbing the running cluster: `scripts/runbooks/vm-restore.md`.

### Home Assistant

Home Assistant runs as a VM of its own, Home Assistant OS, made by hand once (`scripts/runbooks/haos.md`): it must
keep working while the cluster is down. Its nightly backup comes from listing its VMID in `vm_backup_extra_vmids`,
and Homepage links to it when `local.yml` sets `haos_url`.

### The iGPU

If the Proxmox host has an Intel iGPU, Jellyfin can transcode and Frigate detect on it: pass it whole to the node
that runs the apps, following `scripts/runbooks/gpu.md` (VT-d, the host's `vfio-pci` binding and a PCI resource
mapping, all by hand), then name the mapping in `local.yml` as `kubernetes_gpu_mapping`. `make kubernetes` gives
the node the device and Talos's i915 driver, and the apps ask for it; restarting that VM and `make talos-upgrade`
are yours. Without it, the same manifests run both apps on the CPU.

### After a hardware failure

`scripts/runbooks/REBUILD.md` is the recovery procedure. A reinstalled NAS needs a new API user and key and an
updated certificate pin before Ansible can reach it, and a reinstalled Proxmox host needs root SSH access. Keep a
copy of what is not in this repo in your password manager: `group_vars/all/local.yml` and `.vault_pass`; with a
cluster, the age key (`~/.config/sops/age/homeserver.agekey`) and `~/.talos/homeserver`, whose `secrets.yaml` is
every key of the cluster; and without a NAS your own vault, `group_vars/all/vault.yml`.

## Manifests

Flux applies them. `roles/flux_bootstrap` points the cluster at `k8s/clusters/nas` (with a NAS) or
`k8s/clusters/local` (without), whose Flux Kustomizations apply, in order:

1. `k8s/infrastructure/controllers/`: MetalLB, Traefik, local-path-provisioner, and csi-driver-nfs with a NAS;
2. `k8s/infrastructure/configs/` (MetalLB's address pool) and `k8s/infrastructure/storage/` (the static volumes:
   the media library, Nextcloud's files, PhotoPrism's photos, Frigate's recordings);
3. `k8s/apps/`: `media` (Jellyfin, Sonarr, Radarr, Prowlarr, SABnzbd, Seerr, FileBrowser Quantum), `nextcloud`,
   `photoprism`, `frigate` and `homepage`.

Each app answers at `https://<app>.<domain>` through Traefik, Homepage at the domain itself, once the router has a
wildcard DNS record for `*.<domain>` pointing at `kubernetes_lb_traefik_ip` and `kubernetes_ingress_allowed_range`
lets your address in. A new cluster lets nobody in: several apps make their first visitor the admin, so set each one
up through a port forward first (`scripts/runbooks/first-run.md`), then open the LAN. The certificate comes from the
cluster's own CA, limited to the domain: trust it once on each device (`scripts/runbooks/tls.md`). Jellyfin also
answers plain HTTP, for its apps on TVs, which cannot trust a private CA. Flux reconciles `main` every minute:
a merged change reaches the cluster without anyone running anything. To see what Flux sees:
`flux get kustomizations` (KUBECONFIG as in `scripts/runbooks/talos.md`).

Monitoring is kube-prometheus-stack: Grafana answers at `https://grafana.<domain>`, with the admin password the
chart generated (CLAUDE.md, Monitoring, says how to read it).

Secrets are SOPS files (`k8s/README-secrets.md`); an app whose Secret you have not made yet waits for it. Each
app's first visit, through a port forward, is `scripts/runbooks/first-run.md`: the wizards and logins, Jellyfin's
transcode path, SABnzbd's folders and host whitelist (a fresh SABnzbd refuses every host name not on it, its own
ingress's included). Frigate's cameras come after.

**Moving the media library into place, once, with a NAS.** The media dataset predates the cluster: its old layout
had `jellyfin/config` and `jellyfin/data` under it. Before the media apps import anything, from a pod that mounts the
`media` claim (FileBrowser's, or a one-off busybox pod), move the content into the layout the apps use,
`/data/media/{tv,movies,...}` and `/data/usenet/{incomplete,complete}`, archive or delete the old `jellyfin/config`,
and once, as root, `chown -R 1000:1000 /data`: the apps' init containers fix only the mount point.

### Linting

```bash
make lint-k8s           # all of the below, and the Talos configurations
make lint-kubeconform   # kubeconform -strict against Kubernetes 1.37.1, the version Talos ships, and the CRDs
make lint-kustomize     # every kustomize directory built as Flux builds it (kustomize from Homebrew)
make lint-kube-linter   # kube-linter on each setup's manifests, built and with its settings substituted as Flux applies them
make lint-helm          # every chart rendered at its pin and checked like the repo's own manifests (helm from Homebrew)
```

## Remote access

Nothing here is published to the internet, and nothing should be. From outside, reach the LAN through
[Tailscale](https://tailscale.com), as one subnet router for the whole LAN: the apps, the machines' web UIs and
Home Assistant, without a Tailscale client on any of them. It is set up by hand; no role manages the router.

**On OPNsense** (the setup with a NAS):

1. Install the `os-tailscale` plugin: System → Firmware → Plugins, with "Show community plugins" ticked (OPNsense
   24.7 or later).
2. In its settings (VPN → Tailscale), log in with an auth key from Tailscale's admin console, and advertise the
   LAN's subnet.
3. In the admin console: approve that route on the router's machine, and disable its key expiry, since a router
   that must log in again cuts you off exactly when you are away.
4. In Interfaces → Assignments, assign the Tailscale interface and enable it, then allow traffic from it to the
   LAN in Firewall → Rules.
5. Names: in the admin console's DNS settings, add a nameserver restricted to the cluster's domain
   (`kubernetes_domain`, `homeserver.internal` by default) and to the router's local domain, pointing at
   OPNsense's LAN address. Unbound then answers with the same wildcard record as at home. If names do not
   resolve, let Tailscale's addresses query Unbound (Services → Unbound DNS → Access Lists).

**Without OPNsense** (a single Proxmox host behind another router): use the router's own Tailscale support if it
has one, or make the Proxmox host the subnet router. Install Tailscale from its Debian repository
([its instructions](https://tailscale.com/kb/1031/install-linux)), turn on IP forwarding, and advertise the
LAN:

```bash
printf 'net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n' > /etc/sysctl.d/99-tailscale.conf
sysctl -p /etc/sysctl.d/99-tailscale.conf
tailscale up --advertise-routes=<the LAN's subnet>
```

Then approve the route, disable key expiry and add the split DNS nameserver (the router's address) as in steps 3
and 5. `make proxmox` leaves Tailscale's repository file alone, but upgrades its package with the rest. The tunnel
goes down whenever the host reboots.

Traffic between the tailnet and the LAN is encrypted by Tailscale, so plain HTTP to the apps stays private on the
way; on the LAN itself it is still plain until the cluster has TLS (`TODO.md`).

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
