"""Stand-ins for talosctl and kubectl, for the idempotence tests of roles/talos and roles/flux_bootstrap.

State is a JSON file named by $FAKE_TALOS_STATE:

- `nodes`: {address: {mode: maintenance | configured | absent, config, config_sha256, version, schematic, install,
  install_error, install_fails, answers_after, volumes_missing}}. `config` keeps the applied text, so a test can
  read it. A node given its configuration in maintenance mode is configured at once, answering with a talosconfig,
  and installs in the background as Talos 1.14 does: `install` holds the phases its install status reports, one per
  question (installing, waiting-for-reboot, then no answer while it reboots, then installed), and bootstrap is
  refused until it is installed. A node with `install_error` stays pending with that error, as one whose disk
  selector matches nothing does (Talos 1.14.2 leaves the pending phase out of the YAML); one with `install_fails`
  fails its install with that error. An absent node with `answers_after` answers in maintenance mode after that many
  questions. A configured node's user volumes (the UserVolumeConfig documents in its configuration) are ready,
  except those named in `volumes_missing`, which wait, as a volume whose disk selector matches nothing does.
  Applying a configuration (no-reboot, or auto, which Talos 1.14 makes no-reboot) always succeeds, as it does in
  Talos 1.14, unless the state's `apply_fails` holds an error to answer with.
- `bootstrapped` (etcd running), `k8s_version`, `snapshots`, `pre_pulls`, `mutations`, and the failure switches
  `bootstrap_precondition_count` (bootstrap answers FailedPrecondition that many times first) and
  `operator_never_available` (for kubectl wait).
- `objects`: {"<kind>/<namespace>/<name>": manifest} for kubectl.

Written from talosctl v1.14.2's flags and output and the Talos documentation, not from a node: `version` prints the
client's block first, then each node's under `NODE:`; a node in maintenance mode answers only --insecure, a
configured one only with a talosconfig. A rendered configuration is deterministic text: its inputs in comments, the
secrets bundle's cluster secret (so a test can prove it never reaches the output), then the patches. A talosconfig
trusts the secrets bundle's OS CA, and a kubeconfig names the node's endpoint and trusts its Kubernetes CA, as
talosctl's do. Its client
certificates are real, made with openssl, valid for the state's `cert_days` (365 otherwise), so the role's expiry
check runs. Every flag the roles pass is listed; anything else fails, as an unexpected command fails the test.
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import fake_kubectl
import yaml


def state_path():
    return Path(os.environ["FAKE_TALOS_STATE"])


def load():
    return json.loads(state_path().read_text())


def save(state):
    state_path().write_text(json.dumps(state, indent=1, sort_keys=True))


def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def mutated(state):
    state["mutations"] = state.get("mutations", 0) + 1


def client_tag():
    return os.environ.get("FAKE_TALOSCTL_CLIENT_TAG", "v1.14.9")


def certificate(days):
    """A real self-signed client certificate and key, as PEM, valid for `days`."""
    with tempfile.TemporaryDirectory() as work:
        key, crt = Path(work) / "key.pem", Path(work) / "crt.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-keyout",
                key,
                "-out",
                crt,
                "-days",
                str(days),
                "-subj",
                "/O=os:admin",
            ],
            check=True,
            capture_output=True,
        )
        return crt.read_bytes(), key.read_bytes()


def b64(data):
    return base64.b64encode(data).decode()


# ------------------------------------------------------------------------------------------- talosctl ---

# Flags that take a value, and flags that do not, per subcommand (after the global ones).
GLOBAL_VALUE = {"--talosconfig", "-e", "--endpoints", "-n", "--nodes"}


def parse(args, value_flags, bool_flags):
    """Flags and positionals; an unknown flag fails, as talosctl's strict parsing would."""
    flags, positional, i = {}, [], 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-") and arg != "-":
            name, eq, value = arg.partition("=")
            if name in bool_flags:
                flags[name] = value if eq else "true"
            elif name in value_flags or name in GLOBAL_VALUE:
                if not eq:
                    i += 1
                    if i >= len(args):
                        fail(f"flag needs an argument: {name}")
                    value = args[i]
                flags.setdefault(name, []).append(value)
            else:
                fail(f"unknown flag: {name}")
        else:
            positional.append(arg)
        i += 1
    return flags, positional


