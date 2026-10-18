"""Stand-ins for the commands roles/proxmox_tailscale runs on a Proxmox host, for the idempotence tests.

`apt-get update` and `apt-get install`, `nft`, `sysctl`, `systemctl` and `tailscale`, backed by a JSON file named by
$FAKE_TAILSCALE_STATE. Every call is recorded in the state's `calls`, and every change counts in `mutations`, so a
test can check that a dry run changed nothing and that a secret never appeared in an argument list.

apt-get reads `tailscale.sources` in $FAKE_TAILSCALE_SOURCES_DIR: `update` fails as apt does when its Signed-By key
does not exist, and otherwise offers Tailscale's two packages; nftables is offered by Debian whatever the sources say.
`install` installs what is missing and upgrades what the lists offer newer, unless `--no-upgrade`; `-s` only
reports, with the simulation's `Inst` lines, and a real install prints dpkg's `Unpacking` lines. A package apt does
not know fails with exit status 100, as the real one does. Installing `tailscale` enables and starts tailscaled, and
upgrading it restarts tailscaled, as the package's scripts do.

tailscale follows Tailscale 1.104 on Linux: `status --json` with `BackendState`, and `Self` leaving out `Tags`,
`PrimaryRoutes` and `KeyExpiry` when empty, as the real one does; `get --json`, keyed by `tailscale set`'s flag names,
with `advertise-routes` a comma-separated string as captured from a real client (1.102); `up`, which takes its key only
as `--auth-key=file:<path>` here, and refuses, like the real one, a change of settings that does not mention every
non-default one; `set`, with only the flags the real one takes (no `--advertise-tags`); and `serve status --json`.
Routes are approved when the state's `auto_approve` is set, standing for the policy's auto-approver. The node's
addresses are built at run time, never written here.

nft checks a file only for the role's table (`-c`), and loads it when systemctl starts or reloads
tailnet-guard.service, whose table file is $FAKE_TAILSCALE_GUARD_FILE. systemctl knows the units in
$FAKE_TAILSCALE_SYSTEMD_DIR once `daemon-reload` has read them, and refuses a plain `restart` of the guard, which would
restart tailscaled through its drop-in. Anything else exits non-zero, so an unexpected command fails the test.
"""

import json
import os
import sys
from pathlib import Path

GUARD = "tailnet-guard.service"
# What a fresh Tailscale node's preferences read as on Linux, by `tailscale set` flag name. auto-update is on, as
# tailnets created after February 2024 turn it on for new devices.
DEFAULT_PREFS = {
    "accept-dns": True,
    "accept-routes": False,
    "advertise-connector": False,
    "advertise-exit-node": False,
    "advertise-routes": "",
    "auto-update": True,
    "exit-node": "",
    "exit-node-allow-lan-access": False,
    "hostname": "",
    "netfilter-mode": "on",
    "nickname": "",
    "operator": "",
    "relay-server-port": "",
    "relay-server-static-endpoints": "",
    "report-posture": False,
    "shields-up": False,
    "snat-subnet-routes": True,
    "ssh": False,
    "stateful-filtering": False,
    "update-check": True,
    "webclient": False,
}
SET_ONLY = {
    "auto-update",
    "nickname",
    "relay-server-port",
    "relay-server-static-endpoints",
    "update-check",
    "webclient",
}
UP_ONLY = {"advertise-tags", "auth-key", "force-reauth", "json", "login-server", "qr", "reset", "timeout"}
UP_FLAGS = (set(DEFAULT_PREFS) - SET_ONLY) | UP_ONLY
SET_FLAGS = set(DEFAULT_PREFS) | {"accept-risk"}
REPOSITORY = {"tailscale": "1.104.1", "tailscale-archive-keyring": "1.35.181"}
DEBIAN = {"nftables": "1.1.3-1"}


