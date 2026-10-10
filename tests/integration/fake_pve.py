"""Stand-ins for the Proxmox VE commands roles/proxmox_storage and roles/proxmox_apt run, for the idempotence
tests.

`pvesh get /storage`, `pvesh get /nodes/<node>/config`, `pvesh get /nodes/<node>/storage/<id>/status`,
`pvesh get /nodes/<node>/qemu` and `/lxc`, `pvesm add nfs`, `pvesm set`, `pvenode config set`, `hostname -s`,
`apt-get update`, `apt-get dist-upgrade`, `uname -r`, `dpkg-query -W` for the kernel packages and
`systemctl reboot`, backed by a JSON file named by $FAKE_PVE_STATE.
Output follows Proxmox VE 9.2, as captured on a fresh install: `disable` appears only when set, every
`/storage` entry carries a `digest` of the whole configuration, `content` comes back in an order that
differs from one listing to the next (it is a hash's order on a real host, so the fake rotates it, and a
status answer shows the latest listing's order), every status answer carries `shared`, 1 only for an NFS
storage, and sizes, 0 for a storage that is not active, and adding an id that exists fails as `pvesm add`
does. A storage named in the state's `inactive` list answers `active: 0` while still enabled, as one whose
mount or pool failed to come up does, and one whose `nodes` leaves this node out answers `enabled: 0`, as
Proxmox does for a storage restricted to other cluster nodes. Nothing the role runs changes either. Anything
else exits non-zero, so an unexpected command fails the test.

apt-get reads the deb822 .sources files in $FAKE_PVE_SOURCES_DIR, the directory the role is given. `update`
fails with 401 Unauthorized, as the real one does, when an enterprise.proxmox.com repository is still
enabled, and otherwise fills the package lists from the no-subscription repository if one is enabled, or with
nothing. `dist-upgrade` upgrades what the lists offer; `-s` only reports it; without `-y` and
DEBIAN_FRONTEND=noninteractive it fails, as the real one would stop to ask. The kernel the node runs is the
state's `running_kernel`; `systemctl reboot` makes it the newest installed signed kernel package and counts the
reboot, and the guests the state lists under `vms` are what `/nodes/<node>/qemu` answers.
"""

import hashlib
import json
import os
import sys
from pathlib import Path

# Proxmox names the node after the short hostname, which the fake `hostname -s` answers. Fixed rather than
# the controller's own: macOS renames itself when the network hands it a name, and a rename between the
# role's `hostname -s` and a later call would fail the run.
NODE = "pve"
# What a fresh install has and what the no-subscription repository offers, by package. `lists` is what apt
# knows: empty until an update has read a repository that carries these packages.
# The kernel comes as a meta package and a signed package named after the release uname reports.
PACKAGES = {
    "installed": {
        "pve-manager": "9.2.2-1",
        "proxmox-kernel-7.0": "7.0.2-6",
        "proxmox-kernel-7.0.2-6-pve-signed": "7.0.2-6",
    },
    "repository": {
        "pve-manager": "9.2.3-1",
        "proxmox-kernel-7.0": "7.0.14-23",
        "proxmox-kernel-7.0.14-23-pve-signed": "7.0.14-23",
    },
    "lists": {},
}
RUNNING_KERNEL = "7.0.2-6-pve"


def fresh_state():
    return {
        "storage": [
            {"storage": "local", "type": "dir", "path": "/var/lib/vz", "content": "backup,iso,vztmpl,import"},
            {
                "storage": "local-lvm",
                "type": "lvmthin",
                "thinpool": "data",
                "vgname": "pve",
                "content": "images,rootdir",
            },
        ],
        "node": {},
        "mutations": 0,
        "reads": 0,
        "inactive": [],
        "packages": json.loads(json.dumps(PACKAGES)),
        "apt_get_runs": 0,
        "running_kernel": RUNNING_KERNEL,
        "reboots": 0,
        "vms": [],
    }


def state_path():
    return Path(os.environ["FAKE_PVE_STATE"])


def load():
    if not state_path().exists():
        return fresh_state()
    return json.loads(state_path().read_text())


def save(state):
    state_path().write_text(json.dumps(state, indent=1, sort_keys=True))


def fail(message):
    print(message, file=sys.stderr)
    sys.exit(255)


def options(args):
    """--key value pairs, plus --delete <names>."""
    out, i = {}, 0
    while i < len(args):
        if not args[i].startswith("--") or i + 1 >= len(args):
            fail(f"400 unable to parse option {args[i]!r}")
        out[args[i][2:]] = args[i + 1]
        i += 2
    return out