def one(flags, *names):
    for name in names:
        if name in flags:
            return flags[name][-1]
    return None


def target(state, flags):
    """The node a command is for: -n, or the talosconfig's node."""
    address = one(flags, "-n", "--nodes")
    if address is None and one(flags, "--talosconfig"):
        context = talosconfig_context(one(flags, "--talosconfig"))
        address = (context.get("nodes") or [None])[0]
    if address is None:
        fail("failed to determine endpoints")
    return address, state.setdefault("nodes", {}).setdefault(address, {"mode": "absent"})


def talosconfig_context(path):
    if not path or not Path(path).exists():
        fail(f"open {path}: no such file or directory")
    config = yaml.safe_load(Path(path).read_text())
    return config["contexts"][config["context"]]


def reach(state, address, node, flags, insecure):
    """Fail as talosctl does when the node does not answer this way."""
    if node["mode"] == "absent":
        if node.get("answers_after") is not None:
            node["answers_after"] -= 1
            if node["answers_after"] <= 0:
                node.update(mode="maintenance")
                node.pop("answers_after")
            save(state)
        fail(
            f"rpc error: code = Unavailable desc = connection error: dial tcp {address}:50000: "
            "connect: connection refused"
        )
    if node["mode"] == "maintenance" and not insecure:
        fail("rpc error: code = Unavailable desc = connection error: tls: certificate required")
    if node["mode"] == "configured":
        if insecure:
            fail("rpc error: code = Unavailable desc = connection error: tls: no client certificate")
        talosconfig_context(one(flags, "--talosconfig"))


def talosctl(state, args):
    """talosctl [global flags] <command> [flags]: the global flags (--talosconfig, -e, -n) may also follow the
    command, as talosctl accepts them anywhere."""
    words = list(args)
    i = 0
    globals_ = []
    while i < len(words) and words[i].startswith("-"):
        globals_.append(words[i])
        if words[i] in GLOBAL_VALUE:
            globals_.append(words[i + 1])
            i += 1
        i += 1
    rest = words[i:]
    if not rest:
        fail("no command")
    command, rest = rest[0], rest[1:]
    handler = COMMANDS.get(command)
    if handler is None:
        fail(f"unknown command {command!r} for talosctl")
    handler(state, globals_, rest)


def cmd_version(state, globals_, args):
    flags, _ = parse(globals_ + args, set(), {"--insecure", "-i", "--client", "--short"})
    if "--client" in flags:
        print(f"Client:\nTalos {client_tag()}" if "--short" in flags else f"Client:\n\tTag:         {client_tag()}")
        return
    address, node = target(state, flags)
    reach(state, address, node, flags, "--insecure" in flags or "-i" in flags)
    print(f"Client:\n\tTag:         {client_tag()}\n\tOS/Arch:     linux/amd64")
    print(
        f"Server:\n\tNODE:        {address}\n"
        f"\tTag:         {node.get('version', 'v1.14.2')}\n\tOS/Arch:     linux/amd64"
    )


