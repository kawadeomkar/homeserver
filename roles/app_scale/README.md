# app_scale

Scales a Flux-managed cluster's apps to zero and back from the controller, with `kubectl`: what `make down` and
`make up` run. With only its defaults it does nothing.

An **app** is a Deployment that a Flux Kustomization whose path lies under `app_scale_path` applies, as the labels
Flux puts on it say; a **group** is the apps of one namespace. `app_scale_only` names apps or groups (a name that is
both, such as `nextcloud`, means the group); empty means every app. A name that is neither is refused, with the list
of both, unless `app_scale_other_names` holds it, for another play to act on.

- **Down** patches each app once: Flux's own annotation `kustomize.toolkit.fluxcd.io/reconcile: disabled`, which
  kustomize-controller reads on the object in the cluster and then neither applies nor prunes it, the replicas to
  come back to in `app_scale_annotation`, and zero replicas. A plain `kubectl scale` would be undone at Flux's next
  reconciliation; the one patch means Flux never finds an app at zero without the annotation. It then waits for
  each app's pods to be gone. An app already down is left as it is, so a second run changes nothing.
- **Up** patches each app that down stopped: the recorded replicas, and both annotations removed. It then waits
  for each to be available (`kubectl rollout status`). Flux takes the app back at its next reconciliation, applying
  whatever changed in Git meanwhile.
- Apps carrying `app_scale_late_label` (their databases) stop in a second wave, once the others' pods are gone,
  and start in a first, so an app never loses its database under it.

Claims, Services, Ingresses and the platform are left alone: data stays, and so does Flux, which is what keeps an
app down across reboots and upgrades and brings it back. The kubeconfig is read under `no_log`, and refused unless it
names `app_scale_server`, so a test host's run never stops another cluster's apps. A dry run reads the cluster and
says what it would stop or start, in order.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `app_scale_state` | `""` | `down` or `up`; empty does nothing |
| `app_scale_only` | `[]` | The apps and groups to act on; empty means every app |
| `app_scale_other_names` | `[]` | Names another play acts on, accepted and left to it |
| `app_scale_disabled_names` | `[]` | Apps and groups the cluster leaves out on purpose; still refused, saying so |
| `app_scale_kubeconfig` | `""` | The cluster's kubeconfig |
| `app_scale_server` | `""` | The API server the kubeconfig must name |
| `app_scale_path` | `""` | The apps are what the Flux Kustomizations under this path apply |
| `app_scale_late_label` | `""` | One label, `key=value`, of the apps that stop last and start first |
| `app_scale_annotation` | `""` | The annotation that records a stopped app's replicas |
| `app_scale_timeout` | `10m` | How long a real run waits for an app to stop, or to be available |

This repository's `down.yml` and `up.yml` pass the state and `ONLY`, with `homeassistant` as the name their second
play takes (Home Assistant's VM); the rest is in `group_vars/kubernetes/apps.yml`.

## Example

```yaml
- hosts: localhost
  roles:
    - role: app_scale
      vars:
        app_scale_state: down
        app_scale_only: [jellyfin, nextcloud]
        app_scale_kubeconfig: "{{ lookup('env', 'HOME') }}/.kube/homelab"
        app_scale_server: https://192.0.2.30:6443
        app_scale_path: k8s/apps
        app_scale_late_label: app.kubernetes.io/component=database
        app_scale_annotation: example.org/replicas
```

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `make docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
