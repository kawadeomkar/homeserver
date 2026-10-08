"""Stand-ins for the Proxmox VE commands roles/proxmox_storage runs, for the idempotence tests.

`pvesh get /storage`, `pvesh get /nodes/<node>/config`, `pvesh get /nodes/<node>/storage/<id>/status`,
`pvesm add nfs`, `pvesm set` and `pvenode config set`, backed by a JSON file named by $FAKE_PVE_STATE.
Output follows Proxmox VE 9.2, as captured on a fresh install: `disable` appears only when set, every
`/storage` entry carries a `digest` of the whole configuration, `content` comes back in an order that
differs from one call to the next (it is a hash's order on a real host, so the fake rotates it), every
status answer carries `shared`, 1 only for an NFS storage, and adding an id that exists fails as `pvesm add`
does. A storage named in the state's `inactive` list answers `active: 0` while still enabled, as one whose
mount or pool failed to come up does, and one whose `nodes` leaves this node out answers `enabled: 0`, as
Proxmox does for a storage restricted to other cluster nodes. Nothing the role runs changes either. Anything
else exits non-zero, so an unexpected command fails the test.
"""

import hashlib
import json
import os
import socket
import sys
from pathlib import Path

STATE = Path(os.environ["FAKE_PVE_STATE"])
# Proxmox names the node after the short hostname.
NODE = socket.gethostname().split(".")[0]


def load():
    if not STATE.exists():
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
        }
    return json.loads(STATE.read_text())


def save(state):
    STATE.write_text(json.dumps(state, indent=1, sort_keys=True))


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


def listing(state):
    """The storage list as `pvesh get /storage` prints it, with `content` in a different order each time."""
    state["reads"] = state.get("reads", 0) + 1
    digest = hashlib.sha1(json.dumps(state["storage"], sort_keys=True).encode()).hexdigest()
    out = []
    for entry in state["storage"]:
        content = entry["content"].split(",")
        shift = state["reads"] % len(content)
        out.append({**entry, "content": ",".join(content[shift:] + content[:shift]), "digest": digest})
    return out


def pvesh(state, args):
    if args[:1] != ["get"] or args[-2:] != ["--output-format", "json"]:
        fail(f"fake pvesh: unsupported {args}")
    path = args[1]
    parts = path.split("/")
    if path == "/storage":
        print(json.dumps(listing(state)))
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
        print(
            json.dumps(
                {
                    "active": 1 if enabled and sid not in state.get("inactive", []) else 0,
                    "enabled": 1 if enabled else 0,
                    "type": entry["type"],
                    "content": entry["content"],
                    "shared": 1 if entry["type"] == "nfs" else 0,
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


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    state = load()
    {"pvesh": pvesh, "pvesm": pvesm, "pvenode": pvenode}[tool](state, args)
    save(state)


if __name__ == "__main__":
    main()