def cmd_get(state, globals_, args):
    flags, positional = parse(globals_ + args, {"-o", "--output"}, {"--insecure", "-i"})
    address, node = target(state, flags)
    insecure = "--insecure" in flags or "-i" in flags
    reach(state, address, node, flags, insecure)
    if one(flags, "-o", "--output") != "yaml":
        fail("the fake answers -o yaml only")
    kind = positional[0] if positional else ""
    if kind == "extensions":
        docs = [
            {
                "node": address,
                "metadata": {"namespace": "runtime", "type": "ExtensionStatuses.runtime.talos.dev", "id": "0"},
                "spec": {"image": "", "metadata": {"name": "qemu-guest-agent", "version": "10.0.0"}},
            },
            {
                "node": address,
                "metadata": {"namespace": "runtime", "type": "ExtensionStatuses.runtime.talos.dev", "id": "1"},
                "spec": {"image": "", "metadata": {"name": "schematic", "version": node.get("schematic", "")}},
            },
        ]
    elif kind == "unattendedinstallstatuses":
        phases = node.get("install") or ["installed"]
        phase = phases.pop(0) if len(phases) > 1 else phases[0]
        save(state)
        if phase == "rebooting":
            fail(
                f"rpc error: code = Unavailable desc = connection error: dial tcp {address}:50000: "
                "connect: connection refused"
            )
        # As Talos 1.14.2 prints it: pending, the phase's zero value, is left out of the YAML.
        spec = {} if phase == "pending" else {"phase": phase}
        if phase in ("pending", "failed"):
            spec["error"] = node.get("install_error") or node.get("install_fails")
        docs = [
            {
                "node": address,
                "metadata": {
                    "namespace": "runtime",
                    "type": "UnattendedInstallStatuses.runtime.talos.dev",
                    "id": "unattended-install",
                    "phase": "running",
                },
                "spec": spec,
            }
        ]
    elif kind == "volumestatus":
        names = re.findall(r"^kind: UserVolumeConfig\nname: (\S+)$", node.get("config", ""), re.MULTILINE)
        docs = [
            {
                "node": address,
                "metadata": {"namespace": "runtime", "type": "VolumeStatuses.block.talos.dev", "id": "EPHEMERAL"},
                "spec": {"phase": "ready"},
            }
        ] + [
            {
                "node": address,
                "metadata": {"namespace": "runtime", "type": "VolumeStatuses.block.talos.dev", "id": f"u-{name}"},
                "spec": {"phase": "waiting" if name in node.get("volumes_missing", []) else "ready"},
            }
            for name in names
        ]
    else:
        fail(f"resource type {kind!r} not known to the fake")
    print(yaml.safe_dump_all(docs, sort_keys=False), end="")


def rendered(path):
    text = Path(path).read_text()
    found = re.search(r"^# install-image: (\S+)$", text, re.MULTILINE)
    if not found:
        fail(f"{path}: not a configuration the fake rendered")
    image = found.group(1).split("@")[0]  # a pinned digest says nothing of the version
    return text, image.rsplit(":", 1)[1], image.rsplit("/", 1)[1].split(":")[0]


def cmd_apply_config(state, globals_, args):
    flags, _ = parse(globals_ + args, {"-f", "--file", "-m", "--mode"}, {"--insecure", "-i", "--dry-run"})
    address, node = target(state, flags)
    insecure = "--insecure" in flags or "-i" in flags
    reach(state, address, node, flags, insecure)
    text, version, schematic = rendered(one(flags, "-f", "--file"))
    sha = hashlib.sha256(text.encode()).hexdigest()
    if insecure:
        if node.get("install_error"):
            install = ["pending"]
        elif node.get("install_fails"):
            install = ["installing", "failed"]
        else:
            install = ["installing", "waiting-for-reboot", "rebooting", "installed"]
        node.update(
            mode="configured", config=text, config_sha256=sha, version=version, schematic=schematic, install=install
        )
        print("Applied configuration without a reboot")
        mutated(state)
        return
    old = node.get("config", "")
    # Talos 1.14 turns auto into no-reboot, and no-reboot applies any configuration at once: nothing is refused for
    # needing a reboot (ApplyConfiguration in v1alpha1_server.go).
    if "--dry-run" in flags:
        print("Dry run summary:\nApplied configuration without a reboot (skipped in dry-run).", file=sys.stderr)
        if sha == node.get("config_sha256"):
            print("No changes.", file=sys.stderr)
        else:
            # As talosctl prints a configuration diff, with its context: lines of the configuration, whatever they
            # hold, secrets included.
            print(
                "Config diff:\n"
                + "\n".join((" " if line in old.splitlines() else "+") + line for line in text.splitlines()),
                file=sys.stderr,
            )
        return
    mode = one(flags, "-m", "--mode") or "auto"
    if mode not in ("no-reboot", "auto"):
        fail(f"the fake does not apply in mode {mode}")
    if state.get("apply_fails"):
        fail(f"error applying new configuration: rpc error: code = Unavailable desc = {state['apply_fails']}")
    node.update(config=text, config_sha256=sha)
    print("Applied configuration without a reboot")
    mutated(state)


