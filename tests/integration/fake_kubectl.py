"""The kubectl verbs roles/flux_bootstrap, roles/app_scale and roles/status use, for fake_talos.py's kubectl (its
docstring has the state).

`apply --server-side --field-manager M -f -|<file>`, `diff --server-side --field-manager M -f -`, `get <kind> <name>
-n <namespace> -o json`, `get kustomizations -A`, `get <kustomizations|deployments> -A -o json`, `create secret
generic ... --dry-run=client -o yaml`, `wait --for=condition=<Available|Ready> <kind>/<name> -n <namespace>
--timeout=<d>`, `wait --for=condition=Ready kustomizations --all -A --timeout=<d>`, which times out on the state's
`kustomizations_not_ready`, `patch deployment <name> -n <namespace> --type=merge --field-manager=M -p <json>`, `wait
--for=delete pod -l <selector> -n <namespace> --timeout=<d>`, `wait --for=delete kustomization/<name>... -n
<namespace> --timeout=<d>`, which passes for one already gone, and `rollout status deployment/<name> -n <namespace>
--timeout=<d>`. Objects live in the state's `objects`, keyed "<kind>/<namespace>/<name>". As on a real cluster, every
verb but the client-side render needs a reachable cluster, and apply and diff fail for a namespace that does not exist
or a kind no CRD defines. A diff prints the whole new object, values included, as kubectl diff prints them, so a test
proves the role never shows a Secret's. `apply -f <file>` takes a Flux Operator release, which its Deployment
flux-system/flux-operator marks (operator_objects, as the tests' release server serves it): applied in order, as
kubectl applies a file, its operator's image recorded in `operator_installs`.

Applying the FluxInstance stands in for the Flux Operator and Flux: the sync Kustomization flux-system/flux-system
carries the patches the FluxInstance's spec.kustomize.patches add to it, and each Flux Kustomization the state's
`synced` names exists, ready, unless one of those patches deletes it, which also drops the Deployments it applied
(labelled with its name) but those marked kustomize.toolkit.fluxcd.io/reconcile: disabled, as Flux's prune skips them.

A Deployment has pods while its replicas are above zero: they go the moment it is scaled to zero, unless the state's
`pods_linger` names it, and it is available once scaled up, unless `never_available` names it; either makes the
wait time out. Every patch and wait is appended to the state's `events`, so a test sees their order.

just status's reads, as kubectl v1.36 prints them: `get <kind> -A -o json` for the kinds it lists (a cluster-scoped
kind ignores -A, as kubectl does), `get deployments -A -o json --show-managed-fields`, `get --raw
/api/v1/nodes/<node>/proxy/stats/summary` from the state's `node_stats` ({node: summary}), and `exec -n <namespace>
<pod> -c alertmanager -- amtool alert query -o json --alertmanager.url=...` from the state's `alerts`. A listed
Deployment carries the status the controller would give it: every replica ready, unless `pod_states` says why its
pods are not ({name: {reason, message, restarts, finished}}, as a waiting container) or `never_available` names it.
Its pods are made up from it, one per replica, with its template's labels, on the state's `pods_node`; Pod objects
in the state are listed as they are. Every get is a read, and leaves `mutations` alone.
"""

import base64
import copy
import json
import re
import sys
from pathlib import Path

import yaml

OPERATOR_KEY = "Deployment/flux-system/flux-operator"
BUILT_IN = {
    "Namespace": False,
    "ConfigMap": True,
    "Secret": True,
    "Deployment": True,
    "ServiceAccount": True,
    "CustomResourceDefinition": False,
    "Node": False,
    "Pod": True,
    "Service": True,
    "Ingress": True,
    "PersistentVolume": False,
}
# What get lists by its plural name.
LISTED = {
    "kustomizations": "Kustomization",
    "deployments": "Deployment",
    "nodes": "Node",
    "pods": "Pod",
    "services": "Service",
    "ingresses": "Ingress",
    "persistentvolumes": "PersistentVolume",
    "helmreleases": "HelmRelease",
    "fluxinstances": "FluxInstance",
    "certificates": "Certificate",
}
STATS = re.compile(r"/api/v1/nodes/([a-z0-9.-]+)/proxy/stats/summary")
AMTOOL = ["amtool", "alert", "query", "-o", "json", "--alertmanager.url=http://127.0.0.1:9093"]


