"""Stand-ins for the Proxmox VE commands roles/proxmox_storage, roles/proxmox_apt and roles/proxmox_backup run,
for the idempotence tests.

`pvesh get /storage`, `pvesh get /nodes/<node>/config`, `pvesh get /nodes/<node>/storage/<id>/status`,
`pvesm add nfs`, `pvesm set`, `pvenode config set`, `hostname -s`, `apt-get update` and `apt-get dist-upgrade`,
backed by a JSON file named by $FAKE_PVE_STATE.
Output follows Proxmox VE 9.2, as captured on a fresh install: `disable` appears only when set, every
`/storage` entry carries a `digest` of the whole configuration, `content` comes back in an order that
differs from one listing to the next (it is a hash's order on a real host, so the fake rotates it, and a
status answer shows the latest listing's order), every status answer carries `shared`, 1 only for an NFS
storage, and sizes, 0 for a storage that is not active, and adding an id that exists, or one Proxmox's id
rule rejects (pve-storage-id: two or more letters, digits, hyphens, underscores and dots, starting with a
letter and ending with a letter or digit, case not mattering), fails as `pvesm add` does. A storage named
in the state's `inactive` list answers `active: 0` while still enabled, as one whose mount or pool failed to
come up does, and one whose `nodes` leaves this node out answers `enabled: 0`, as Proxmox does for a storage
restricted to other cluster nodes. Nothing the role runs changes either. Anything else exits non-zero, so an
unexpected command fails the test.

Backup jobs: `pvesh get|create /cluster/backup` and `pvesh get|set /cluster/backup/<id>`, from Proxmox VE 9's
API schema, not yet captured from a host. A job's prune settings come back as an object and its enabled flag
as a number; the role reads text as well, in case a real host gives that. Ids follow Proxmox's pve-configid
rule, and an option the schema lacks fails as it does there. A job's VMIDs start at 100.

apt-get reads the deb822 .sources files in $FAKE_PVE_SOURCES_DIR, the directory the role is given. `update`
fails with 401 Unauthorized, as the real one does, when an enterprise.proxmox.com repository is still
enabled, and otherwise fills the package lists from the no-subscription repository if one is enabled, or with
nothing. `dist-upgrade` upgrades what the lists offer; `-s` only reports it; without `-y` and
DEBIAN_FRONTEND=noninteractive it fails, as the real one would stop to ask. Its lines follow the real ones: a
simulation's `Inst <package> [<old>] (<new> ...)`, without the old version for a new package, and dpkg's
`Unpacking <package> (<new>) over (<old>) ...`, without `over` for a new one. With the state's `dpkg_fails` set,
dist-upgrade unpacks everything and then fails as dpkg does when a package's script fails, with apt's error and
exit status 100.
"""

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

# Proxmox names the node after the short hostname, which the fake `hostname -s` answers. Fixed rather than
# the controller's own: macOS renames itself when the network hands it a name, and a rename between the
# role's `hostname -s` and a later call would fail the run.
NODE = "pve"
# What a fresh install has and what the no-subscription repository offers, by package. `lists` is what apt
# knows: empty until an update has read a repository that carries these packages.
# The kernel comes as a meta package and a signed package named after the kernel release.
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
    """pvesh <get|create|set> <path> [--option value ...] [--output-format json]: get prints JSON and needs the
    format; create and set print nothing, as for these paths on a real host."""
    if len(args) < 2 or args[0] not in ("get", "create", "set"):
        fail(f"fake pvesh: unsupported {args}")
    verb, path, rest = args[0], args[1], args[2:]
    formatted = rest[-2:] == ["--output-format", "json"]
    if formatted:
        rest = rest[:-2]
    if verb == "get" and not formatted:
        fail(f"fake pvesh: get needs --output-format json: {args}")
    for route, handler in ROUTES:
        found = route.fullmatch(path)
        if found and verb in handler:
            result = handler[verb](state, *found.groups(), options(rest))
            if verb == "get":
                print(json.dumps(result))
            return
    fail(f"fake pvesh: unsupported {verb} {path}")


def no_options(opts):
    if opts:
        fail(f"400 Parameter verification failed.\n{sorted(opts)[0]}: property is not defined in schema")


def get_storage(state, opts):
    no_options(opts)
    return listing(state)


def get_node_config(state, node, opts):
    # As on a real host, /nodes/localhost/config (or any name but the node's own) answers {},
    # and values come back as strings.
    return {k: str(v) for k, v in state["node"].items()} if node == NODE else {}


def check_node(node):
    if node not in (NODE, "localhost"):
        fail(f"500 hostname lookup '{node}' failed")


def get_storage_status(state, node, sid, opts):
    check_node(node)
    no_options(opts)
    entry = next((s for s in state["storage"] if s["storage"] == sid), None)
    if entry is None:
        fail(f"500 storage '{sid}' does not exist")
    restricted = "nodes" in entry and NODE not in entry["nodes"].split(",")
    enabled = not entry.get("disable") and not restricted
    active = enabled and sid not in state.get("inactive", [])
    total = 64 * 2**30 if active else 0
    # content in the latest listing's order: counting status reads too would change the order the next
    # listing shows, which the content-order test depends on.
    return {
        "active": 1 if active else 0,
        "avail": total - total // 8,
        "content": rotated(entry["content"], state.get("reads", 0)),
        "enabled": 1 if enabled else 0,
        "shared": 1 if entry["type"] == "nfs" else 0,
        "total": total,
        "type": entry["type"],
        "used": total // 8,
    }