def rotated(content, reads):
    """A comma-separated list, rotated by the number of listings so far."""
    items = content.split(",")
    shift = reads % len(items)
    return ",".join(items[shift:] + items[:shift])


def listing(state):
    """The storage list as `pvesh get /storage` prints it, with `content` in a different order each time."""
    reads = state["reads"] = state.get("reads", 0) + 1
    digest = hashlib.sha1(json.dumps(state["storage"], sort_keys=True).encode()).hexdigest()
    return [{**e, "content": rotated(e["content"], reads), "digest": digest} for e in state["storage"]]


def pvesh(state, args):
    if args[:1] != ["get"] or args[-2:] != ["--output-format", "json"]:
        fail(f"fake pvesh: unsupported {args}")
    path = args[1]
    parts = path.split("/")
    if path == "/storage":
        print(json.dumps(listing(state)))
    elif len(parts) == 4 and parts[1] == "nodes" and parts[3] in ("qemu", "lxc"):
        if parts[2] != NODE:
            fail(f"500 hostname lookup '{parts[2]}' failed")
        print(json.dumps(state.get("vms", []) if parts[3] == "qemu" else []))
    elif len(parts) == 4 and parts[1] == "nodes" and parts[3] == "config":
        # As on a real host, /nodes/localhost/config (or any name but the node's own) answers {},
        # and values come back as strings.
        print(json.dumps({k: str(v) for k, v in state["node"].items()} if parts[2] == NODE else {}))
    elif len(parts) == 6 and parts[1] == "nodes" and parts[3] == "storage" and parts[5] == "status":
        if parts[2] not in (NODE, "localhost"):
            fail(f"500 hostname lookup '{parts[2]}' failed")
        sid = parts[4]
        entry = next((s for s in state["storage"] if s["storage"] == sid), None)
        if entry is None:
            fail(f"500 storage '{sid}' does not exist")
        restricted = "nodes" in entry and NODE not in entry["nodes"].split(",")
        enabled = not entry.get("disable") and not restricted
        active = enabled and sid not in state.get("inactive", [])
        total = 64 * 2**30 if active else 0
        # content in the latest listing's order: counting status reads too would change the order the next
        # listing shows, which the content-order test depends on.
        print(
            json.dumps(
                {
                    "active": 1 if active else 0,
                    "avail": total - total // 8,
                    "content": rotated(entry["content"], state.get("reads", 0)),
                    "enabled": 1 if enabled else 0,
                    "shared": 1 if entry["type"] == "nfs" else 0,
                    "total": total,
                    "type": entry["type"],
                    "used": total // 8,
                }
            )
        )
    else:
        fail(f"fake pvesh: unsupported path {path}")


def pvesm(state, args):
    verb = args[0]
    if verb == "add":
        if args[1] != "nfs":
            fail("fake pvesm: only nfs storage is supported")
        sid, opts = args[2], options(args[3:])
        if any(s["storage"] == sid for s in state["storage"]):
            fail(f"create storage failed: storage ID '{sid}' already defined")
        missing = {"server", "export"} - set(opts)
        if missing:
            fail(f"400 missing option(s) {sorted(missing)}")
        entry = {"storage": sid, "type": "nfs", "path": f"/mnt/pve/{sid}", **opts}
    elif verb == "set":
        sid, opts = args[1], options(args[2:])
        entry = next((s for s in state["storage"] if s["storage"] == sid), None)
        if entry is None:
            fail(f"update storage failed: storage '{sid}' does not exist")
        state["storage"].remove(entry)
        for fixed in ("server", "export", "path", "type"):
            if fixed in opts:
                fail(f"400 option '{fixed}' can't be changed")
        for name in opts.pop("delete", "").split(","):
            entry.pop(name, None)
        entry.update(opts)
    else:
        fail(f"fake pvesm: unsupported verb {verb}")
    if entry.get("disable") == "0":
        entry.pop("disable")  # Proxmox stores and shows the flag only when it is set.
    elif entry.get("disable") == "1":
        entry["disable"] = 1
    state["storage"].append(entry)
    state["mutations"] += 1


def pvenode(state, args):
    if args[:2] != ["config", "set"]:
        fail(f"fake pvenode: unsupported {args}")
    opts = options(args[2:])
    for key, value in opts.items():
        if key != "startall-onboot-delay":
            fail(f"fake pvenode: unsupported option {key}")
        delay = int(value)
        if not 0 <= delay <= 300:
            fail("400 startall-onboot-delay: value must be between 0 and 300")
        state["node"][key] = delay
    state["mutations"] += 1


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


