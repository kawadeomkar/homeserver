# flux_bootstrap

Hands a Kubernetes cluster to Flux from the controller, with `kubectl`: installs or upgrades the
[Flux Operator](https://fluxoperator.dev), then keeps three objects as configured. With only its defaults it does
nothing.

- **`flux-system/sops-age`**: the age private key that decrypts the repository's SOPS secrets, rendered by
  `kubectl create secret --dry-run=client` from `flux_bootstrap_age_key_file`. Never printed: the file, its
  Secret and their diffs stay out of the output. The file must hold exactly one key and nothing but comments,
  readable by its owner alone, since the whole file goes into the cluster: never a keyring shared with other
  projects.
- **`flux-system/cluster-settings`**: a ConfigMap of `flux_bootstrap_settings`, which Flux Kustomizations
  substitute into their manifests (`postBuild.substituteFrom`). Strings only. A key removed here disappears.
- **The FluxInstance `flux`**: an exact Flux release, built from the operator's manifests artifact pinned by
  digest, and the Git repository, branch and path it syncs. From Flux 2.9,
  kustomize-controller substitutes strictly by default, so a manifest naming a setting that does not exist fails
  rather than getting an empty string; nothing sets the feature gate, which a later release could drop.

The operator is installed from its release's `install.yaml` only once the file has the pinned SHA-256
(`flux_bootstrap_operator_sha256`), with its image given the pinned digest beside the tag
(`flux_bootstrap_operator_digest`); it is installed again when the running image is not that tag and digest.

Each object is compared with `kubectl diff --server-side` and applied with `kubectl apply --server-side`, both
under the field manager `homeserver-ansible`, only when it differs. A dry run before the operator exists says what
a real run would install; the diffs need the operator's namespace and its FluxInstance definition.

`flux_bootstrap_sync_exclude` has the sync leave part of the repository out, through the FluxInstance's own
patches (`spec.kustomize.patches`), which the operator applies to the sync Kustomization `flux-system` it renders. An
entry with a `kustomization` alone drops that Flux Kustomization from the sync, which prunes it and what it applied,
bar objects marked `kustomize.toolkit.fluxcd.io/prune: disabled`; all such entries share one delete, whose target
names them in one regular expression, and a target that matches nothing is no error. An entry with a `selector` too
keeps the Kustomization and adds a delete by that label selector to its own `spec.patches`, so its build drops the
matching objects, which it then prunes. Empty, the FluxInstance carries no patch. A real run then reads the sync back
and fails if it does not carry the patches, waits for each Kustomization dropped to be deleted and for each kept one
to have applied its new patch, and deletes the Deployments it left out that carry
`kustomize.toolkit.fluxcd.io/reconcile: disabled`, which Flux's prune skips; a dry run names them.

A FluxInstance that already syncs another path is refused, in a dry run too, before anything is applied: Flux
prunes what the new path lacks, so moving it deletes everything the old one applied, the apps' volumes with
them. `flux_bootstrap_allow_path_change` lets one run do it.

A real run then waits for the operator, the FluxInstance and every Flux Kustomization to be ready, each for up to
`flux_bootstrap_wait_timeout`, shows the Kustomizations, and fails when one is not ready: the FluxInstance is ready
once its own sync has applied, while the Kustomizations it creates, which apply the apps, have conditions of
their own.

The FluxInstance is owned by this role, never committed under the synced path, which would give it two owners.
Never install Flux through Talos's `KubeExternalManifestConfig`: Talos re-applies and prunes it on upgrades.

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
| `flux_bootstrap_sync_exclude` | `[]` | What the sync leaves out: `{kustomization}` drops one, `{kustomization, selector}` drops the objects a label selector matches from its build |
| `flux_bootstrap_allow_path_change` | `false` | Let a run move the FluxInstance to another path, deleting what the old one applied |

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

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `make docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
