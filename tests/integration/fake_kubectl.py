"""The kubectl verbs roles/flux_bootstrap uses, for fake_talos.py's kubectl (its docstring has the state).

`apply --server-side --field-manager M -f -|<file>`, `diff --server-side --field-manager M -f -`, `get <kind> <name>
-n <namespace> -o json`, `get kustomizations -A`, `create secret generic ... --dry-run=client -o yaml` and `wait
--for=condition=<Available|Ready> <kind>/<name> -n <namespace> --timeout=<d>`. Objects live in the state's
`objects`, keyed "<kind>/<namespace>/<name>". As on a real cluster, every verb but the client-side render needs a
reachable cluster, and apply and diff fail for a namespace that does not exist or a kind no CRD defines. A diff
prints the whole new object, values included, as kubectl diff prints them, so a test proves the role never shows a
Secret's. `apply -f <file>` takes a Flux Operator release, which its Deployment flux-system/flux-operator marks
(operator_objects, as the tests' release server serves it): applied in order, as kubectl applies a file, its
operator's image recorded in `operator_installs`.
"""

import base64
import json
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
}


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
        return
    if verb == "get":
        flags, positional = flags_of(rest, {"-n", "-o"}, {"-A"}, fail)
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
    if verb == "wait":
        flags, positional = flags_of(rest, {"--for", "-n", "--timeout"}, set(), fail)
        kind, _, name = positional[0].partition("/")
        kind = {"deployment": "Deployment", "fluxinstance": "FluxInstance"}[kind]
        if f"{kind}/{flags.get('-n', '')}/{name}" not in objects:
            fail(f"Error from server (NotFound): {positional[0]} not found")
        if state.get("operator_never_available"):
            fail(f"error: timed out waiting for the condition on {positional[0]}")
        print(f"{positional[0]} condition met")
        return
    fail(f"fake kubectl: unsupported {args}")