def cmd_bootstrap(state, globals_, args):
    flags, _ = parse(globals_ + args, {"--recover-from"}, set())
    address, node = target(state, flags)
    reach(state, address, node, flags, False)
    if (node.get("install") or ["installed"])[0] != "installed":
        fail("rpc error: code = FailedPrecondition desc = the node is not installed yet")
    if state.get("bootstrap_precondition_count", 0) > 0:
        state["bootstrap_precondition_count"] -= 1
        save(state)
        fail("rpc error: code = FailedPrecondition desc = time is not in sync yet")
    if state.get("bootstrapped"):
        fail("rpc error: code = AlreadyExists desc = etcd data directory is not empty")
    state["bootstrapped"] = True
    state.setdefault("k8s_version", "1.37.1")
    state["bootstrap_calls"] = state.get("bootstrap_calls", 0) + 1
    mutated(state)


def cmd_service(state, globals_, args):
    flags, positional = parse(globals_ + args, set(), set())
    address, node = target(state, flags)
    reach(state, address, node, flags, False)
    if positional != ["etcd"]:
        fail(f"the fake knows the etcd service only: {positional}")
    running = state.get("bootstrapped") and node["mode"] == "configured"
    print(
        f"NODE     {address}\nID       etcd\nSTATE    {'Running' if running else 'Preparing'}\n"
        f"HEALTH   {'OK' if running else '?'}"
    )


def cmd_health(state, globals_, args):
    flags, _ = parse(globals_ + args, {"--wait-timeout", "--control-plane-nodes", "--worker-nodes"}, set())
    named = [a for key in ("--control-plane-nodes", "--worker-nodes") for v in flags.get(key, []) for a in v.split(",")]
    if not named:
        fail("health: name the nodes")
    for address in named:
        if state.get("nodes", {}).get(address, {}).get("mode") != "configured":
            fail(f"waiting for {address} to be ready: timed out")
    if not state.get("bootstrapped"):
        fail("waiting for etcd to be healthy: timed out")
    print("all checks passed")


def write_kubeconfig(state, path, node):
    """As talosctl kubeconfig writes it: the server and CA from the node's configuration."""
    crt, key = certificate(state.get("cert_days", 365))
    rendered = dict(line[2:].split(": ", 1) for line in node["config"].splitlines() if line.startswith("# "))
    cluster = {"server": rendered["endpoint"], "certificate-authority-data": rendered["k8s-ca"]}
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "homeserver", "cluster": cluster}],
        "users": [
            {"name": "admin@homeserver", "user": {"client-certificate-data": b64(crt), "client-key-data": b64(key)}}
        ],
        "contexts": [{"name": "admin@homeserver", "context": {"cluster": "homeserver", "user": "admin@homeserver"}}],
        "current-context": "admin@homeserver",
    }
    Path(path).write_text(yaml.safe_dump(config))


def cmd_kubeconfig(state, globals_, args):
    flags, positional = parse(globals_ + args, set(), {"--merge", "-m", "--force", "-f"})
    if flags.get("--merge") != "false" or "--force" not in flags or len(positional) != 1:
        fail("the fake writes a kubeconfig with --merge=false --force <path> only")
    address, node = target(state, flags)
    reach(state, address, node, flags, False)
    if not state.get("bootstrapped"):
        fail("rpc error: code = Unavailable desc = kubeconfig is not available until the cluster is bootstrapped")
    write_kubeconfig(state, positional[0], node)
    state["kubeconfigs"] = state.get("kubeconfigs", 0) + 1


