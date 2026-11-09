# Bumping the pins nothing else bumps

Dependabot updates the SHA-pinned actions and every package in `poetry.lock` (`.github/CLAUDE.md`). Renovate is set up
for the container images, the HelmRelease chart versions and the OCIRepository tags under `k8s/` (`k8s/CLAUDE.md`),
but runs only once the owner installs the Mend Renovate app (`TODO.md`): until then those are bumped by hand too
(below). Neither bot can see the pins below, which are bumped by hand each quarter, each together with its integrity
value: a version bumped alone fails CI's download check, a policy test or the role that reads it.

## CI's tools (`.github/workflows/ci.yml`)

- `ACTIONLINT_VERSION`, `KUBECONFORM_VERSION`, `KUBE_LINTER_VERSION`, `GITLEAKS_VERSION`, `KUSTOMIZE_VERSION` and
  `HELM_VERSION`, each with its `_SHA256`.
- `TALOSCTL_VERSION` with its `_SHA256`, bumped with `talos_version` (below), which it must equal; a policy test
  checks.
- `JUST_VERSION` with its two checksums (`JUST_SHA256_X86_64`, `JUST_SHA256_AARCH64`, one per runner architecture).
  `set minimum-version` in the justfile must not be newer; a policy test checks.
- `POETRY_VERSION`, which has none and must stay within `pyproject.toml`'s `requires-poetry`.
- At the same bump, delete `.github/actionlint.yaml` if actionlint now knows `ubuntu-26.04`.

## The schemas kubeconform reads (`justfile`)

`kube_schema_ref` with `kubernetes_version` (Talos's default), and `kube_crd_schema_ref`.

## The cluster

- Until Renovate runs, every container image under `k8s/`, its tag and digest together (`k8s/CLAUDE.md`, "Every image
  is pinned by tag and digest"), and the charts' versions, each with its digest (or csi-driver-nfs's commit and path)
  and the images its release pins ("Every chart is pulled by something its publisher cannot move"). FileBrowser
  Quantum publishes security releases often.
- local-path-provisioner's vendored manifest, which stays a hand bump
  (`k8s/infrastructure/controllers/common/local-path/README.md`), and Intel's GPU plugin
  (`k8s/infrastructure/gpu/README.md`).
- The Flux Operator in `group_vars/kubernetes/flux.yml`: `flux_bootstrap_operator_version` with its `install.yaml`'s
  SHA-256 and its image's digest, and the FluxInstance's exact Flux release with the manifests artifact's digest.
  `roles/flux_bootstrap` refuses them unpinned.
- The Talos pins in `group_vars/all/kubernetes.yml`: `talos_version`, `talos_contract` and `talos_kubernetes_version`,
  which must equal the justfile's `kubernetes_version` (a policy test checks), with the ISO's SHA-256 and each
  schematic's installer digest at the new version, since the Image Factory publishes no checksum. Keep the installer
  digests of the release the nodes run until `just talos-upgrade` has moved them: a running node's configuration is
  rendered with its own release's installer, which without its digest would be a bare tag that every run would want
  to apply, and nothing refuses the removal. `just talos-upgrade` moves them onto the cluster
  (`scripts/runbooks/talos.md`).

## Home Assistant

Home Assistant OS's OVA: `haos_version` with `haos_ova_sha256`, GitHub's digest of the release asset, in
`group_vars/proxmox/haos.yml`. It only ever makes a new VM, since Home Assistant updates itself
(`scripts/runbooks/haos.md`).