def flags_of(args, value_flags, bool_flags, fail):
    flags, positional, i = {}, [], 0
    while i < len(args):
        arg = args[i]
        name, eq, value = arg.partition("=")
        if arg.startswith("-") and arg != "-":
            if name in bool_flags:
                flags[name] = True
            elif name in value_flags:
                if not eq:
                    i += 1
                    value = args[i]
                flags[name] = value
            else:
                fail(f"error: unknown flag: {name}")
        else:
            positional.append(arg)
        i += 1
    return flags, positional


def key(obj):
    return f"{obj['kind']}/{obj['metadata'].get('namespace', '')}/{obj['metadata']['name']}"


def kinds(state):
    """Every kind the cluster knows, and whether it is namespaced."""
    out = dict(BUILT_IN)
    for obj in state.get("objects", {}).values():
        if obj["kind"] == "CustomResourceDefinition":
            out[obj["spec"]["names"]["kind"]] = obj["spec"].get("scope", "Namespaced") == "Namespaced"
    return out


def check(state, obj, fail):
    known = kinds(state)
    if obj["kind"] not in known:
        fail(
            f'error: resource mapping not found for name: "{obj["metadata"]["name"]}": no matches for kind '
            f'"{obj["kind"]}" in version "{obj["apiVersion"]}"',
            2,
        )
    namespace = obj["metadata"].get("namespace")
    if known[obj["kind"]] and f"Namespace//{namespace}" not in state.get("objects", {}):
        fail(f'Error from server (NotFound): namespaces "{namespace}" not found', 2)


def compared(obj):
    """What an apply sets, without what the server adds."""
    meta = {k: v for k, v in obj["metadata"].items() if k in ("name", "namespace", "labels", "annotations")}
    return {**{k: v for k, v in obj.items() if k not in ("metadata", "status")}, "metadata": meta}


def operator_objects(version):
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "flux-system"}},
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "fluxinstances.fluxcd.controlplane.io"},
            "spec": {"names": {"kind": "FluxInstance"}, "scope": "Namespaced"},
        },
        {
            "apiVersion": "apiextensions.k8s.io/v1",
            "kind": "CustomResourceDefinition",
            "metadata": {"name": "kustomizations.kustomize.toolkit.fluxcd.io"},
            "spec": {"names": {"kind": "Kustomization"}, "scope": "Namespaced"},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "flux-operator", "namespace": "flux-system"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {"name": "manager", "image": f"ghcr.io/controlplaneio-fluxcd/flux-operator:{version}"}
                        ]
                    }
                }
            },
        },
    ]


def merge_patch(target, patch):
    """RFC 7386: a null removes a key, an object merges, anything else replaces."""
    if not isinstance(patch, dict):
        return patch
    out = dict(target) if isinstance(target, dict) else {}
    for name, value in patch.items():
        if value is None:
            out.pop(name, None)
        else:
            out[name] = merge_patch(out.get(name), value)
    return out


