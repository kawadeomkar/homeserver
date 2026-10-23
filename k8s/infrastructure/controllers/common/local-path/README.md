# local-path-provisioner

The cluster's default StorageClass, `local`: a directory per volume on the node's data volume
(`/var/mnt/local-path-provisioner`, a Talos user volume on the VM's data disk). Upstream publishes no Helm
repository, so its manifest is vendored, which also lets kubeconform and kube-linter read it.

**`local-path-storage.yaml` is upstream's `deploy/local-path-storage.yaml`, with one edit:** the Namespace's Pod
Security labels, which `tests/policy/test_k8s.py` reads from the raw file and a patch would hide. Every other change
is a patch in `kustomization.yaml`: the StorageClass renamed `local`, made the default, `Retain`, its volumes of type
`local`; requests on the provisioner; the node path; the helper pod's image pinned by digest.

**Bumping it** is by hand, the whole file at once, each quarter with the other hand-bumped pins (CLAUDE.md), never
the image tag alone, which would upgrade the binary while its RBAC, configuration and helper pod stayed old.
Renovate leaves this directory alone (`renovate.json`). To bump:

1. Replace `local-path-storage.yaml` with the new release's `deploy/local-path-storage.yaml`.
2. Put back the comment and the Namespace's three `pod-security.kubernetes.io/*: privileged` labels.
3. Check the patches still apply (`make lint TYPE=manifests`), and the new release's notes for changes to the
   ConfigMap's keys, which the patches replace whole.