# A scheduled backup job's settings, as `pvesh create /cluster/backup` takes them (Proxmox VE 9's vzdump job
# schema, the ones this repo uses and a few more), and Proxmox's rule for its id (pve-configid).
BACKUP_OPTIONS = {
    "schedule",
    "storage",
    "vmid",
    "all",
    "exclude",
    "node",
    "mode",
    "compress",
    "prune-backups",
    "notes-template",
    "enabled",
    "comment",
    "mailto",
    "mailnotification",
}


def backup_job(state, opts):
    unknown = set(opts) - BACKUP_OPTIONS - {"delete"}
    if unknown:
        fail(f"400 Parameter verification failed.\n{sorted(unknown)[0]}: property is not defined in schema")
    if "mode" in opts and opts["mode"] not in ("snapshot", "suspend", "stop"):
        fail(
            f"400 Parameter verification failed.\nmode: value '{opts['mode']}' does not have a value in the enumeration"
        )
    if "prune-backups" in opts:
        for item in opts["prune-backups"].split(","):
            key, _, value = item.partition("=")
            if not key.startswith("keep-") or not value.isdigit():
                fail(f"400 Parameter verification failed.\nprune-backups: invalid format - {item}")
    if "vmid" in opts and (
        not re.fullmatch(r"\d+(,\d+)*", opts["vmid"]) or any(int(v) < 100 for v in opts["vmid"].split(","))
    ):
        fail("400 Parameter verification failed.\nvmid: value does not look like a valid VM ID list")


def shown_job(job):
    """A job as `pvesh get /cluster/backup` shows it: prune settings as an object, enabled as a number."""
    shown = {**job, "type": "vzdump", "enabled": int(job.get("enabled", "1"))}
    if "prune-backups" in job:
        shown["prune-backups"] = dict(item.split("=", 1) for item in job["prune-backups"].split(","))
    return shown


def get_backup_jobs(state, opts):
    return [shown_job(job) for job in state.get("backup_jobs", {}).values()]


def create_backup_job(state, opts):
    jid = opts.pop("id", None)
    if jid is None or not re.fullmatch(r"[a-z][a-z0-9_-]+", jid, re.IGNORECASE):
        fail(f"400 Parameter verification failed.\nid: invalid configuration ID '{jid}'")
    backup_job(state, opts)
    jobs = state.setdefault("backup_jobs", {})
    if jid in jobs:
        fail(f"500 Job '{jid}' already exists")
    jobs[jid] = {"id": jid, **opts}
    state["mutations"] += 1


def get_backup_job(state, jid, opts):
    job = state.get("backup_jobs", {}).get(jid)
    if job is None:
        fail(f"500 Job '{jid}' does not exist")
    return shown_job(job)


def set_backup_job(state, jid, opts):
    get_backup_job(state, jid, opts)
    backup_job(state, opts)
    job = state["backup_jobs"][jid]
    for name in opts.pop("delete", "").split(","):
        job.pop(name, None)
    job.update(opts)
    state["mutations"] += 1


ROUTES = [
    (re.compile(r"/storage"), {"get": get_storage}),
    (re.compile(r"/nodes/([^/]+)/config"), {"get": get_node_config}),
    (re.compile(r"/nodes/([^/]+)/storage/([^/]+)/status"), {"get": get_storage_status}),
    (re.compile(r"/cluster/backup"), {"get": get_backup_jobs, "create": create_backup_job}),
    (re.compile(r"/cluster/backup/([^/]+)"), {"get": get_backup_job, "set": set_backup_job}),
]


def pvesm(state, args):
    verb = args[0]
    if verb == "add":
        if args[1] != "nfs":
            fail("fake pvesm: only nfs storage is supported")
        sid, opts = args[2], options(args[3:])
        if len(sid) < 2:
            fail(f"400 Parameter verification failed.\nstorage: storage ID '{sid}' cannot be shorter than 2 characters")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]*[a-z0-9]", sid, re.IGNORECASE):
            fail(f"400 Parameter verification failed.\nstorage: storage ID '{sid}' contains illegal characters")
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


def hosted_at(uris, host, path_prefix=""):
    """Whether one of the space-separated URIs is on `host`, under `path_prefix`."""
    return any(urlsplit(uri).hostname == host and urlsplit(uri).path.startswith(path_prefix) for uri in uris.split())


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
            if enabled and hosted_at(uris, "enterprise.proxmox.com"):
                print(f"E: Failed to fetch {uris}/dists/trixie/InRelease  401  Unauthorized", file=sys.stderr)
                print(f"E: The repository '{uris} trixie InRelease' is not signed.", file=sys.stderr)
                sys.exit(100)
        offered = any(
            enabled and hosted_at(uris, "download.proxmox.com", "/debian/pve") and "pve-no-subscription" in components
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
            old = packages["installed"].get(name)
            if simulate:
                print(f"Inst {name} {f'[{old}] ' if old else ''}({version} Proxmox:trixie [amd64])")
            else:
                print(f"Unpacking {name} ({version}) {f'over ({old}) ' if old else ''}...")
        if not simulate and upgrades:
            packages["installed"].update(upgrades)
            state["mutations"] += 1
            if state.get("dpkg_fails"):
                print("Errors were encountered while processing:\n " + sorted(upgrades)[0])
                print("E: Sub-process /usr/bin/dpkg returned an error code (1)", file=sys.stderr)
                save(state)
                sys.exit(100)
    else:
        fail(f"fake apt-get: unsupported {args}")


def hostname(state, args):
    if args != ["-s"]:
        fail(f"fake hostname: unsupported {args}")
    print(NODE)


def main():
    tool, args = sys.argv[1], sys.argv[2:]
    state = load()
    tools = {
        "pvesh": pvesh,
        "pvesm": pvesm,
        "pvenode": pvenode,
        "hostname": hostname,
        "apt-get": apt_get,
    }
    tools[tool](state, args)
    save(state)


if __name__ == "__main__":
    main()
