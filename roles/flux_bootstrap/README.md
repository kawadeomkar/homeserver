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
- **The FluxInstance `flux`**: the Flux version, and the Git repository, branch and path it syncs. From Flux 2.9,
  kustomize-controller substitutes strictly by default, so a manifest naming a setting that does not exist fails
  rather than getting an empty string; nothing sets the feature gate, which a later release could drop.

Each object is compared with `kubectl diff --server-side` and applied with `kubectl apply --server-side`, both
under the field manager `homeserver-ansible`, only when it differs. A dry run before the operator exists says what
a real run would install; the diffs need the operator's namespace and its FluxInstance definition.

The FluxInstance is owned by this role, never committed under the synced path, which would give it two owners.
Never install Flux through Talos's `KubeExternalManifestConfig`: Talos re-applies and prunes it on upgrades.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `flux_bootstrap_kubeconfig` | `""` | The cluster's kubeconfig |
| `flux_bootstrap_operator_version` | `""` | The operator's release, such as `v0.61.0`; empty does nothing |
| `flux_bootstrap_operator_url` | the operator's GitHub releases | `<url>/<version>/install.yaml` is applied |
| `flux_bootstrap_instance` | `{}` | `version` (a Flux minor, `2.9.x`), `url` (https), `ref`, `path` |
| `flux_bootstrap_settings` | `{}` | The `cluster-settings` ConfigMap's keys and values, all strings |
| `flux_bootstrap_age_key_file` | `""` | The age key file; one key, mode 0600 |
| `flux_bootstrap_wait_timeout` | `10m` | How long a real run waits for the operator and for the FluxInstance |

## Example

```yaml
flux_bootstrap_kubeconfig: "{{ lookup('env', 'HOME') }}/.kube/homelab"
flux_bootstrap_operator_version: v0.61.0
flux_bootstrap_instance: {version: "2.9.x", url: "https://github.com/<you>/homelab.git", ref: refs/heads/main, path: clusters/home}
flux_bootstrap_settings: {DOMAIN: home.example.org}
flux_bootstrap_age_key_file: "{{ lookup('env', 'HOME') }}/.config/sops/age/homelab.agekey"
```

Flux's controllers hold cluster-admin: whoever can merge to the synced branch controls the cluster.
