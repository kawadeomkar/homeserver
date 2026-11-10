# flux_bootstrap

Hands a Kubernetes cluster to Flux from the controller, with `kubectl`: installs or upgrades the
[Flux Operator](https://fluxoperator.dev), then keeps three objects as configured. With only its defaults it does
nothing.

- **`flux-system/sops-age`**: the age private key that decrypts the repository's SOPS secrets, rendered by
  `kubectl create secret --dry-run=client` from `flux_bootstrap_age_key_file`. Never printed: the file, its
  Secret, its rendering and their diffs stay out of the output (`no_log`). The file must hold exactly one
  `AGE-SECRET-KEY-` line and nothing but comments, mode 0600, since the whole file goes into the cluster: never a
  keyring shared with other projects, such as sops's `keys.txt`. The role reads only counts of its lines, never the
  key, and the refusal says to make one for the cluster alone with `age-keygen -o <file>`.
- **`flux-system/cluster-settings`**: a ConfigMap of `flux_bootstrap_settings`, which Flux Kustomizations
  substitute into their manifests (`postBuild.substituteFrom`). Strings only: Flux substitutes text. A key removed
  here disappears. It is labelled `reconcile.fluxcd.io/watch: Enabled`, so a change reconciles each Kustomization
  that reads it at once, rather than at its next interval (up to 10 minutes later). The sync Kustomization the
  operator renders (`flux-system`) neither substitutes nor decrypts, so the synced path holds Flux Kustomizations,
  each of which does both (`decryption` with `sops-age`, `postBuild.substituteFrom` with `cluster-settings`).
- **The FluxInstance `flux`**: an exact Flux release (a range would let the operator move Flux unreviewed), built
  from the operator's manifests artifact pinned by digest, and the Git repository, branch and path it syncs. From
  Flux 2.9, kustomize-controller substitutes strictly by default, so a manifest naming a setting that does not exist
  fails rather than getting an empty string (a misspelt `${DOMIAN}` would otherwise give every app the host
  `sonarr.`). Nothing sets the feature gate: a release that dropped it would stop kustomize-controller from starting.
  The FluxInstance carries no patch but what the sync leaves out (below).

The operator is installed from its release's `install.yaml` only once the file has the pinned SHA-256
(`flux_bootstrap_operator_sha256`), with its image given the pinned digest beside the tag
(`flux_bootstrap_operator_digest`); a release whose file does not name the operator's image exactly once is refused,
since its digest could not be pinned. It is installed again when the `flux-operator` Deployment is missing or its
running image is not that tag and digest, and a real run then waits for it to be available.

Each object is compared with `kubectl diff --server-side` and applied with `kubectl apply --server-side`, both
under the field manager `homeserver-ansible`, only when it differs. A client-side diff would rely on the annotation
server-side apply never writes, and would miss a key removed from the settings. A dry run before the cluster or the
operator exists says what a real run would install; the diffs need the operator's namespace and its FluxInstance
definition. A real run without the kubeconfig fails, saying that `roles/talos` writes it into `talos_config_dir`
once the cluster is bootstrapped.

`flux_bootstrap_sync_exclude` names Flux Kustomizations the sync leaves out, through the FluxInstance's own patches
(`spec.kustomize.patches`), which the operator applies to the sync Kustomization `flux-system` it renders. The
operator applies its patches to its whole build, the sync included, as its guide does
(`docs/guides/instance/instance-customization.md` in the operator's repository), though its API reference speaks of
the controllers alone. The patch adds to the sync's `spec.patches` one strategic-merge `$patch: delete`, whose target
names them all in one anchored regular expression: a target that matches nothing is no error, where a patch without
one that names an object the build lacks would fail the whole sync. Flux prunes each and what it applied, bar
objects marked `kustomize.toolkit.fluxcd.io/prune: disabled` (the claims and namespaces, so the data stays) and
Deployments `just suspend` holds (`kustomize.toolkit.fluxcd.io/reconcile: disabled`), which Flux's prune skips: they
stay at zero and come back held with their Kustomization. Empty, the FluxInstance carries no patch. A real run waits
for each one left out to be deleted; kubectl's wait passes for one already gone, and it times out should a release
of the operator stop passing its patches on to the sync.

A FluxInstance that already syncs another path is refused, in a dry run too, before any of the three objects is
applied (a real run has installed or upgraded the operator by then, which prunes nothing): Flux
prunes what the new path lacks, so moving it deletes everything the old one applied, the apps' volumes and their
data with them. `flux_bootstrap_allow_path_change` lets one run do it.

A real run then waits for the operator, the FluxInstance and every Flux Kustomization (`kubectl wait --all -A`) to be
ready, each for up to `flux_bootstrap_wait_timeout`, shows the Kustomizations (how many are ready and at which
revision, then kubectl's table, which says why one is not), and fails when one is not ready: the FluxInstance is
ready once its own sync has applied, while the Kustomizations it creates, which apply the apps, have conditions of
their own, and one can fail in one setup's tree only. Without that wait the run would end green over a cluster that
is not.

The FluxInstance is owned by this role, never committed under the synced path, which would give it two owners; it
differs per setup and per fork. Never install Flux through Talos's `KubeExternalManifestConfig`: Talos re-applies
and prunes it on upgrades.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `flux_bootstrap_kubeconfig` | `""` | The cluster's kubeconfig |
| `flux_bootstrap_operator_version` | `""` | The operator's release, such as `v0.61.0`; empty does nothing |
| `flux_bootstrap_operator_url` | the operator's GitHub releases | `<url>/<version>/install.yaml` is applied |
| `flux_bootstrap_operator_sha256` | `""` | The SHA-256 (hex) `install.yaml` must have; required with a version |
| `flux_bootstrap_operator_digest` | `""` | The operator image's digest (`sha256:...`); required with a version |
| `flux_bootstrap_instance` | `{}` | `version` (an exact Flux release, `2.9.6`), `artifact` (`oci://...@sha256:...`), `url` (https), `ref`, `path` |
| `flux_bootstrap_settings` | `{}` | The `cluster-settings` ConfigMap's keys and values, all strings |
| `flux_bootstrap_age_key_file` | `""` | The age key file; one key, mode 0600 |
| `flux_bootstrap_wait_timeout` | `10m` | How long a real run waits for the operator, the FluxInstance and the Kustomizations |
| `flux_bootstrap_sync_exclude` | `[]` | The Flux Kustomizations the sync leaves out |
| `flux_bootstrap_allow_path_change` | `false` | Let a run move the FluxInstance to another path, deleting what the old one applied |

## In this repository

`kubernetes.yml`'s second play runs the role on `homeserver-k8s` (local), after `roles/talos`, which writes the
kubeconfig it reads, `talos_config_dir`'s `kubeconfig` (`~/.talos/homeserver`, or `local.yml`'s
`kubernetes_talos_config_dir` for a second cluster). `group_vars/kubernetes/flux.yml` sets everything else:

- The operator and Flux pins, each bumped by hand with its pair (the release with its `install.yaml`'s SHA-256 and
  its image's digest, the Flux release with the manifests artifact's digest), as that file's comments say; the role
  refuses them unpinned.
- The FluxInstance syncs `flux_sync_url`, from `local.yml`, which has no default: Flux applies whatever the
  repository holds with cluster-admin rights, and a fork syncs its own (`kubernetes_settings_error` refuses it
  missing). The branch is `local.yml`'s `flux_sync_ref`, by default `refs/heads/main`, the one protected branch;
  `kubernetes_settings_error` refuses another without `flux_sync_ref_unprotected: true`, since whoever can push to
  the synced branch owns the cluster. The path is `k8s/clusters/nas` with a NAS and `k8s/clusters/local`
  without, so it changes only with the setup: the path refusal is what stops a `local.yml` rewritten for the other
  setup from pruning this one's apps and volumes.
- The age key file is `local.yml`'s `sops_age_key_file`, by default `~/.config/sops/age/homeserver.agekey`.
- `flux_bootstrap_sync_exclude` is the Flux Kustomization `apps-<group>` of each group of `kubernetes_app_groups`
  that `local.yml`'s `kubernetes_apps` does not name (`kubernetes_apps_enabled`, `group_vars/all/kubernetes.yml`).
  With every group there is no patch at all.

The settings every Flux Kustomization substitutes are all strings, and the addresses among them come from
`local.yml`, never written in a manifest. The NAS's address is `nas_address` in `group_vars/all`, since this host
never sees `group_vars/proxmox`. A `${VAR}` a manifest adds goes here and into `tests/fixtures/cluster-settings.yaml`.

| Key | Value |
|---|---|
| `DOMAIN` | `local.yml`'s `kubernetes_domain`, else `kubernetes_domain_default` (`homeserver.internal`) |
| `TZ` | `kubernetes_timezone` |
| `LB_RANGE`, `LB_TRAEFIK_IP`, `LB_FRIGATE_IP` | MetalLB's range and Traefik's and Frigate's addresses in it, from `local.yml` |
| `DATA_NODE` | `kubernetes_data_node`, the worker |
| `HOMEPAGE_SERVICES` | The sections of Homepage's `services.yaml` in order (`k8s/apps/homepage/tiles/`), space-separated: each group this host runs but `homepage`, in `kubernetes_app_groups`' order, then `home` (Home Assistant's) when `local.yml` sets `haos_url` |
| `GPU_I915`, `FRIGATE_DETECTOR_DEVICE` | `1` and `GPU` with `local.yml`'s `kubernetes_gpu_mapping` (the host's iGPU passed to the worker), so Jellyfin and Frigate ask for it and Frigate detects on it; else `0` and `CPU` (a limit of 0 asks for nothing and schedules anywhere) |
| `PROMETHEUS_RETENTION`, `PROMETHEUS_RETENTION_SIZE` | `15d` and `15GB` with a NAS, `7d` and `5GB` without, whose host is usually the smaller |
| `INGRESS_ALLOWED_RANGE` | Who Traefik serves: `local.yml`'s `kubernetes_ingress_allowed_range`, set once every app's first-run setup is done, and until then `127.0.0.1/32`, which no client has (`scripts/runbooks/first-run.md`) |
| `EGRESS_DENIED_ADDRESS` | The NAS's address, whose NFS exports trust the worker's address, which pods' traffic leaves the node with; without a NAS, the Proxmox host's |
| `PROXMOX_ADDRESS` | The Proxmox host's (`proxmox_address`), whose API and SSH answer the LAN. With `EGRESS_DENIED_ADDRESS`, the two hosts no app pod may reach (each app namespace's NetworkPolicy), the same address twice without a NAS, which is valid. Prometheus, outside the apps' namespaces, also scrapes the host's own metrics there (`roles/proxmox_node_exporter`) |
| `NAS_ADDRESS`, `NAS_POOL_PERSISTENT`, `NAS_POOL_EPHEMERAL`, `MEDIA_DATASET` | With a NAS only: `nas_address`, the pools' names (`nas_pools`) and the media dataset's path on the NAS, `<vm_storage_mount_root>/<ephemeral pool>/jellyfin` |
| `MEDIA_LOCAL_PATH`, `FRIGATE_LOCAL_PATH` | Without a NAS only: the library and the camera recordings on the worker's media disk, Talos's volume `media` (`/var/mnt/media/library`, `/var/mnt/media/frigate`), or, with `kubernetes_media_on_data_volume`, under its data volume (`/var/mnt/local-path-provisioner/static/media`, `.../frigate-media`), so one set of manifests serves both |
| `HAOS_URL` | Only when `local.yml` sets `haos_url`: Home Assistant OS's address, for Homepage's tile |

## Tests

`tests/integration/flux.yml` runs the role with this repository's configuration against `fake_talos_bin/`'s
kubectl (`tests/integration/fake_kubectl.py`), in `tests/integration/test_idempotence.py`'s `test_flux_bootstrap` (in
both setups), `test_flux_bootstrap_leaves_groups_out` and
`test_flux_bootstrap_refuses_a_shared_key_and_reports_a_stuck_operator`, all marked `cluster`. Between them they run
a dry run and a real run before the cluster exists, the first install (the operator pinned by digest, the exact
Flux release, the setup's own path and settings), a second run and a dry run that change nothing, the path refusal
in a dry run and a real one with nothing applied, then each allowed, a changed and a removed setting, an operator
bump refused while its `install.yaml` is not the pinned one and installed once it is, groups left out (a held
Deployment staying at zero) and brought back, an age key file with two keys, one others can read and one with
none, an operator that never becomes available and a Kustomization that is not ready. No run's output may hold the
age key, plain or base64.

The fake never fetches anything: its `-f` takes standard input or a file. The operator's `install.yaml` comes from a
release server in the test's own process (the `operator_releases` fixture), which `flux_bootstrap_operator_url`
points at and whose SHA-256 the test pins; the fake applies a file only when it holds a release, marked by its
`flux-system/flux-operator` Deployment, and records the operator's image. It applies and diffs server-side under
`homeserver-ansible` only and, as a real cluster does, fails both for a namespace that does not exist or a kind no
CRD defines. Its diff prints the whole new object, values included, as kubectl's does, so the tests prove the role
never shows the Secret's. Applying the FluxInstance stands in for the operator and Flux: the sync Kustomization
carries the FluxInstance's patches, and each Kustomization the state lists exists, ready, unless a patch deletes it,
which drops the Deployments it applied but those `just suspend` holds.

`tests/policy/test_roles.py`'s `test_flux_settings_resolve_on_the_kubernetes_host` resolves the settings and the
FluxInstance on `homeserver-k8s` through the real inventory layout, with a NAS, without one with a media disk, and
with the media on the data volume: the keys each setup has, every value a non-empty string, each media path on a
Talos volume the node has, and the synced path existing with everything its `kustomization.yaml` lists.
`test_flux_is_new_enough_to_substitute_strictly` keeps the Flux release at 2.9 or later, since the role sets no
feature gate.

## Example

```yaml
flux_bootstrap_kubeconfig: "{{ lookup('env', 'HOME') }}/.kube/homelab"
flux_bootstrap_operator_version: v0.61.0
flux_bootstrap_operator_sha256: <sha256sum of that release's install.yaml>
flux_bootstrap_operator_digest: sha256:<the digest of ghcr.io/controlplaneio-fluxcd/flux-operator:v0.61.0>
flux_bootstrap_instance: {version: "2.9.6", artifact: "oci://ghcr.io/controlplaneio-fluxcd/flux-operator-manifests@sha256:<digest>", url: "https://github.com/<you>/homelab.git", ref: refs/heads/main, path: clusters/home}
flux_bootstrap_settings: {DOMAIN: home.example.org}
flux_bootstrap_age_key_file: "{{ lookup('env', 'HOME') }}/.config/sops/age/homelab.agekey"
```

Flux's controllers hold cluster-admin: whoever can merge to the synced branch controls the cluster.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