def cmd_config(state, globals_, args):
    flags, positional = parse(globals_ + args, set(), set())
    path = one(flags, "--talosconfig")
    if len(positional) != 2 or positional[0] not in ("endpoint", "node"):
        fail(f"the fake knows config endpoint and config node only: {positional}")
    config = yaml.safe_load(Path(path).read_text())
    config["contexts"][config["context"]]["endpoints" if positional[0] == "endpoint" else "nodes"] = [positional[1]]
    Path(path).write_text(yaml.safe_dump(config))


# The pod network the fake's gen config gives a cluster: an RFC 5737 range, never a real network.
FAKE_POD_SUBNET = "198.51.100.0/24"

GEN_VALUE = {
    "--with-secrets",
    "--talos-version",
    "--kubernetes-version",
    "--install-image",
    "--additional-sans",
    "--config-patch",
    "--config-patch-control-plane",
    "--config-patch-worker",
    "--output-types",
    "-t",
    "-o",
    "--output",
}


def cmd_gen(state, globals_, args):
    if args[:1] != ["config"]:
        fail(f"the fake knows gen config only: {args}")
    flags, positional = parse(args[1:], GEN_VALUE, {"--with-docs", "--with-examples", "--force", "-f"})
    if len(positional) != 2:
        fail("gen config takes <cluster name> <cluster endpoint>")
    secrets_path = one(flags, "--with-secrets")
    secrets = yaml.safe_load(Path(secrets_path).read_text()) if secrets_path else {}
    kinds = (one(flags, "--output-types", "-t") or "").split(",")
    output = one(flags, "-o", "--output")
    if len(kinds) != 1 or not output or "--force" not in flags:
        fail("the fake renders one output type to one file, with --force")
    kind = kinds[0]
    if kind == "talosconfig":
        crt, key = certificate(state.get("cert_days", 365))
        config = {
            "context": positional[0],
            "contexts": {
                positional[0]: {"endpoints": [], "ca": secrets["certs"]["os"]["crt"], "crt": b64(crt), "key": b64(key)}
            },
        }
        Path(output).write_text(yaml.safe_dump(config))
        return
    if kind not in ("controlplane", "worker"):
        fail(f"unknown output type {kind}")
    if flags.get("--with-docs") != "false" or flags.get("--with-examples") != "false":
        fail("the role renders without docs and examples")
    patches = list(flags.get("--config-patch", []))
    patches += flags.get("--config-patch-control-plane" if kind == "controlplane" else "--config-patch-worker", [])
    lines = [
        f"# type: {kind}",
        f"# cluster: {positional[0]}",
        f"# endpoint: {positional[1]}",
        f"# talos-version: {one(flags, '--talos-version')}",
        f"# kubernetes-version: {one(flags, '--kubernetes-version')}",
        f"# install-image: {one(flags, '--install-image')}",
        f"# sans: {','.join(flags.get('--additional-sans', []))}",
        f"# k8s-ca: {secrets['certs']['k8s']['crt']}",
        f"# cluster-secret: {((secrets or {}).get('cluster') or {}).get('secret', '')}",
        # The pod network talosctl writes by default, as its own document; a documentation range here.
        "---",
        "apiVersion: v1alpha1",
        "kind: KubeNetworkConfig",
        "podSubnets: [" + FAKE_POD_SUBNET + "]",
    ]
    # A patch is the YAML itself, or @file as talosctl also takes it.
    for patch in patches:
        text = Path(patch[1:]).read_text() if patch.startswith("@") else patch
        lines += ["---", text.rstrip("\n")]
    Path(output).write_text("\n".join(lines) + "\n")