def selects(deployment, selector):
    """Whether -l <selector> (k=v,...) picks the Deployment's pods, which carry its matchLabels."""
    labels = ((deployment.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    wanted = dict(part.split("=", 1) for part in selector.split(",") if part)
    return bool(wanted) and all(labels.get(k) == v for k, v in wanted.items())


def with_status(state, deployment):
    """A Deployment as the API lists it: with the status its controller gives it."""
    out = copy.deepcopy(deployment)
    name = out["metadata"]["name"]
    replicas = out.get("spec", {}).get("replicas", 1)
    trouble = name in state.get("pod_states", {}) or name in state.get("never_available", [])
    ready = 0 if trouble else replicas
    out.setdefault("status", {}).update(
        {"replicas": replicas, "readyReplicas": ready, "availableReplicas": ready, "updatedReplicas": replicas}
    )
    return out


def made_up_pods(state):
    """One pod per replica of each Deployment, as its ReplicaSet would make them, with its template's labels."""
    pods = []
    for key, deployment in sorted(state.get("objects", {}).items()):
        if not key.startswith("Deployment/"):
            continue
        meta, spec = deployment["metadata"], deployment.get("spec", {})
        replicas = spec.get("replicas", 1)
        if replicas == 0 and meta["name"] not in state.get("pods_linger", []):
            continue
        trouble = state.get("pod_states", {}).get(meta["name"])
        for n in range(max(replicas, 1)):
            container = {"name": meta["name"], "ready": not trouble, "restartCount": 0, "state": {"running": {}}}
            if trouble:
                container["restartCount"] = trouble.get("restarts", 0)
                container["state"] = {"waiting": {"reason": trouble["reason"], "message": trouble.get("message", "")}}
                if trouble.get("finished"):
                    container["lastState"] = {"terminated": {"finishedAt": trouble["finished"]}}
            template = spec.get("template", {})
            pods.append(
                {
                    "apiVersion": "v1",
                    "kind": "Pod",
                    "metadata": {
                        "name": f"{meta['name']}-5d8c7b9f4-{n:05d}",
                        "namespace": meta.get("namespace", ""),
                        "labels": template.get("metadata", {}).get("labels", {}),
                        "creationTimestamp": "2026-10-21T04:10:29Z",
                    },
                    "spec": {"nodeName": state.get("pods_node", ""), **template.get("spec", {})},
                    "status": {
                        "phase": "Running",
                        "conditions": [{"type": "PodScheduled", "status": "True"}],
                        "containerStatuses": [container],
                    },
                }
            )
    return pods


def reconcile_flux(state):
    """The Flux Operator rendering the sync from the FluxInstance, and the sync applying and pruning."""
    objects = state["objects"]
    instance = objects.get("FluxInstance/flux-system/flux")
    if instance is None:
        return
    sync_patches = []
    for patch in ((instance.get("spec") or {}).get("kustomize") or {}).get("patches") or []:
        if (patch.get("target") or {}).get("name") == "flux-system":
            for op in yaml.safe_load(patch["patch"]):
                sync_patches += op["value"]
    sync = {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": "flux-system", "namespace": "flux-system"},
        "spec": {"path": instance["spec"]["sync"]["path"], "prune": True, "interval": "10m0s"},
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }
    if sync_patches:
        sync["spec"]["patches"] = sync_patches
    objects["Kustomization/flux-system/flux-system"] = sync
    for name in state.get("synced", []):
        key = f"Kustomization/flux-system/{name}"
        dropped = any(
            p["target"].get("kind") == "Kustomization"
            and re.fullmatch(p["target"].get("name", ""), name)
            and yaml.safe_load(p["patch"]).get("$patch") == "delete"
            for p in sync_patches
        )
        if not dropped:
            objects[key] = {
                "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
                "kind": "Kustomization",
                "metadata": {"name": name, "namespace": "flux-system"},
                "spec": {"path": f"./k8s/apps/{name.removeprefix('apps-')}", "prune": True},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
        elif objects.pop(key, None) is not None:
            for k, obj in list(objects.items()):
                meta = obj["metadata"]
                if (
                    k.startswith("Deployment/")
                    and (meta.get("labels") or {}).get("kustomize.toolkit.fluxcd.io/name") == name
                    and (meta.get("annotations") or {}).get("kustomize.toolkit.fluxcd.io/reconcile") != "disabled"
                ):
                    del objects[k]


def documents(source, fail):
    if source == "-":
        return [doc for doc in yaml.safe_load_all(sys.stdin.read()) if doc]
    if Path(source).is_file():
        return [doc for doc in yaml.safe_load_all(Path(source).read_text()) if doc]
    fail(f"error: the path {source} does not exist")
    return []


def run(state, kubeconfig, args, reach, fail):
    verb, rest = args[0], args[1:]
    objects = state.setdefault("objects", {})
    if verb == "create":
        flags, positional = flags_of(rest, {"-n", "--namespace", "--from-file", "--dry-run", "-o"}, set(), fail)
        if positional[:2] != ["secret", "generic"] or flags.get("--dry-run") != "client" or flags.get("-o") != "yaml":
            fail("the fake renders a generic secret with --dry-run=client -o yaml only")
        if not kubeconfig or not Path(kubeconfig).exists():
            fail(f"error: stat {kubeconfig}: no such file or directory")
        name, _, path = flags["--from-file"].partition("=")
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {
                "name": positional[2],
                "namespace": flags.get("-n") or flags.get("--namespace"),
                "creationTimestamp": None,
            },
            "data": {name: base64.b64encode(Path(path).read_bytes()).decode()},
        }
        print(yaml.safe_dump(secret), end="")
        return
    reach(state, kubeconfig)
    if verb in ("apply", "diff"):
        flags, _ = flags_of(rest, {"--field-manager", "-f"}, {"--server-side"}, fail)
        if not flags.get("--server-side") or flags.get("--field-manager") != "homeserver-ansible":
            fail("the fake applies and diffs server-side, under the field manager homeserver-ansible, only")
        source = flags["-f"]
        docs = documents(source, fail)
        if verb == "apply" and source != "-" and any(key(doc) == OPERATOR_KEY for doc in docs):
            for doc in docs:
                check(state, doc, fail)
                objects[key(doc)] = doc
            image = objects[OPERATOR_KEY]["spec"]["template"]["spec"]["containers"][0]["image"]
            state.setdefault("operator_installs", []).append(image.rpartition("/")[2])
            state["mutations"] = state.get("mutations", 0) + 1
            print(f"deployment.apps/flux-operator serverside-applied ({image})")
            return
        for doc in docs:
            check(state, doc, fail)
        if verb == "diff":
            different = [doc for doc in docs if compared(objects.get(key(doc), {"metadata": {}})) != compared(doc)]
            for doc in different:
                print(f"diff -u -N live/{key(doc)} merged/{key(doc)}")
                print("\n".join("+" + line for line in yaml.safe_dump(compared(doc)).splitlines()))
            sys.exit(1 if different else 0)
        for doc in docs:
            objects[key(doc)] = compared(doc)
            print(f"{doc['kind'].lower()}/{doc['metadata']['name']} serverside-applied")
        state["mutations"] = state.get("mutations", 0) + 1
        if any(doc["kind"] == "FluxInstance" for doc in docs):
            reconcile_flux(state)
        return
    if verb == "exec":
        flags, positional = flags_of(rest[: rest.index("--")] if "--" in rest else rest, {"-n", "-c"}, set(), fail)
        if rest[rest.index("--") + 1 :] != AMTOOL or flags.get("-c") != "alertmanager":
            fail(f"the fake runs amtool alert query in Alertmanager's container only: {rest}")
        if "alerts" not in state:
            fail(f'Error from server (NotFound): pods "{positional[0]}" not found')
        print(json.dumps(state["alerts"]))
        return
    if verb == "get":
        flags, positional = flags_of(rest, {"-n", "-o", "--raw"}, {"-A", "--show-managed-fields"}, fail)
        if flags.get("--raw"):
            found = STATS.fullmatch(flags["--raw"])
            if not found or found.group(1) not in state.get("node_stats", {}):
                fail("Error from server (NotFound): the server could not find the requested resource")
            print(json.dumps(state["node_stats"][found.group(1)]))
            return
        if len(positional) == 1 and positional[0] in LISTED and flags.get("-A") and flags.get("-o") == "json":
            kind = LISTED[positional[0]]
            if kind not in kinds(state):
                fail(f'error: the server doesn\'t have a resource type "{positional[0]}"')
            items = [obj for k, obj in sorted(objects.items()) if k.startswith(kind + "/")]
            if kind == "Deployment":
                items = [with_status(state, obj) for obj in items]
            if kind == "Pod":
                items += made_up_pods(state)
            print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
            return
        if positional == ["kustomizations"] and flags.get("-A"):
            if "Kustomization" not in kinds(state):
                fail('error: the server doesn\'t have a resource type "kustomizations"')
            print("NAMESPACE     NAME          AGE   READY   STATUS\nflux-system   flux-system   1m    True    Applied")
            return
        kind = {"deployment": "Deployment", "fluxinstance": "FluxInstance", "configmap": "ConfigMap"}[positional[0]]
        obj = objects.get(f"{kind}/{flags.get('-n', '')}/{positional[1]}")
        if obj is None:
            fail(f'Error from server (NotFound): {positional[0]}s "{positional[1]}" not found')
        print(json.dumps(obj))
        return
    if verb == "patch":
        flags, positional = flags_of(rest, {"-n", "--type", "--field-manager", "-p"}, set(), fail)
        if positional[:1] != ["deployment"] or flags.get("--type") != "merge":
            fail("the fake merge-patches a deployment only")
        if flags.get("--field-manager") != "homeserver-ansible":
            fail("the fake patches under the field manager homeserver-ansible only")
        name, namespace = positional[1], flags.get("-n", "")
        obj = objects.get(f"Deployment/{namespace}/{name}")
        if obj is None:
            fail(f'Error from server (NotFound): deployments.apps "{name}" not found')
        objects[f"Deployment/{namespace}/{name}"] = merge_patch(obj, json.loads(flags["-p"]))
        replicas = objects[f"Deployment/{namespace}/{name}"]["spec"].get("replicas", 1)
        state.setdefault("events", []).append(f"patch {namespace}/{name} {replicas}")
        state["mutations"] = state.get("mutations", 0) + 1
        print(f"deployment.apps/{name} patched")
        return
    if verb == "rollout":
        flags, positional = flags_of(rest, {"-n", "--timeout"}, set(), fail)
        if positional[:1] != ["status"]:
            fail("the fake reads a rollout's status only")
        name, namespace = positional[1].removeprefix("deployment/"), flags.get("-n", "")
        if f"Deployment/{namespace}/{name}" not in objects:
            fail(f'Error from server (NotFound): deployments.apps "{name}" not found')
        state.setdefault("events", []).append(f"rollout {namespace}/{name}")
        if name in state.get("never_available", []):
            fail("error: timed out waiting for the condition")
        print(f'deployment "{name}" successfully rolled out')
        return
    if verb == "wait" and "--for=delete" in rest and any(a.startswith("kustomization/") for a in rest):
        flags, positional = flags_of(rest, {"--for", "-n", "--timeout"}, set(), fail)
        namespace = flags.get("-n", "")
        state.setdefault("events", []).append("wait deleted " + " ".join(positional))
        left = [p for p in positional if f"Kustomization/{namespace}/{p.partition('/')[2]}" in objects]
        if left:
            fail(f"error: timed out waiting for the condition on {left[0]}")
        return
    if verb == "wait" and "--for=delete" in rest:
        flags, positional = flags_of(rest, {"--for", "-n", "--timeout", "-l"}, set(), fail)
        if positional != ["pod"]:
            fail("the fake waits for pods to be deleted only")
        namespace = flags.get("-n", "")
        state.setdefault("events", []).append(f"wait {namespace} {flags['-l']}")
        running = [
            obj["metadata"]["name"]
            for k, obj in sorted(objects.items())
            if k.startswith(f"Deployment/{namespace}/")
            and selects(obj, flags["-l"])
            and (obj["spec"].get("replicas", 1) > 0 or obj["metadata"]["name"] in state.get("pods_linger", []))
        ]
        if running:
            fail(f"error: timed out waiting for the condition on pods/{running[0]}-5d8c7b9f4-x2kqz")
        return
    if verb == "wait":
        flags, positional = flags_of(rest, {"--for", "-n", "--timeout"}, {"--all", "-A"}, fail)
        if positional == ["kustomizations"]:
            if not (flags.get("--all") and flags.get("-A")) or flags.get("--for") != "condition=Ready":
                fail("the fake waits for every Kustomization to be Ready only")
            if "Kustomization" not in kinds(state):
                fail('error: the server doesn\'t have a resource type "kustomizations"')
            if state.get("kustomizations_not_ready"):
                fail("error: timed out waiting for the condition on kustomizations/apps")
            print("kustomization.kustomize.toolkit.fluxcd.io/flux-system condition met")
            return
        kind, _, name = positional[0].partition("/")
        kind = {"deployment": "Deployment", "fluxinstance": "FluxInstance"}[kind]
        if f"{kind}/{flags.get('-n', '')}/{name}" not in objects:
            fail(f"Error from server (NotFound): {positional[0]} not found")
        if state.get("operator_never_available"):
            fail(f"error: timed out waiting for the condition on {positional[0]}")
        print(f"{positional[0]} condition met")
        return
    fail(f"fake kubectl: unsupported {args}")