def fresh_state():
    return {
        "packages": {"installed": dict(DEBIAN), "repository": dict(REPOSITORY), "lists": dict(DEBIAN)},
        "units": {},
        "loaded_units": [],
        "sysctl": {"net.ipv4.ip_forward": "0"},
        "table": None,
        "node": None,
        "auth_key": "",
        "auto_approve": True,
        "serve": {},
        "health": [],
        "restarts": 0,
        "mutations": 0,
        "calls": [],
    }


def state_path():
    return Path(os.environ["FAKE_TAILSCALE_STATE"])


def load():
    if not state_path().exists():
        return fresh_state()
    return json.loads(state_path().read_text())


def save(state):
    state_path().write_text(json.dumps(state, indent=1, sort_keys=True))


def fail(state, message, status=1):
    save(state)
    print(message, file=sys.stderr)
    sys.exit(status)


def stanzas(path):
    """The fields of each deb822 stanza in a .sources file, keys lower-cased."""
    out, current = [], {}
    for line in path.read_text().splitlines():
        if not line.strip():
            if current:
                out.append(current)
            current = {}
        elif not line.startswith("#"):
            key, _, value = line.partition(":")
            current[key.strip().lower()] = value.strip()
    if current:
        out.append(current)
    return out


# ------------------------------------------------------------------------------------------- apt-get ---


def apt_get(state, args):
    packages = state["packages"]
    flags, words, i = set(), [], 0
    while i < len(args):
        if args[i] == "-o":  # -o Dpkg::Options::=..., accepted and ignored
            i += 2
            continue
        (flags.add if args[i].startswith("-") else words.append)(args[i])
        i += 1
    if words == ["update"]:
        packages["lists"] = dict(DEBIAN)
        source = Path(os.environ["FAKE_TAILSCALE_SOURCES_DIR"]) / "tailscale.sources"
        for stanza in stanzas(source) if source.exists() else []:
            if not Path(stanza.get("signed-by", "")).is_file():
                fail(
                    state,
                    f"E: The repository '{stanza.get('uris')} {stanza.get('suites')} InRelease' is not signed.",
                    100,
                )
            packages["lists"].update(packages["repository"])
        print("Reading package lists...")
    elif words[:1] == ["install"]:
        simulate = "-s" in flags
        if not simulate and ("-y" not in flags or os.environ.get("DEBIAN_FRONTEND") != "noninteractive"):
            fail(state, "fake apt-get: install would stop to ask: it needs -y and DEBIAN_FRONTEND=noninteractive")
        for name in words[1:]:
            if name not in packages["lists"] and name not in packages["installed"]:
                fail(state, f"E: Unable to locate package {name}", 100)
        changes = {
            name: packages["lists"][name]
            for name in words[1:]
            if name in packages["lists"]
            and packages["installed"].get(name) != packages["lists"][name]
            and (name not in packages["installed"] or "--no-upgrade" not in flags)
        }
        new = sum(1 for name in changes if name not in packages["installed"])
        print(f"{len(changes) - new} upgraded, {new} newly installed, 0 to remove and 0 not upgraded.")
        for name, version in sorted(changes.items()):
            old = packages["installed"].get(name)
            if simulate:
                print(f"Inst {name} {f'[{old}] ' if old else ''}({version} Tailscale:stable [amd64])")
            else:
                print(f"Unpacking {name} ({version}) {f'over ({old}) ' if old else ''}...")
        if not simulate and changes:
            packages["installed"].update(changes)
            state["mutations"] += 1
            if "tailscale" in changes:
                tailscaled = state["units"].setdefault("tailscaled.service", {"enabled": False, "active": False})
                if tailscaled["active"]:
                    state["restarts"] += 1
                tailscaled.update(enabled=True, active=True)
                if state["node"] is None:
                    state["node"] = {"backend": "NeedsLogin", "prefs": dict(DEFAULT_PREFS), "tags": []}
    else:
        fail(state, f"fake apt-get: unsupported {args}")


# ----------------------------------------------------------------------------------------------- nft ---


