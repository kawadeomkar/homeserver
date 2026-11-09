# app_scale

Scales a Flux-managed cluster's apps to zero and back from the controller, with `kubectl`: what `just suspend` and
`just resume` run. With only its defaults it does nothing.

An **app** is a Deployment that a Flux Kustomization whose path lies under `app_scale_path` applies, as the labels
Flux puts on it say (`kustomize.toolkit.fluxcd.io/name` and `.../namespace`): the apps are found, never listed. A
**group** is the apps of one namespace. `app_scale_only` names apps or groups (a name that is both, such as
`nextcloud`, means the group); empty means every app. A name that is neither is refused before anything changes, in
a dry run too, with the list of both, unless `app_scale_other_names` holds it, for another play to act on.

- **Down never just scales.** A plain `kubectl scale` is undone at Flux's next reconciliation: kustomize-controller
  cleans up the `kubectl` field manager's changes (seen live). So down patches each app once, a merge patch under
  the field manager `homeserver-ansible`: Flux's own annotation `kustomize.toolkit.fluxcd.io/reconcile: disabled`,
  which kustomize-controller reads on the object in the cluster and then neither applies nor prunes it
  (`ExclusionSelector` in fluxcd/pkg/ssa), the replicas to come back to in `app_scale_annotation`, and zero
  replicas. The one patch means Flux never finds an app at zero without the annotation. It then waits for each
  app's pods to be gone. An app already down (held, recorded and at zero) is left as it is, so a second run changes
  nothing. The replicas recorded are those an earlier down recorded, or the app's own, at least one: an app scaled
  to zero by hand comes back with one, and one started by hand while held is stopped again, keeping its first
  record.
- **Up** patches each app that down stopped, in one patch: the recorded replicas, and both annotations removed. It
  then waits for each to be available (`kubectl rollout status`). An app with no record is left alone, so a second
  run changes nothing either. Flux takes the app back at its next reconciliation, applying whatever changed in Git
  meanwhile.
- Apps carrying `app_scale_late_label` (their databases) stop in a second wave, once the others' pods are gone,
  and start in a first, so in a run that names both an app and its database the app never loses its database under
  it; `--only` naming a database alone stops it under its running app. A wave that times out ends the run before
  the next begins, so within that run a database is never stopped under pods that linger, and an app never starts
  before its database is available. The next run does not wait again for what the last one patched: down counts an
  app already held at zero as done, and up a database whose record it already removed, so after a timeout check
  that the apps' pods are gone (down) or that the databases are available (up) before running it again. Each wave's
  loop label says what it holds, `apps` or `late apps`, which a live run draws as a step of its own.

Claims, Services, Ingresses and the platform are left alone: data stays, and so does Flux, which is what keeps an
app down across reboots, Talos upgrades and `just deploy kubernetes`, and brings it back. A stopped app is out of
Flux's hands until up: Flux skips it entirely, so a change merged meanwhile (an image bump) reaches it only then, and
one deleted from Git meanwhile is not pruned. `kubectl get deploy -A` shows such apps at 0, annotated
`kustomize.toolkit.fluxcd.io/reconcile: disabled`. A group a host leaves out of the sync (`roles/flux_bootstrap`)
keeps its held apps at zero, since Flux's prune skips them too, and they come back held with their group.

A run without the kubeconfig is refused, saying that `roles/talos` writes it once the cluster is bootstrapped. The
kubeconfig holds the cluster's admin key, so it is read under `no_log` and only its server kept, and it is refused
unless it names `app_scale_server`, so a test host's run never stops another cluster's apps (the home cluster's, on a
test host whose `local.yml` leaves `kubernetes_talos_config_dir` unset). A dry run reads the cluster and says what it
would stop or start, in order. The report names each app in its wave, with the replicas it comes back with, or
starts with, in brackets, then the apps already down (`app_scale_report` in the filter plugin).

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `app_scale_state` | `""` | `down` or `up`; empty does nothing |
| `app_scale_only` | `[]` | The apps and groups to act on; empty means every app |
| `app_scale_other_names` | `[]` | Names another play acts on, accepted and left to it |
| `app_scale_kubeconfig` | `""` | The cluster's kubeconfig |
| `app_scale_server` | `""` | The API server the kubeconfig must name |
| `app_scale_path` | `""` | The apps are what the Flux Kustomizations under this path apply |
| `app_scale_late_label` | `""` | One label, `key=value`, of the apps that stop last and start first |
| `app_scale_annotation` | `""` | The annotation that records a stopped app's replicas |
| `app_scale_timeout` | `10m` | How long a real run waits for an app to stop, or to be available |

## just suspend and just resume

- `just suspend`: every app scaled to zero, Flux told to leave it there, data kept, then Home Assistant's VM shut
  down. `--only jellyfin,media,homeassistant` for some (apps, groups, or Home Assistant); `--all` then the cluster's
  VMs too, the worker first; `--check` says what it would do, in order; ansible-playbook's own flags after `--`,
  since before it just reads a word that begins with `-` as one of the recipe's own options.
- `just resume`: brings back what `just suspend` stopped, handing each app back to Flux, and starts Home Assistant's
  VM. `--only` narrows it too, `--all` starts the cluster's VMs first, and `--check` says what it would do.

