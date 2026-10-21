# Intel's GPU plugin

Hands the host's iGPU, passed whole to the worker (`scripts/runbooks/gpu.md`), to the pods that ask for
`gpu.intel.com/i915`. Applied by the Flux Kustomization `infra-gpu`, after `infra-controllers`.

**`intel-gpu-plugin.yaml` is upstream's `deployments/gpu_plugin/base/intel-gpu-plugin.yaml` from
[intel-device-plugins-for-kubernetes](https://github.com/intel/intel-device-plugins-for-kubernetes) v0.37.1
(commit `6e5cf25a`), with one edit:** the DaemonSet's namespace, which `tests/policy/test_k8s.py` reads from the raw
file. Every other change is in `kustomization.yaml`: the image pinned by digest, two pods sharing the device
(`-shared-dev-num=2`) and the node selector. Vendored rather than fetched from Intel's repository at a tag, so that
a tag moved upstream changes nothing here, every check reads it (kubeconform, kube-linter, the pinned hostPaths and
Pod Security in `test_k8s.py`), and a failed fetch cannot hold the platform back.

**Bumping it** is by hand, the whole file at once, each quarter with the other hand-bumped pins (CLAUDE.md).
Renovate leaves this directory alone (`renovate.json`). To bump:

1. Replace `intel-gpu-plugin.yaml` with the new release's `deployments/gpu_plugin/base/intel-gpu-plugin.yaml`, and
   put back the DaemonSet's `namespace: intel-gpu-plugin`.
2. Pin the new image's tag and digest in `kustomization.yaml` (`newTag`, `digest`: with a digest alone, kustomize
   drops the tag)
   (`docker buildx imagetools inspect intel/intel-gpu-plugin:<version>`), and update the release and commit above.
3. Check the patches still apply (`make lint TYPE=manifests`), that the release added no `args` the patch would replace,
   and that `make lint TYPE=k8s` and `make test TYPE=policy` pass: a new hostPath or privilege fails `test_k8s.py` until
   it is pinned there, with a reason.