def guard_file():
    return Path(os.environ["FAKE_TAILSCALE_GUARD_FILE"])


def nft(state, args):
    if args[:2] == ["-c", "-f"] and len(args) == 3:
        text = Path(args[2]).read_text() if Path(args[2]).is_file() else ""
        if "destroy table inet tailnet_guard" not in text or text.count("{") != text.count("}"):
            fail(state, f"Error: {args[2]}: not a whole tailnet_guard table")
    elif args == ["list", "table", "inet", "tailnet_guard"]:
        if state["table"] is None:
            fail(state, "Error: No such file or directory\nlist table inet tailnet_guard")
        print(state["table"])
    else:
        fail(state, f"fake nft: unsupported {args}")


def load_guard(state):
    """What tailnet-guard.service's ExecStart and ExecReload do: nft -f, all or nothing."""
    text = guard_file().read_text() if guard_file().is_file() else ""
    if "destroy table inet tailnet_guard" not in text:
        fail(state, f"Job for {GUARD} failed: nft could not load {guard_file()}")
    state["table"] = text
    state["mutations"] += 1


# -------------------------------------------------------------------------------------------- sysctl ---


def sysctl(state, args):
    if args[:1] == ["-n"] and len(args) == 2:
        if args[1] not in state["sysctl"]:
            fail(state, f"sysctl: cannot stat /proc/sys/{args[1].replace('.', '/')}: No such file or directory")
        print(state["sysctl"][args[1]])
    elif args[:1] == ["-p"] and len(args) == 2:
        for line in Path(args[1]).read_text().splitlines():
            if line.strip() and not line.lstrip().startswith(("#", ";")):
                key, _, value = line.partition("=")
                state["sysctl"][key.strip()] = value.strip()
                print(f"{key.strip()} = {value.strip()}")
        state["mutations"] += 1
    else:
        fail(state, f"fake sysctl: unsupported {args}")


# ------------------------------------------------------------------------------------------ systemctl ---


def unit_files():
    directory = Path(os.environ["FAKE_TAILSCALE_SYSTEMD_DIR"])
    return sorted(str(p.relative_to(directory)) for p in directory.rglob("*") if p.is_file())


def systemctl(state, args):
    units = state["units"]
    if args == ["daemon-reload"]:
        state["loaded_units"] = unit_files()
        state["mutations"] += 1
        return
    verb, name = (args[0], args[-1]) if len(args) >= 2 else (args[0] if args else "", "")
    known = name in units or name in state["loaded_units"] or name == "nftables.service"
    unit = units.setdefault(name, {"enabled": False, "active": False}) if known else None
    if verb == "is-enabled" and len(args) == 2:
        if unit is None:
            fail(state, f"Failed to get unit file state for {name}: No such file or directory")
        print("enabled" if unit["enabled"] else "disabled")
        if not unit["enabled"]:
            sys.exit(1)
    elif verb == "is-active" and len(args) == 2:
        print("active" if unit and unit["active"] else "inactive")
        if not (unit and unit["active"]):
            sys.exit(3)
    elif verb == "enable" and args[1:-1] in ([], ["--now"]):
        if unit is None:
            fail(state, f"Failed to enable unit: Unit file {name} does not exist.")
        unit["enabled"] = True
        if args[1:-1] == ["--now"] and not unit["active"]:
            start(state, name, unit)
        state["mutations"] += 1
    elif verb == "reload-or-restart" and len(args) == 2:
        if unit is None:
            fail(state, f"Failed to reload-or-restart {name}: Unit {name} not found.", 5)
        state.setdefault("loads", []).append(("reload " if unit["active"] else "start ") + name)
        start(state, name, unit)
        state["mutations"] += 1
    elif verb == "restart" and name == GUARD:
        fail(state, f"fake systemctl: a restart of {GUARD} restarts tailscaled through its drop-in; reload it instead")
    else:
        fail(state, f"fake systemctl: unsupported {args}")