The justfile's `_scale` hands `--only` to the playbooks as JSON (`-e '{"only": "<names>"}'`), so spaces survive, and
`--all` as `{"cluster_vms": true}`; a bare word where `--only` belongs is refused. Every play of both playbooks
reads `--only` alike, blanks and spaces dropped. `suspend.yml` and `resume.yml` run the role on `homeserver-k8s`
(local), passing the state and `--only`; `group_vars/kubernetes/apps.yml` gives it the rest: `talos_config_dir`'s
kubeconfig, `talos_endpoint` as the server it must name, the path `k8s/apps`, the late label
`app.kubernetes.io/component=database` (both MariaDBs, `nextcloud-db` and `photoprism-db`), the annotation
`homeserver.internal/replicas`, and ten minutes to wait, since Nextcloud's and PhotoPrism's first start after an
upgrade migrates for that long (their startup probes). The platform under `k8s/infrastructure/` is never touched.

`suspend.yml`'s plays, in order:

1. On localhost, a refusal of a cluster whose inventory lacks the `kubernetes` group, as `kubernetes.yml` refuses
   it: the apps' play would match no host and end without a word.
2. The apps' play, on `homeserver-k8s`. It refuses `--all` with `--only` (everything stops, the cluster's VMs last)
   before any play changes anything, refuses the NAS and Kubernetes sections' errors, and ends by itself without a
   cluster. It passes `homeassistant` as `app_scale_other_names` when the setup has Home Assistant, and steps aside
   when `--only` names Home Assistant alone, so a cluster that is down cannot block it.
3. Home Assistant's play, on the Proxmox host, through the proxmox_vm role's `power` entry point, when the setup has
   Home Assistant (`haos_mac`) and `--only` names it or nothing. The shutdown is `qm shutdown`, the guest's own
   (through ACPI or its agent), never a forced stop: one that outlasts `proxmox_vm_power_timeout` (180 seconds) fails
   and leaves the VM running. It refuses the Home Assistant section's errors too. A failure of the apps' play,
   whose one host it is, ends the run before this play. Without a cluster no play checks app names, so this one
   refuses every name but `homeassistant`.
4. Only with `--all`, the cluster's VMs, after everything else, in `kubernetes_power_order`
   (`group_vars/all/kubernetes.yml`: the worker, then the control plane, whose API answers until the end). Each
   shutdown is the guest's own, which Talos takes as an orderly one, with 600 seconds each, since a worker stops its
   pods first.

`resume.yml`'s plays: the same refusal on localhost; with `--all`, the cluster's VMs started first, the control
plane before the worker (refusing `--only` there); the apps' play, which with `--all` first waits for every node to
be Ready (45 tries ten seconds apart, each waiting up to 20 seconds; a dry run asks once and, the cluster still down,
says that a real run starts its VMs, waits for every node and then brings the apps back, and ends there); then Home
Assistant's VM started (`qm start`), under the same conditions as in `suspend.yml`.

The `power` entry point leaves a VM already as asked alone, refuses a VMID that does not exist, and changes nothing
else about any VM. A VM shut down here stays down until `just resume` starts it, or `just deploy homeassistant` and
the host's boot for Home Assistant's, which is marked `start` and `onboot`; the cluster's VMs come back with
`just resume --all`, `just deploy kubernetes` or the host's boot. A live run (`filter_plugins/live_recipes.py`)
draws the apps, Home Assistant and the cluster's VMs as stages, the last only with `--all`, each wave of apps a step.

## Tests

`tests/unit/test_app_scale.py` covers the filter plugin's reading and plan on their own, including forms the fake
never produces (kubectl's JSON as text, an unset replica count, a malformed record). `tests/integration/apps.yml`
runs the role as the playbooks' apps' play does, with this repository's configuration, against `fake_talos_bin/`'s
kubectl (`tests/integration/fake_kubectl.py`), in `tests/integration/test_idempotence.py`'s
`test_app_scale_down_and_up` and `test_app_scale_refusals_and_timeouts` (marked `cluster`): down and up with their
dry runs and second runs, the waves' order, `--only` with an app and a group, an app started by hand while held,
unknown names, no kubeconfig or another cluster's, and pods that never stop or an app that never becomes available.
The fake models a Deployment's pods by its replicas: they go the moment it is scaled to zero unless the state's
`pods_linger` names it, and it is available once scaled up unless `never_available` names it. Every patch and wait
is appended to the state's `events`, so a test sees their order.

`tests/integration/haos_power.yml` runs the `power` entry point as the Home Assistant plays do, against the fake
Proxmox host (`test_haos_power`): down and up, each leaving a VM already as asked alone, a dry run changing nothing,
a VMID that does not exist, and a shutdown that never finishes, which fails with the VM still running.
`tests/policy/test_roles.py` holds the playbooks to those test playbooks (`test_down_and_up_read_only_alike`: the
state, `--only`'s expression, the power state and the VMIDs), and checks that `--all` takes no `--only`
(`test_all_takes_no_only`), that without a cluster Home Assistant's play refuses an app
(`test_down_without_a_cluster_refuses_an_app`), and that the worker goes down first in both setups
(`test_the_cluster_goes_down_worker_first`).

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

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