def cmd_etcd(state, globals_, args):
    if args[:1] != ["snapshot"] or len(args) < 2:
        fail(f"the fake knows etcd snapshot <path> only: {args}")
    flags, positional = parse(globals_ + args[1:], set(), set())
    address, node = target(state, flags)
    reach(state, address, node, flags, False)
    if not state.get("bootstrapped"):
        fail("rpc error: code = Unavailable desc = etcd is not running")
    Path(positional[0]).write_text("fake etcd snapshot\n")
    state.setdefault("snapshots", []).append(positional[0])


def cmd_upgrade(state, globals_, args):
    flags, _ = parse(globals_ + args, {"--image", "-i", "--progress"}, set())
    address, node = target(state, flags)
    reach(state, address, node, flags, False)
    image = one(flags, "--image", "-i")
    tagged = image.split("@")[0]  # a pinned digest says nothing of the version
    node.update(version=tagged.rsplit(":", 1)[1], schematic=tagged.rsplit("/", 1)[1].split(":")[0])
    # Talos records the new installer image in the node's configuration.
    reconfigure(node, "install-image", image)
    state.setdefault("upgrades", []).append(address)
    mutated(state)


def reconfigure(node, field, value):
    """A node's stored configuration with one of the rendered inputs changed, as an upgrade changes it."""
    text = re.sub(rf"^# {field}: .*$", f"# {field}: {value}", node.get("config", ""), flags=re.MULTILINE)
    node.update(config=text, config_sha256=hashlib.sha256(text.encode()).hexdigest())


def cmd_upgrade_k8s(state, globals_, args):
    flags, _ = parse(globals_ + args, {"--to", "--endpoint", "--pre-pull-images"}, {"--dry-run"})
    address, node = target(state, flags)
    reach(state, address, node, flags, False)
    to = one(flags, "--to")
    # As talosctl 1.14.2 does: the images are pulled onto the nodes first, dry run or not, unless told otherwise.
    if one(flags, "--pre-pull-images") not in ("false",):
        state["pre_pulls"] = state.get("pre_pulls", 0) + 1
    if "--dry-run" in flags:
        print(
            f"automatically detected the lowest Kubernetes version {state.get('k8s_version')}\n"
            f"checking for removed Kubernetes component flags\nupgrading to {to} (dry run): no deprecated APIs found"
        )
        return
    state["k8s_version"] = to
    # upgrade-k8s updates every node's configuration to the new version.
    for each in state.get("nodes", {}).values():
        if each.get("mode") == "configured":
            reconfigure(each, "kubernetes-version", to)
    mutated(state)


COMMANDS = {
    "version": cmd_version,
    "get": cmd_get,
    "apply-config": cmd_apply_config,
    "bootstrap": cmd_bootstrap,
    "service": cmd_service,
    "health": cmd_health,
    "kubeconfig": cmd_kubeconfig,
    "config": cmd_config,
    "gen": cmd_gen,
    "etcd": cmd_etcd,
    "upgrade": cmd_upgrade,
    "upgrade-k8s": cmd_upgrade_k8s,
}


# --------------------------------------------------------------------------------------------- kubectl ---


def kube_reach(state, kubeconfig):
    if not kubeconfig or not Path(kubeconfig).exists():
        fail(f"error: stat {kubeconfig}: no such file or directory")
    if not state.get("bootstrapped"):
        fail("The connection to the server 127.0.0.30:6443 was refused - did you specify the right host or port?")


def kubectl(state, args):
    kubeconfig = None
    if args[:1] == ["--kubeconfig"]:
        kubeconfig, args = args[1], args[2:]
    if args == ["version", "-o", "json"]:
        kube_reach(state, kubeconfig)
        print(
            json.dumps(
                {
                    "clientVersion": {"gitVersion": "v1.37.1"},
                    "serverVersion": {"gitVersion": "v" + state.get("k8s_version", "1.37.1")},
                }
            )
        )
        return
    fake_kubectl.run(state, kubeconfig, args, kube_reach, fail)


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    state = load()
    {"talosctl": talosctl, "kubectl": kubectl}[tool](state, args)
    save(state)


if __name__ == "__main__":
    main()