def repositories():
    """(uris, components, enabled) for every stanza in $FAKE_PVE_SOURCES_DIR."""
    directory = os.environ.get("FAKE_PVE_SOURCES_DIR")
    if not directory:
        fail("fake apt-get: FAKE_PVE_SOURCES_DIR is not set")
    for path in sorted(Path(directory).glob("*.sources")):
        for stanza in stanzas(path):
            enabled = stanza.get("enabled", "yes").lower() not in ("no", "false", "off", "0")
            yield stanza.get("uris", ""), stanza.get("components", "").split(), enabled


def apt_get(state, args):
    state["apt_get_runs"] = state.get("apt_get_runs", 0) + 1
    packages = state.setdefault("packages", json.loads(json.dumps(PACKAGES)))
    flags, verbs, i = set(), [], 0
    while i < len(args):
        if args[i] == "-o":  # -o Dpkg::Options::=..., accepted and ignored
            i += 2
            continue
        (flags.add if args[i].startswith("-") else verbs.append)(args[i])
        i += 1
    if verbs == ["update"]:
        found = list(repositories())
        for uris, _, enabled in found:
            if enabled and "enterprise.proxmox.com" in uris:
                print(f"E: Failed to fetch {uris}/dists/trixie/InRelease  401  Unauthorized", file=sys.stderr)
                print(f"E: The repository '{uris} trixie InRelease' is not signed.", file=sys.stderr)
                sys.exit(100)
        offered = any(
            enabled and "download.proxmox.com/debian/pve" in uris and "pve-no-subscription" in components
            for uris, components, enabled in found
        )
        packages["lists"] = dict(packages["repository"]) if offered else {}
        print("Reading package lists...")
    elif verbs == ["dist-upgrade"]:
        simulate = "-s" in flags
        if not simulate and ("-y" not in flags or os.environ.get("DEBIAN_FRONTEND") != "noninteractive"):
            fail("fake apt-get: dist-upgrade would stop to ask: it needs -y and DEBIAN_FRONTEND=noninteractive")
        upgrades = {p: v for p, v in packages["lists"].items() if packages["installed"].get(p) != v}
        new = sum(1 for p in upgrades if p not in packages["installed"])
        print(f"{len(upgrades) - new} upgraded, {new} newly installed, 0 to remove and 0 not upgraded.")
        for name, version in sorted(upgrades.items()):
            old = packages["installed"].get(name, "none")
            print(
                f"Inst {name} [{old}] ({version} Proxmox:trixie [amd64])"
                if simulate
                else f"Unpacking {name} ({version}) over ({old}) ..."
            )
        if not simulate and upgrades:
            packages["installed"].update(upgrades)
            state["mutations"] += 1
    else:
        fail(f"fake apt-get: unsupported {args}")


def hostname(state, args):
    if args != ["-s"]:
        fail(f"fake hostname: unsupported {args}")
    print(NODE)


def installed_kernels(state):
    """The kernel releases whose signed package is installed, newest last."""
    names = [p for p in state["packages"]["installed"] if p.startswith("proxmox-kernel-") and p.endswith("-pve-signed")]
    releases = [n.removeprefix("proxmox-kernel-").removesuffix("-signed") for n in names]
    return sorted(releases, key=lambda r: [int(x) for x in r.removesuffix("-pve").replace("-", ".").split(".")])


def uname(state, args):
    if args != ["-r"]:
        fail(f"fake uname: unsupported {args}")
    print(state["running_kernel"])


def dpkg_query(state, args):
    """Only the form the role uses: installed status and name of the signed kernel packages."""
    if args != ["-W", "-f", "${db:Status-Status} ${Package}\\n", "proxmox-kernel-*-pve-signed"]:
        fail(f"fake dpkg-query: unsupported {args}")
    for release in installed_kernels(state):
        print(f"installed proxmox-kernel-{release}-signed")


def systemctl(state, args):
    if args != ["reboot"]:
        fail(f"fake systemctl: unsupported {args}")
    state["running_kernel"] = installed_kernels(state)[-1]
    state["reboots"] = state.get("reboots", 0) + 1
    state["mutations"] += 1


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    state = load()
    tools = {
        "pvesh": pvesh,
        "pvesm": pvesm,
        "pvenode": pvenode,
        "hostname": hostname,
        "apt-get": apt_get,
        "uname": uname,
        "dpkg-query": dpkg_query,
        "systemctl": systemctl,
    }
    tools[tool](state, args)
    save(state)


if __name__ == "__main__":
    main()
