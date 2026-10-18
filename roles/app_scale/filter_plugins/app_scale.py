"""What roles/app_scale does, worked out from what kubectl read from the cluster.

The role's tasks read the Flux Kustomizations and the Deployments with kubectl, pass them to app_scale_apps and
app_scale_plan, and run the patches the plan returns. Everything here is pure: it runs on the controller, never
connects anywhere, and is unit-tested in tests/unit/test_app_scale.py.

An app is a Deployment that a Flux Kustomization whose path lies under the role's path applies, as Flux's own labels
on the Deployment say; its group is its namespace. A stopped app carries two annotations: Flux's own
kustomize.toolkit.fluxcd.io/reconcile: disabled, which kustomize-controller reads on the object in the cluster and
then neither applies nor prunes it, and the role's, which records the replicas to start it with again.
"""

import json

RECONCILE = "kustomize.toolkit.fluxcd.io/reconcile"
FLUX_NAME = "kustomize.toolkit.fluxcd.io/name"
FLUX_NAMESPACE = "kustomize.toolkit.fluxcd.io/namespace"


def _items(listed):
    """The items of a kubectl list, given as its JSON text or already parsed."""
    if isinstance(listed, str):
        listed = json.loads(listed)
    return listed.get("items") or []


def _path(text):
    return str(text).strip().removeprefix("./").strip("/")


def _under(path, prefix):
    path, prefix = _path(path), _path(prefix)
    return bool(prefix) and (path == prefix or path.startswith(prefix + "/"))


def apps(deployments, kustomizations, path, late_label, annotation):
    """Every app, as {name, namespace, replicas, recorded, held, late, selector}, sorted by namespace and name.

    `recorded` is the replicas the role's annotation holds (None without it, or with anything but a count), `held`
    whether Flux's reconcile annotation says disabled, `late` whether the Deployment carries `late_label`
    (key=value), and `selector` its matchLabels as kubectl's -l takes them."""
    owners = {
        (k["metadata"].get("namespace", ""), k["metadata"]["name"])
        for k in _items(kustomizations)
        if _under((k.get("spec") or {}).get("path", ""), path)
    }
    late_key, _, late_value = str(late_label).partition("=")
    out = []
    for deployment in _items(deployments):
        meta = deployment["metadata"]
        labels = meta.get("labels") or {}
        if (labels.get(FLUX_NAMESPACE, ""), labels.get(FLUX_NAME, "")) not in owners:
            continue
        annotations = meta.get("annotations") or {}
        spec = deployment.get("spec") or {}
        match = (spec.get("selector") or {}).get("matchLabels") or {}
        recorded = str(annotations.get(annotation, ""))
        out.append(
            {
                "name": meta["name"],
                "namespace": meta["namespace"],
                # Kubernetes defaults an unset count to one.
                "replicas": int(spec.get("replicas", 1)),
                "recorded": int(recorded) if recorded.isdigit() else None,
                "held": annotations.get(RECONCILE) == "disabled",
                "late": bool(late_key) and labels.get(late_key) == late_value,
                "selector": ",".join(f"{k}={v}" for k, v in sorted(match.items())),
            }
        )
    return sorted(out, key=lambda a: (a["namespace"], a["name"]))


def plan(found, state, only, other_names, annotation):
    """What make down (`state` down) or make up (up) does to the apps `found`, narrowed to the names in `only` (an
    app's or a group's; none means every app). Names in `other_names` belong to another play and are accepted.

    Returns {unknown, apps, groups, waves, order, unchanged}: the names that are none of these, every app's and
    group's name, two waves of steps, the steps' apps as text (each wave's joined), and the apps already as asked,
    each as <namespace>/<name>. Down stops the apps first and the late ones (their
    databases) after; up starts the late ones first. A step is {name, namespace, selector, late, replicas, patch},
    `patch` the merge patch that sets both annotations and the replicas at once, so Flux never finds an app at zero
    without its own: down records the replicas to come back to (those an earlier down recorded, or at least one),
    up restores them and removes both."""
    only = [str(n) for n in only or []]
    names = sorted({a["name"] for a in found})
    groups = sorted({a["namespace"] for a in found})
    unknown = sorted(set(only) - set(names) - set(groups) - {str(n) for n in other_names or []})
    chosen = [a for a in found if not only or a["name"] in only or a["namespace"] in only]
    steps, unchanged = [], []
    for app in chosen:
        if state == "down":
            if app["recorded"] is not None and app["held"] and app["replicas"] == 0:
                unchanged.append(app)
                continue
            back = app["recorded"] if app["recorded"] is not None else max(app["replicas"], 1)
            replicas = 0
            annotations = {RECONCILE: "disabled", annotation: str(back)}
        else:
            if app["recorded"] is None:
                unchanged.append(app)
                continue
            replicas = app["recorded"]
            annotations = {RECONCILE: None, annotation: None}
        patch = {"metadata": {"annotations": annotations}, "spec": {"replicas": replicas}}
        steps.append(
            {
                **{k: app[k] for k in ("name", "namespace", "selector", "late")},
                "replicas": replicas,
                "patch": json.dumps(patch, sort_keys=True),
            }
        )
    late_first = state == "up"
    waves = [[s for s in steps if s["late"] == late_first], [s for s in steps if s["late"] != late_first]]
    return {
        "unknown": unknown,
        "apps": names,
        "groups": groups,
        "waves": [wave for wave in waves if wave],
        "order": [", ".join(f"{s['namespace']}/{s['name']}" for s in wave) for wave in waves if wave],
        "unchanged": [f"{a['namespace']}/{a['name']}" for a in unchanged],
    }


class FilterModule:
    def filters(self):
        return {
            "app_scale_apps": apps,
            "app_scale_plan": plan,
        }