def start(state, name, unit):
    if name == GUARD:
        load_guard(state)
    unit["active"] = True


# ------------------------------------------------------------------------------------------ tailscale ---


def flags_of(state, args, allowed, command):
    """--name=value or a bare --name (true), as Go's flag package takes them; an unknown flag fails as it does."""
    out = {}
    for arg in args:
        name, has_value, value = arg[2:].partition("=")
        if not arg.startswith("--") or name not in allowed:
            fail(state, f"flag provided but not defined: -{name}\nUSAGE\n  tailscale {command} [flags]", 2)
        if isinstance(DEFAULT_PREFS.get(name), bool):
            value = (value.lower() == "true") if has_value else True
        out[name] = value
    return out


def address():
    """The node's tailnet address, built here so no address is written in this file."""
    return ".".join(["100", "64", "0", "1"])


def status(state):
    node = state["node"]
    prefs = node["prefs"]
    routes = [r for r in prefs["advertise-routes"].split(",") if r]
    self = {"HostName": "pve", "OS": "linux", "Online": node["backend"] == "Running", "TailscaleIPs": [address()]}
    if node["tags"]:
        self["Tags"] = node["tags"]
    else:
        self["KeyExpiry"] = "2027-04-15T00:00:00Z"
    if routes and state["auto_approve"] and node["backend"] == "Running":
        self["PrimaryRoutes"] = routes
    return {"BackendState": node["backend"], "Self": self, "Health": state["health"], "Version": "1.104.1"}


def tailscale(state, args):
    if "tailscale" not in state["packages"]["installed"]:
        fail(state, "fake: tailscale: command not found", 127)
    if not state["units"].get("tailscaled.service", {}).get("active"):
        fail(state, "failed to connect to local tailscaled; it doesn't appear to be running")
    node = state["node"]
    if args == ["status", "--json"]:
        print(json.dumps(status(state), indent=2))
    elif args == ["get", "--json"]:
        print(json.dumps(node["prefs"], indent=2))
    elif args == ["serve", "status", "--json"]:
        print(json.dumps(state["serve"]))
    elif args[:1] == ["up"]:
        given = flags_of(state, args[1:], UP_FLAGS, "up")
        if node["backend"] == "NeedsLogin":
            key = given.get("auth-key", "")
            if not key.startswith("file:"):
                fail(state, "fake tailscale: the tests pass the auth key only as --auth-key=file:<path>")
            path = Path(key[len("file:") :])
            if not path.is_file() or path.read_text().strip() != state["auth_key"] or not state["auth_key"]:
                fail(state, "backend error: invalid key: unable to validate API key")
            node["tags"] = [t for t in given.get("advertise-tags", "").split(",") if t]
        else:
            # Tailscale's check: every preference up can set that differs from its default must be mentioned.
            unmentioned = [
                name
                for name in sorted(set(DEFAULT_PREFS) - SET_ONLY)
                if node["prefs"][name] != DEFAULT_PREFS[name] and name not in given
            ]
            if unmentioned:
                fail(
                    state,
                    "Error: changing settings via 'tailscale up' requires mentioning all non-default flags. "
                    f"Missing: {', '.join(unmentioned)}",
                )
        node["prefs"].update({k: v for k, v in given.items() if k in DEFAULT_PREFS})
        node["backend"] = "Running"
        state["mutations"] += 1
    elif args[:1] == ["set"]:
        given = flags_of(state, args[1:], SET_FLAGS, "set")
        node["prefs"].update({k: v for k, v in given.items() if k in DEFAULT_PREFS})
        state["mutations"] += 1
    else:
        fail(state, f"fake tailscale: unsupported {args}")


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    state = load()
    state["calls"].append([tool, *args])
    tools = {"apt-get": apt_get, "nft": nft, "sysctl": sysctl, "systemctl": systemctl, "tailscale": tailscale}
    tools[tool](state, args)
    save(state)


if __name__ == "__main__":
    main()
