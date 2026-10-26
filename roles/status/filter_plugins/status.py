"""What just status reports, worked out from what roles/status read, and the report as text.

The role's tasks read each host through status_read and pass the results here: status_proxmox for the Proxmox host (with
status_backup_failed_runs naming the failed backup runs whose logs are read next), status_cluster for the cluster (with
status_credentials for its credentials' expiry, read under no_log), status_nas for what truenas_info read of the NAS
with its status option; the Proxmox host's Tailscale comes as roles/proxmox_tailscale's status entry point recorded it,
and the report adds what this machine's own Tailscale says of the tailnet's split DNS. The report play asks the apps'
addresses (status_probe_targets, then the module status_probe), passes what every host found and what the probes saw to
status_report, then the report to status_text. Everything here is pure: it runs on the controller, never connects
anywhere, and is unit-tested in tests/unit/test_status_filters.py, with the shapes captured from Proxmox VE 9.2 hosts
and from the cluster on one, the NAS's as TrueNAS 25.10's API reference documents them (and seen since on a real NAS),
and Tailscale's from 1.104.2 on a Proxmox host and 1.102.4 on a Mac.

The report is a mapping that just status --json prints as it is: every source's findings, the attention list
(problems, things to do, and sources that could not be read, each with the command that deals with it), and the exit
code just status returns: 2 when a source could not be read, else 1 when there is a problem, else 0. The commands it
suggests are configuration (status_commands), so the role names none of this repo's recipes itself. Which sources
each section is worked out from is section_sources, so a host reads only what the sections shown need
(status_host_reads), and a source no section shown needs is never missed.
"""

import base64
import html
import ipaddress
import json
import re
import textwrap
from datetime import UTC, datetime

try:
    from cryptography import x509
except ImportError:  # pragma: no cover - ansible-core installs it
    x509 = None

GIB = 2**30
SPARK = "▁▂▃▄▅▆▇█"
DAY = 24 * 3600
# Storage types proxmox_vm budgets (the disks' provisioned sizes never above the storage's size), and those whose size
# the host's own files share, which keep proxmox_vm_host_headroom_gib out of the budget: as in roles/proxmox_vm.
BUDGETED_TYPES = ("lvmthin", "zfspool", "dir", "btrfs")
HOST_SHARED_TYPES = ("zfspool", "dir", "btrfs")
GUEST_CONTENT = ("images", "rootdir")
SHORT_TYPES = {"lvmthin": "thin", "zfspool": "zfs"}
# Filesystems a guest agent lists that say nothing about space: read-only images, and the tiny ones.
IMAGE_FILESYSTEMS = ("erofs", "squashfs", "iso9660")
SMALL_FILESYSTEM = GIB
# A storage this full is a thing to do, and this full a problem.
STORAGE_TODO, STORAGE_PROBLEM = 0.8, 0.9
# The sections just status knows, in the order it shows them.
TYPES = ("apps", "admin", "tailscale", "vms", "storage", "backups", "cluster", "nas")
# A guest's interfaces that are never the address it is reached at: loopback, container bridges, tunnels.
INNER_INTERFACES = ("lo", "docker", "hassio", "veth", "br-", "cni", "flannel", "tailscale", "virbr")


# ---------------------------------------------------------------------------------------------- reading ---


def _out(results, name):
    """(text, why) for one status_read result: the output when the command worked, else None and the reason."""
    result = (results or {}).get(name)
    if result is None:
        return None, "not read"
    if result.get("error"):
        return None, result["error"]
    if result.get("rc") != 0:
        lines = (result.get("stderr") or result.get("stdout") or "").strip().splitlines()
        return None, (lines[-1] if lines else f"exit status {result.get('rc')}")
    return result.get("stdout") or "", ""


def _json(results, name):
    """(data, why) for one result whose output is JSON."""
    text, why = _out(results, name)
    if text is None:
        return None, why
    try:
        return json.loads(text), ""
    except ValueError:
        return None, "not JSON"


def _agent(data):
    """A guest agent's answer, which qm guest cmd prints bare and pvesh wraps in result."""
    if isinstance(data, dict) and "result" in data:
        return data["result"]
    return data


def cpu_buckets(points, buckets=12):
    """The CPU use of the last day in `buckets` equal spans, each its points' average, None where none has data.
    Proxmox's day history has a point a minute; the first ones of a VM started since carry only the time."""
    timed = [p for p in points or [] if isinstance(p, dict) and isinstance(p.get("time"), int | float)]
    if not timed:
        return []
    end = max(p["time"] for p in timed)
    span = DAY / buckets
    sums, counts = [0.0] * buckets, [0] * buckets
    for p in timed:
        if not isinstance(p.get("cpu"), int | float) or p["time"] <= end - DAY:
            continue
        i = min(buckets - 1, int((p["time"] - (end - DAY)) // span))
        sums[i] += p["cpu"]
        counts[i] += 1
    return [round(s / c, 4) if c else None for s, c in zip(sums, counts, strict=True)]


def filesystems(found):
    """A guest agent's filesystems worth showing, each {mount, used, total}: no read-only image, none under 1 GiB."""
    out = []
    for fs in found or []:
        total, used = fs.get("total-bytes"), fs.get("used-bytes")
        if not isinstance(total, int) or not isinstance(used, int) or total < SMALL_FILESYSTEM:
            continue
        if fs.get("type") in IMAGE_FILESYSTEMS:
            continue
        out.append({"mount": fs.get("mountpoint", "?"), "used": used, "total": total})
    return sorted(out, key=lambda f: f["mount"])


def os_name(found):
    """A guest agent's OS name and version, when it says them (Talos's says neither)."""
    if not isinstance(found, dict):
        return ""
    return str(found.get("pretty-name") or "")


def guest_address(interfaces):
    """The IPv4 address a guest agent reports on the guest's own network, past its container bridges."""
    for interface in interfaces or []:
        if not isinstance(interface, dict) or str(interface.get("name") or "").startswith(INNER_INTERFACES):
            continue
        for address in interface.get("ip-addresses") or []:
            ip = str(address.get("ip-address") or "")
            if address.get("ip-address-type") == "ipv4" and not ip.startswith(("127.", "169.254.")):
                return ip
    return ""


def pending_keys(entries):
    """The settings that wait for the VM's next start, from .../pending."""
    return sorted(e["key"] for e in entries or [] if isinstance(e, dict) and ("pending" in e or "delete" in e))


# A version stamp's values, as the begin marker of roles/proxmox_notes's block carries them
# (group_vars/proxmox/notes.yml): the commit, its describe string (git describe's after a 20YY.M.PATCH tag, or the
# commit alone, its 12 digits or more if git needed them, -dirty for uncommitted changes) and its date. Each is
# matched whole, as the justfile's _deploy_version checks them.
STAMP_COMMIT = re.compile(r"[0-9a-f]{40}")
STAMP_DESCRIBE = re.compile(
    r"(20[0-9]{2}\.([1-9]|1[0-2])\.(0|[1-9][0-9]*)(-[0-9]+-g[0-9a-f]{12,40})?|[0-9a-f]{12,40})(-dirty)?"
)
STAMP_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# A fenced code block's fence, as roles/proxmox_notes's filter reads one.
STAMP_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")


def _stamp_markers(lines, marker):
    """(begins, ends, open): the block's begin and end markers by roles/proxmox_notes's own rule (its filter's
    markers()), so that the two never disagree on what a block is: a line of its own from the first column, outside
    any fenced code block; and whether the Notes end inside a fence."""
    begin = re.compile(rf"<!-- {re.escape(marker)}:begin( [^\n]*)? -->")
    end = f"<!-- {marker}:end -->"
    fence, begins, ends = "", [], []
    for i, line in enumerate(lines):
        found_fence = STAMP_FENCE.match(line)
        if fence:
            closing = found_fence and found_fence.group(1)[0] == fence[0] and len(found_fence.group(1)) >= len(fence)
            if closing and not line[found_fence.end() :].strip():
                fence = ""
        elif found_fence:
            fence = found_fence.group(1)
        elif begin.fullmatch(line.rstrip()):
            begins.append(i)
        elif line.rstrip() == end:
            ends.append(i)
    return begins, ends, bool(fence)


def stamp(notes, marker):
    """The version a deploy stamped in a machine's Notes: {commit, describe, date} from the begin marker of
    roles/proxmox_notes's block, or None without one. A value of any other shape is left out, and a stamp without a
    commit is none. Notes the role would refuse to write, a marker without its partner, two blocks or no block in
    Notes that end inside a fenced code block, are {broken: why}, since the next deploy refuses them."""
    if not marker:
        return None
    lines = str(notes or "").split("\n")
    begins, ends, open_fence = _stamp_markers(lines, marker)
    if not begins and not ends:
        return {"broken": "they end inside a code block, a fence never closed"} if open_fence else None
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        return {"broken": f"{len(begins)} begin markers and {len(ends)} end markers"}
    found = re.fullmatch(rf"<!-- {re.escape(marker)}:begin( [^\n]*)? -->", lines[begins[0]].rstrip())
    pairs = dict(p.split("=", 1) for p in (found.group(1) or "").split() if "=" in p)
    commit = pairs.get("commit", "")
    if not STAMP_COMMIT.fullmatch(commit):
        return None
    describe, date = pairs.get("describe", ""), pairs.get("date", "")
    return {
        "commit": commit,
        "describe": describe if STAMP_DESCRIBE.fullmatch(describe) else commit[:12],
        "date": date if STAMP_DATE.fullmatch(date) else "",
    }


def _notes(pending):
    """A VM's Notes, from the list .../pending gives of its current settings."""
    entries = (e for e in pending or [] if isinstance(e, dict) and e.get("key") == "description")
    return next((e.get("value", "") for e in entries), "")


INST = re.compile(r"^Inst (\S+) (?:\[(\S+)\] )?\((\S+)")


def upgrades(text):
    """What apt-get -s dist-upgrade would upgrade or install, each {package, old, new}; old is "" for a new one."""
    out = []
    for line in (text or "").splitlines():
        found = INST.match(line)
        if found:
            out.append({"package": found.group(1), "old": found.group(2) or "", "new": found.group(3)})
    return out


def proxmox(found, vms, budget_storages=(), headroom_gib=0, reboot_file="", backup_jobs=(), notes_marker=""):
    """What the Proxmox host's reads say, as the report needs it.

    `found` is {unread, node, results}: why the host could not be read at all (else ""), the node's name, and
    status_read's results by name (resources, node, node-config, rrd-node, rrd-<vmid>, pending-<vmid>, fsinfo-<vmid>,
    osinfo-<vmid>, network-<vmid>, content-<storage>, updates, reboot, and for the backups backup-jobs,
    backup-tasks, backups-<storage>, schedule-<job> and log-<upid>). `vms` is this repo's VMs, each {vmid, name,
    made_by, start, guest_agent, node, app}: `node` the cluster node it is, `app` {name, url, port} the app it serves
    (its address from its guest agent when `url` is empty); every other VM on the host is only counted. Storages in
    `budget_storages` show their disk budget. `backup_jobs` is the backup jobs this setup has, as roles/proxmox_backup
    takes them ({id, schedule, storage, vmid, enabled}), checked against the host's. With `notes_marker`, each of
    this repo's VMs and the host whose Notes were read carry the version stamp found there (stamp), None without one,
    and why their Notes could not be read when the read failed (stamp_unread).
    """
    found = found or {}
    if found.get("unread"):
        return {"unread": found["unread"]}
    results = found.get("results") or {}
    errors = []
    resources, why = _json(results, "resources")
    if resources is None:
        return {"unread": f"pvesh get /cluster/resources: {why}"}
    node = found.get("node") or ""
    status, why = _json(results, "node")
    if status is None:
        # A read that was not asked for, for a section not shown, is no error.
        if "node" in results:
            errors.append(f"the node's status: {why}")
        status = {}
    host_entry = next((r for r in resources if r.get("type") == "node" and r.get("node") == node), {})
    rrd, _ = _json(results, "rrd-node")
    version = str(status.get("pveversion") or "")
    host = {
        "uptime": int(host_entry.get("uptime") or status.get("uptime") or 0),
        "cpu": float(host_entry.get("cpu") or 0),
        "cores": int(host_entry.get("maxcpu") or 0),
        "memory": {"used": int(host_entry.get("mem") or 0), "total": int(host_entry.get("maxmem") or 0)},
        "root": {
            "used": int((status.get("rootfs") or {}).get("used") or host_entry.get("disk") or 0),
            "total": int((status.get("rootfs") or {}).get("total") or host_entry.get("maxdisk") or 0),
        },
        "cpu_24h": cpu_buckets(rrd),
    }
    # Read only for the sections that show the stamps; a read that failed is said as such, never as no stamp.
    if notes_marker and "node-config" in results:
        config, why = _json(results, "node-config")
        host["stamp"] = stamp((config or {}).get("description", ""), notes_marker)
        if config is None:
            errors.append(f"the node's Notes: {why}")
            host["stamp_unread"] = why

    guests = {int(r["vmid"]): r for r in resources if r.get("type") == "qemu" and "vmid" in r}
    ours = {int(vm["vmid"]) for vm in vms or []}
    out_vms = []
    for vm in vms or []:
        vmid = int(vm["vmid"])
        entry = guests.get(vmid)
        item = {
            "vmid": vmid,
            "name": (entry or {}).get("name") or vm.get("name") or "",
            "status": (entry or {}).get("status", "missing") if entry else "missing",
            "made_by": vm.get("made_by", ""),
            "start": vm.get("start", ""),
        }
        if vm.get("node"):
            item["node"] = vm["node"]
        if vm.get("app"):
            app = vm["app"]
            interfaces, _ = _json(results, f"network-{vmid}")
            address = guest_address(_agent(interfaces)) if entry else ""
            port = int(app.get("port") or 80)
            url = app.get("url") or (f"http://{address}{'' if port == 80 else f':{port}'}" if address else "")
            item["app"] = {"name": app.get("name") or item["name"], "url": url}
        if entry:
            rrd, _ = _json(results, f"rrd-{vmid}")
            pending, why = _json(results, f"pending-{vmid}")
            if pending is None and f"pending-{vmid}" in results:
                errors.append(f"VM {vmid}'s pending changes: {why}")
            item |= {
                "uptime": int(entry.get("uptime") or 0),
                "cpu": float(entry.get("cpu") or 0),
                "cores": int(entry.get("maxcpu") or 0),
                # Ballooning is off on every VM here, so this is the host's view of the guest's memory.
                "memory": {"used": int(entry.get("mem") or 0), "total": int(entry.get("maxmem") or 0), "view": "host"},
                "cpu_24h": cpu_buckets(rrd),
                "pending": pending_keys(pending),
                "filesystems": [],
                "os": "",
                "agent": "",
            }
            if notes_marker and f"pending-{vmid}" in results:
                item["stamp"] = stamp(_notes(pending), notes_marker)
                if pending is None:
                    item["stamp_unread"] = why
            if vm.get("guest_agent"):
                fsinfo, why = _json(results, f"fsinfo-{vmid}")
                osinfo, _ = _json(results, f"osinfo-{vmid}")
                if fsinfo is None and f"fsinfo-{vmid}" in results and item["status"] == "running":
                    item["agent"] = why
                item["filesystems"] = filesystems(_agent(fsinfo))
                item["os"] = os_name(_agent(osinfo))
        out_vms.append(item)

    others = [r for vmid, r in sorted(guests.items()) if vmid not in ours]
    templates = [r for r in others if r.get("template")]
    machines = [r for r in others if not r.get("template")]
    other = {
        "vmids": [int(r["vmid"]) for r in machines],
        "templates": [int(r["vmid"]) for r in templates],
        "running": sum(1 for r in machines if r.get("status") == "running"),
        "cores": sum(int(r.get("maxcpu") or 0) for r in machines),
        "memory": sum(int(r.get("maxmem") or 0) for r in machines),
    }

    storages = []
    for entry in sorted(
        (r for r in resources if r.get("type") == "storage" and r.get("node") == node), key=lambda r: r["storage"]
    ):
        sid, kind = entry["storage"], entry.get("plugintype", "")
        item = {
            "id": sid,
            "type": kind,
            "status": entry.get("status", ""),
            "used": int(entry.get("disk") or 0),
            "total": int(entry.get("maxdisk") or 0),
            "budget": None,
        }
        holds_guests = any(c in str(entry.get("content", "")).split(",") for c in GUEST_CONTENT)
        if sid in budget_storages and kind in BUDGETED_TYPES and holds_guests:
            content, why = _json(results, f"content-{sid}")
            if content is None:
                if f"content-{sid}" in results:
                    errors.append(f"{sid}'s volumes: {why}")
            else:
                headroom = int(headroom_gib or 0) * GIB if kind in HOST_SHARED_TYPES else 0
                item["budget"] = {
                    "provisioned": sum(int(v.get("size") or 0) for v in content if v.get("content") in GUEST_CONTENT),
                    "limit": max(item["total"] - headroom, 0),
                }
        storages.append(item)

    text, why = _out(results, "updates")
    if text is None and "updates" in results:
        errors.append(f"the pending upgrades: {why}")
    reboot = None
    if reboot_file:
        result = results.get("reboot") or {}
        reboot = result.get("rc") == 0 if result.get("rc") in (0, 1) else None
    backed_up = backups(results, out_vms, backup_jobs, errors)
    return {
        "unread": "",
        "node": node,
        "version": version.split("/")[1] if version.count("/") >= 1 else version,
        "kernel": str((status.get("current-kernel") or {}).get("release") or ""),
        "host": host,
        "vms": out_vms,
        "others": other,
        "storages": storages,
        "upgrades": upgrades(text) if text is not None else None,
        "reboot": reboot,
        "reboot_file": reboot_file,
        "backups": backed_up,
        "errors": errors,
    }


# ------------------------------------------------------------------------------------------------- backups ---

# A vzdump run's log: the VMs it was asked to back up, on its first line, and each that failed, with why.
VZDUMP_JOB = re.compile(r"starting new backup job: vzdump((?: \d+)+)")
VZDUMP_STARTED = re.compile(r"Starting Backup of VM \d+")
VZDUMP_FAILED = re.compile(r"ERROR: Backup of VM (\d+) failed - (.+)")
TASK_ERROR = re.compile(r"TASK ERROR: (.+)")
# A backup run's id as status_read reads its log, and how many of the newest failed runs' logs are read.
UPID = re.compile(
    r"UPID:[A-Za-z0-9][A-Za-z0-9.-]*:[0-9A-F]{8,}:[0-9A-F]{8,}:[0-9A-F]{8,}:vzdump:[0-9]*:[A-Za-z0-9@._!-]+:"
)
FAILED_RUNS_READ = 3
KEEP_KINDS = ("last", "hourly", "daily", "weekly", "monthly", "yearly")


def _vmids(text):
    return sorted({int(v) for v in re.split(r"[,\s]+", str(text or "")) if v.isdigit()})


def _keep(prune):
    """A job's retention as {kind: count}, from pvesh's object or its text (keep-daily=7,keep-weekly=4)."""
    if isinstance(prune, str):
        prune = dict(item.split("=", 1) for item in prune.split(",") if "=" in item)
    out = {}
    for key, value in (prune or {}).items():
        kind = str(key).strip().removeprefix("keep-")
        if kind in (*KEEP_KINDS, "all") and str(value).strip().isdigit():
            out[kind] = int(value)
    return out


def _running(task):
    return not task.get("endtime") or str(task.get("status") or "RUNNING") == "RUNNING"


def _failed(task):
    """A run that finished with an error; one with warnings made its backups."""
    status = str(task.get("status") or "")
    return not _running(task) and status != "OK" and not status.startswith("WARNINGS")


def backup_failed_runs(results):
    """The ids of the newest backup runs that failed, at most FAILED_RUNS_READ: their logs say which VMs failed and
    why, and are read next."""
    tasks, _ = _json(results, "backup-tasks")
    runs = sorted((t for t in tasks or [] if isinstance(t, dict)), key=lambda t: -int(t.get("starttime") or 0))
    return [t["upid"] for t in runs if _failed(t) and UPID.fullmatch(str(t.get("upid") or ""))][:FAILED_RUNS_READ]


def _run_failures(task, log, vmids):
    """{vmid: why} for one failed run: the VMs its log says failed, or every VM it was to back up when it failed
    before any (a storage that could not be activated), with the run's own error. `vmids` are those of the jobs."""
    lines = [str(entry.get("t", "")) for entry in log or [] if isinstance(entry, dict)]
    failed = {}
    for line in lines:
        match = VZDUMP_FAILED.search(line)
        if match:
            failed[int(match.group(1))] = match.group(2).strip()
    if failed or any(VZDUMP_STARTED.search(line) for line in lines):
        return failed
    asked = next((VZDUMP_JOB.search(line) for line in lines if VZDUMP_JOB.search(line)), None)
    if asked:
        vmids = _vmids(asked.group(1))
    elif str(task.get("id") or "").isdigit():
        vmids = [int(task["id"])]
    why = next((m.group(1) for m in map(TASK_ERROR.search, reversed(lines)) if m), "") or str(task.get("status"))
    return {vmid: why for vmid in vmids}


def backups(results, vms, jobs, errors):
    """The backups of this repo's VMs: each of `jobs` (this setup's) against the host's, each VM's newest backup on its
    job's storage, and the latest failure a run's log names for it. None when the backups were not asked for. Read
    errors are added to `errors`."""
    if "backup-jobs" not in results:
        return None
    wanted = [j for j in jobs or [] if isinstance(j, dict) and j.get("id")]
    live, why = _json(results, "backup-jobs")
    if live is None:
        errors.append(f"the backup jobs: {why}")
        live = None
    hosted = {j.get("id"): j for j in live or [] if isinstance(j, dict) and j.get("type", "vzdump") == "vzdump"}
    ours = [int(vm["vmid"]) for vm in vms or []]

    def covered(job):
        if job.get("all"):
            return sorted(set(ours) - set(_vmids(job.get("exclude"))))
        return _vmids(job.get("vmid"))

    out_jobs = []
    for job in wanted:
        host = hosted.get(job["id"])
        events, _ = _json(results, f"schedule-{job['id']}")
        times = [int(e["timestamp"]) for e in events or [] if isinstance(e, dict) and "timestamp" in e]
        out_jobs.append(
            {
                "id": job["id"],
                "on_host": None if live is None else host is not None,
                "enabled": bool(int((host or {}).get("enabled", 1) or 0)) if host else False,
                "schedule": str((host or {}).get("schedule") or job.get("schedule") or ""),
                "storage": str((host or {}).get("storage") or ""),
                "wanted_storage": str(job.get("storage") or ""),
                "keep": _keep((host or {}).get("prune-backups")),
                "next_run": int(host["next-run"]) if host and host.get("next-run") else None,
                "period": times[1] - times[0] if len(times) >= 2 and times[1] > times[0] else None,
                "vmids": covered(host) if host else [],
                "wanted_vmids": sorted({int(v) for v in job.get("vmid") or []}),
            }
        )

    archives, read = {}, set()
    for storage in sorted({j["wanted_storage"] for j in out_jobs if j["wanted_storage"]}):
        content, why = _json(results, f"backups-{storage}")
        if content is None:
            if f"backups-{storage}" in results:
                errors.append(f"the backups on {storage}: {why}")
            continue
        read.add(storage)
        for volume in content:
            if isinstance(volume, dict) and volume.get("content") == "backup" and str(volume.get("vmid", "")).isdigit():
                archives.setdefault((storage, int(volume["vmid"])), []).append(volume)

    tasks, why = _json(results, "backup-tasks")
    if tasks is None and "backup-tasks" in results:
        errors.append(f"the backup runs: {why}")
    tasks = [t for t in tasks or [] if isinstance(t, dict)]
    running = [int(t.get("starttime") or 0) for t in tasks if _running(t)]
    asked = sorted({v for j in out_jobs for v in j["vmids"]})
    failures = {}
    for task in sorted(tasks, key=lambda t: int(t.get("starttime") or 0)):
        name = f"log-{task.get('upid')}"
        if not _failed(task) or name not in results:
            continue
        log, why = _json(results, name)
        if log is None:
            errors.append(f"the log of the backup run at {task.get('starttime')}: {why}")
        for vmid, reason in _run_failures(task, log, asked).items():
            failures[vmid] = {"time": int(task.get("starttime") or 0), "reason": reason}

    out_vms = []
    for vm in vms or []:
        vmid = int(vm["vmid"])
        job = next((j for j in out_jobs if j["on_host"] and j["enabled"] and vmid in j["vmids"]), None)
        storage = (job or next((j for j in out_jobs if vmid in j["wanted_vmids"]), {})).get("wanted_storage", "")
        kept = sorted(archives.get((storage, vmid), []), key=lambda v: int(v.get("ctime") or 0))
        out_vms.append(
            {
                "vmid": vmid,
                "name": vm.get("name", ""),
                "status": vm.get("status", ""),
                "job": job["id"] if job else "",
                "wanted": any(vmid in j["wanted_vmids"] for j in out_jobs),
                "read": storage in read,
                "newest": int(kept[-1].get("ctime") or 0) if kept else None,
                "size": int(kept[-1].get("size") or 0) if kept else None,
                "kept": len(kept),
                "failure": failures.get(vmid),
            }
        )
    return {"jobs": out_jobs, "vms": out_vms, "running": min(running) if running else None}


# ---------------------------------------------------------------------------------------------- the cluster ---

RECONCILE = "kustomize.toolkit.fluxcd.io/reconcile"
FLUX_NAME = "kustomize.toolkit.fluxcd.io/name"
FLUX_NAMESPACE = "kustomize.toolkit.fluxcd.io/namespace"
APP_NAME = "app.kubernetes.io/name"
APP_VERSION = "app.kubernetes.io/version"
CONTROL_PLANE_LABEL = "node-role.kubernetes.io/control-plane"
# Why a container waits that means it will not start by itself.
FAILING_REASONS = (
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerError",
    "InvalidImageName",
    "RunContainerError",
)
SECRET_MISSING = re.compile(r'secret "([^"]+)" not found')
# A pod still pending after this many seconds is stuck: a volume that does not mount, an image that never arrives.
STUCK = 600
# A node's conditions that are trouble when true.
PRESSURES = ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")
# Port names that are a URL scheme, for a LoadBalancer outside Traefik (Frigate's camera streams).
STREAM_SCHEMES = ("rtsp", "rtsps", "rtmp")
# Alerts that fire by design (Watchdog, InfoInhibitor) or only inform.
QUIET_SEVERITIES = ("none", "info", "")
VERSION = re.compile(r"v?\d+(?:\.\d+){0,2}")
DAY_SECONDS = 86400


def _items(data):
    return (data.get("items") or []) if isinstance(data, dict) else []


def _when(text):
    """Seconds since the epoch of an RFC 3339 time, None for anything else."""
    if not text:
        return None
    try:
        when = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (when if when.tzinfo else when.replace(tzinfo=UTC)).timestamp()


def _condition(obj, kind="Ready"):
    conditions = ((obj or {}).get("status") or {}).get("conditions") or []
    return next((c for c in conditions if isinstance(c, dict) and c.get("type") == kind), {})


def _first_line(text, limit=200):
    line = next((part.strip() for part in str(text or "").splitlines() if part.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _selects(selector, labels):
    """Whether a label selector (matchLabels) picks an object with these labels; an empty one picks nothing here."""
    return bool(selector) and all((labels or {}).get(k) == v for k, v in selector.items())


def _path(text):
    return str(text or "").strip().removeprefix("./").strip("/")


def _under(path, prefix):
    path, prefix = _path(path), _path(prefix)
    return bool(prefix) and (path == prefix or path.startswith(prefix + "/"))


def _ago(seconds):
    seconds = max(int(seconds or 0), 0)
    if seconds < 3600:
        return f"{seconds // 60} m"
    if seconds < 2 * DAY_SECONDS:
        return f"{seconds // 3600} h"
    return f"{seconds // DAY_SECONDS} d"


def short_version(text):
    """A release as people say it: 10.11.11 from 10.11.11ubu2404-ls42, 11.4.12 from 11.4.12-r0-ls224."""
    found = VERSION.match(str(text or ""))
    return found.group(0) if found else str(text or "")[:12]


def image_version(deployment, name):
    """The version an app runs: its app.kubernetes.io/version label, else its own container's image tag (the one
    named after the app, else the first: Grafana's first is a sidecar)."""
    meta, spec = deployment.get("metadata") or {}, deployment.get("spec") or {}
    labelled = (meta.get("labels") or {}).get(APP_VERSION)
    if labelled:
        return short_version(labelled)
    containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
    main = next((c for c in containers if c.get("name") == name), containers[0] if containers else {})
    repository, _, tag = str(main.get("image") or "").split("@")[0].rpartition(":")
    return short_version(tag) if repository and "/" not in tag else ""


def set_at(meta, annotation):
    """When an annotation was last set, from the managed fields kubectl shows with --show-managed-fields."""
    for entry in (meta or {}).get("managedFields") or []:
        fields = ((entry.get("fieldsV1") or {}).get("f:metadata") or {}).get("f:annotations") or {}
        if f"f:{annotation}" in fields:
            return entry.get("time") or ""
    return ""


def pods_health(pods, now):
    """What an app's pods say: their restarts, when the last one ended, why any will not start by itself, and the
    Secrets any waits for."""
    restarts, last, failing, secrets = 0, None, [], []
    for pod in pods:
        status = pod.get("status") or {}
        for c in (status.get("initContainerStatuses") or []) + (status.get("containerStatuses") or []):
            restarts += int(c.get("restartCount") or 0)
            ended = _when(((c.get("lastState") or {}).get("terminated") or {}).get("finishedAt"))
            if ended and (last is None or ended > last):
                last = ended
            waiting = (c.get("state") or {}).get("waiting") or {}
            reason, missing = waiting.get("reason", ""), SECRET_MISSING.search(waiting.get("message", ""))
            if reason == "CreateContainerConfigError" and missing:
                secrets.append(missing.group(1))
            elif reason in FAILING_REASONS:
                failing.append(reason)
        scheduled = _condition(pod, "PodScheduled")
        created = _when((pod.get("metadata") or {}).get("creationTimestamp"))
        if scheduled.get("status") == "False" and scheduled.get("reason") == "Unschedulable":
            failing.append("Unschedulable: " + _first_line(scheduled.get("message"), 120))
        elif status.get("phase") == "Pending" and created and now - created > STUCK and not failing:
            failing.append(f"Pending for {_ago(now - created)}")
    return {
        "restarts": restarts,
        "last_restart": last,
        "failing": list(dict.fromkeys(failing)),
        "secrets": sorted(set(secrets)),
    }


def app_state(deployment, pods, annotation, now):
    """An app's state: running, starting, waiting (for a Secret), failing, suspended (by just suspend, which records
    its replicas in `annotation`) or stopped (scaled to zero otherwise), with what explains it."""
    meta, spec, status = (deployment.get(k) or {} for k in ("metadata", "spec", "status"))
    wanted, ready = int(spec.get("replicas", 1)), int(status.get("readyReplicas") or 0)
    recorded = str((meta.get("annotations") or {}).get(annotation, ""))
    health = pods_health(pods, now)
    out = {
        "wanted": wanted,
        "ready": ready,
        "restarts": health["restarts"],
        "last_restart": datetime.fromtimestamp(health["last_restart"], UTC).isoformat()
        if health["last_restart"]
        else "",
        "detail": "",
        "since": "",
    }
    if wanted == 0:
        if recorded.isdigit():
            return out | {"state": "suspended", "since": set_at(meta, annotation), "recorded": int(recorded)}
        return out | {"state": "stopped", "detail": "scaled to zero"}
    namespace = meta.get("namespace", "")
    if health["secrets"]:
        names = ", ".join(f"{namespace}/{s}" for s in health["secrets"])
        return out | {"state": "waiting", "detail": f"waits for the Secret {names}"}
    if health["failing"]:
        detail = ", ".join(health["failing"])
        if health["restarts"]:
            detail += f", {health['restarts']} restart{'s' if health['restarts'] != 1 else ''}"
            if health["last_restart"]:
                detail += f", the last {_ago(now - health['last_restart'])} ago"
        return out | {"state": "failing", "detail": detail}
    progressing = _condition(deployment, "Progressing")
    if progressing.get("status") == "False" and progressing.get("reason") == "ProgressDeadlineExceeded":
        return out | {"state": "failing", "detail": f"{ready} of {wanted} ready past its progress deadline"}
    if ready >= wanted:
        return out | {"state": "running"}
    return out | {"state": "starting", "detail": f"{ready} of {wanted} ready"}


def _template_labels(deployment):
    return (((deployment.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {}


def apps(deployments, pods, services, ingresses, kustomizations, apps_path, annotation, database_label, now):
    """Every app: each Ingress's backend Service, then the Deployment whose pods it selects (so the platform's
    Grafana too), and every other Deployment a Flux Kustomization under `apps_path` applies. A Deployment labelled
    `database_label` is listed under its app, the one beside it from the same Kustomization. A Kustomization under
    `apps_path` that applied nothing and is not ready is an app of its own, not applied."""
    owners = {
        (k["metadata"].get("namespace", ""), k["metadata"]["name"]): k
        for k in kustomizations
        if _under((k.get("spec") or {}).get("path"), apps_path)
    }
    late_key, _, late_value = str(database_label or "").partition("=")
    by_namespace, found = {}, {}
    for d in deployments:
        by_namespace.setdefault(d["metadata"].get("namespace", ""), []).append(d)
    service_index = {(s["metadata"].get("namespace", ""), s["metadata"]["name"]): s for s in services}

    def pods_of(d):
        selector = ((d.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        namespace = d["metadata"].get("namespace", "")
        return [
            p
            for p in pods
            if p["metadata"].get("namespace", "") == namespace
            and _selects(selector, p["metadata"].get("labels"))
            and not p["metadata"].get("deletionTimestamp")
        ]

    def is_database(d):
        return bool(late_key) and (d["metadata"].get("labels") or {}).get(late_key) == late_value

    def entry(d):
        meta = d["metadata"]
        key = (meta.get("namespace", ""), meta["name"])
        if key not in found:
            labels = meta.get("labels") or {}
            name = labels.get(APP_NAME) or meta["name"]
            owner = (labels.get(FLUX_NAMESPACE, ""), labels.get(FLUX_NAME, ""))
            found[key] = {
                "name": name,
                "group": key[0],
                "deployment": meta["name"],
                "kustomization": owner[1] if owner in owners else "",
                "version": image_version(d, name),
                **app_state(d, pods_of(d), annotation, now),
                "urls": [],
                "streams": [],
                "databases": [],
            }
        return found[key]

    for ingress in ingresses:
        namespace = ingress["metadata"].get("namespace", "")
        balancer = ((ingress.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        address = next((i.get("ip") for i in balancer if i.get("ip")), "")
        for rule in (ingress.get("spec") or {}).get("rules") or []:
            host = rule.get("host")
            if not host:
                continue
            for path in (rule.get("http") or {}).get("paths") or []:
                backend = ((path.get("backend") or {}).get("service") or {}).get("name", "")
                service = service_index.get((namespace, backend)) or {}
                selector = (service.get("spec") or {}).get("selector") or {}
                url = f"https://{host}{str(path.get('path') or '/').rstrip('/')}"
                for d in by_namespace.get(namespace, []):
                    if _selects(selector, _template_labels(d)) and not is_database(d):
                        app = entry(d)
                        if all(u["url"] != url for u in app["urls"]):
                            app["urls"].append({"url": url, "host": host, "address": address})
    for d in deployments:
        labels = d["metadata"].get("labels") or {}
        if (labels.get(FLUX_NAMESPACE, ""), labels.get(FLUX_NAME, "")) in owners and not is_database(d):
            entry(d)
    for d in deployments:
        if not is_database(d):
            continue
        meta = d["metadata"]
        labels = meta.get("labels") or {}
        owner = next(
            (
                a
                for a in found.values()
                if a["group"] == meta.get("namespace", "") and a["kustomization"] == labels.get(FLUX_NAME, "")
            ),
            None,
        )
        name = labels.get(APP_NAME) or meta["name"]
        database = {"name": name, "deployment": meta["name"], "version": image_version(d, name)}
        database |= app_state(d, pods_of(d), annotation, now)
        if owner is None:
            entry(d)
        else:
            owner["databases"].append(database)
    for service in services:
        spec = service.get("spec") or {}
        if spec.get("type") != "LoadBalancer":
            continue
        namespace = service["metadata"].get("namespace", "")
        balancer = ((service.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        address = next((i.get("ip") for i in balancer if i.get("ip")), "")
        for d in by_namespace.get(namespace, []):
            key = (namespace, d["metadata"]["name"])
            if key in found and address and _selects(spec.get("selector"), _template_labels(d)):
                for port in spec.get("ports") or []:
                    scheme = port.get("name") if port.get("name") in STREAM_SCHEMES else ""
                    stream = f"{scheme}://{address}:{port['port']}" if scheme else f"{address}:{port['port']}"
                    # A port open over TCP and UDP (Frigate's WebRTC) is one address.
                    if stream not in found[key]["streams"]:
                        found[key]["streams"].append(stream)
    out = sorted(found.values(), key=lambda a: (a["group"], a["name"]))
    applied = {a["kustomization"] for a in out}
    for (_, name), k in sorted(owners.items()):
        ready = _condition(k)
        if name not in applied and ready.get("status") != "True":
            out.append(
                {
                    "name": name,
                    "group": _path((k.get("spec") or {}).get("path")).rpartition("/")[2],
                    "deployment": "",
                    "kustomization": name,
                    "version": "",
                    "state": "not applied",
                    "detail": _first_line(ready.get("message")) or "its Flux Kustomization is not ready",
                    "wanted": 0,
                    "ready": 0,
                    "restarts": 0,
                    "last_restart": "",
                    "since": "",
                    "urls": [],
                    "streams": [],
                    "databases": [],
                }
            )
    return out


def _flux_objects(items, kind):
    """How many of a Flux kind are ready, and those that are not, each with Flux's message."""
    failing, suspended = [], []
    for item in items:
        meta = item.get("metadata") or {}
        name = f"{meta.get('namespace', '')}/{meta.get('name', '')}"
        if (item.get("spec") or {}).get("suspend"):
            suspended.append(name)
            continue
        ready = _condition(item)
        if ready.get("status") != "True":
            failing.append({"name": name, "message": _first_line(ready.get("message")) or "not ready yet"})
    return {
        "kind": kind,
        "total": len(items),
        "ready": len(items) - len(failing) - len(suspended),
        "failing": failing,
        "suspended": suspended,
    }


def _flux(instances, kustomizations, remote):
    """The FluxInstance's version and state, the commit its root Kustomization applied, and the commit the branch it
    syncs points at now."""
    instance = next(iter(instances), None)
    if instance is None:
        return None
    spec, status = instance.get("spec") or {}, instance.get("status") or {}
    sync = spec.get("sync") or {}
    version = str(status.get("lastAppliedRevision") or "").partition("@")[0]
    root = next((k for k in kustomizations if _path((k.get("spec") or {}).get("path")) == _path(sync.get("path"))), {})
    revision = str((root.get("status") or {}).get("lastAppliedRevision") or "")
    ref, _, digest = revision.partition("@")
    ready = _condition(instance)
    return {
        "version": version or str((spec.get("distribution") or {}).get("version") or ""),
        "ready": ready.get("status") == "True",
        "message": _first_line(ready.get("message")),
        "ref": str(sync.get("ref") or ref),
        "path": _path(sync.get("path")),
        "applied": digest.rpartition(":")[2],
        "remote": remote,
    }


def _alerts(found):
    out = []
    for alert in found or []:
        if not isinstance(alert, dict) or (alert.get("status") or {}).get("state", "active") != "active":
            continue
        labels = alert.get("labels") or {}
        if str(labels.get("severity", "")).lower() in QUIET_SEVERITIES:
            continue
        what = next(
            (
                labels[k]
                for k in ("pod", "deployment", "statefulset", "daemonset", "job", "persistentvolumeclaim")
                if labels.get(k)
            ),
            labels.get("node") or labels.get("instance") or labels.get("service") or "",
        )
        subject = "/".join(part for part in (labels.get("namespace", ""), what) if part)
        out.append(
            {
                "name": labels.get("alertname", "?"),
                "severity": labels.get("severity", ""),
                "subject": subject,
                "since": alert.get("startsAt", ""),
            }
        )
    return sorted(out, key=lambda a: (a["severity"] != "critical", a["name"], a["subject"]))


def _etcd(text):
    """The state and health talosctl service etcd prints."""
    fields = {}
    for line in (text or "").splitlines():
        key, _, value = line.strip().partition(" ")
        if key in ("STATE", "HEALTH") and key.lower() not in fields:
            fields[key.lower()] = value.strip()
    return {"state": fields.get("state", ""), "health": fields.get("health", "")} if fields else None


def _volume_paths(pvs):
    """Where each claim's volume is on its node, for the local volumes (a local or hostPath one)."""
    out = {}
    for pv in pvs:
        spec = pv.get("spec") or {}
        claim = spec.get("claimRef") or {}
        path = (spec.get("local") or {}).get("path") or (spec.get("hostPath") or {}).get("path")
        if claim.get("name") and path:
            out[(claim.get("namespace", ""), claim["name"])] = path
    return out


def node_usage(summary, paths, volumes):
    """A node's memory, and its filesystems, from kubelet's statistics: /var (where kubelet keeps pods and images)
    and each of `volumes` ({path: label}, a Talos user volume's mount) that a claim in use lies on. Every claim on a
    volume reports that volume's whole filesystem, so one claim is enough."""
    node = (summary or {}).get("node") or {}
    memory = node.get("memory") or {}
    used, available = memory.get("workingSetBytes"), memory.get("availableBytes")
    out = {"memory": None, "filesystems": []}
    if isinstance(used, int) and isinstance(available, int):
        out["memory"] = {"used": used, "total": used + available}
    fs = node.get("fs") or {}
    if isinstance(fs.get("capacityBytes"), int):
        out["filesystems"].append(
            {"mount": "/var", "used": int(fs.get("usedBytes") or 0), "total": fs["capacityBytes"]}
        )
    found = {}
    for pod in (summary or {}).get("pods") or []:
        for volume in pod.get("volume") or []:
            claim = volume.get("pvcRef") or {}
            path = paths.get((claim.get("namespace", ""), claim.get("name", "")), "")
            prefix = max(
                (p for p in volumes or {} if path == p or path.startswith(p.rstrip("/") + "/")), key=len, default=""
            )
            if not prefix or not isinstance(volume.get("capacityBytes"), int):
                continue
            entry = found.setdefault(
                prefix,
                {"mount": volumes[prefix], "path": prefix, "used": 0, "total": 0, "claims": []},
            )
            entry["used"] = max(entry["used"], int(volume.get("usedBytes") or 0))
            entry["total"] = max(entry["total"], volume["capacityBytes"])
            name = f"{claim.get('namespace', '')}/{claim.get('name', '')}"
            if name not in entry["claims"]:
                entry["claims"].append(name)
    for prefix in sorted(found, key=lambda p: found[p]["mount"]):
        found[prefix]["claims"].sort()
        out["filesystems"].append(found[prefix])
    return out


def _gpus(node, pods):
    allocatable = (node.get("status") or {}).get("allocatable") or {}
    out = []
    for resource, count in sorted(allocatable.items()):
        if "gpu" not in resource.partition("/")[0] or str(count) in ("0", ""):
            continue
        users = set()
        for pod in pods:
            if (pod.get("spec") or {}).get("nodeName") != node["metadata"]["name"]:
                continue
            if (pod.get("status") or {}).get("phase") not in ("Running", "Pending"):
                continue
            for container in (pod.get("spec") or {}).get("containers") or []:
                limits = (container.get("resources") or {}).get("limits") or {}
                if str(limits.get(resource, "0")) not in ("0", ""):
                    users.add((pod["metadata"].get("labels") or {}).get(APP_NAME) or pod["metadata"]["name"])
        out.append({"resource": resource, "count": int(count), "used_by": sorted(users)})
    return out


def _days_left(when, now):
    seconds = _when(when)
    return None if seconds is None else int((seconds - now) // DAY_SECONDS)


def cluster(
    found,
    nodes=(),
    apps_path="",
    annotation="",
    database_label="",
    volumes=None,
    settings=None,
    pins=None,
    kubeconfig="",
    now=0,
):
    """What the cluster's reads say, as the report needs it.

    `found` is {unread, results, access}: why the cluster could not be read at all (else ""), status_read's results
    by name (nodes, deployments, pods, services, ingresses, persistentvolumes, kustomizations, helmreleases,
    fluxinstances, certificates, settings, stats-<node>, alerts, etcd, remote) and what status_credentials found in
    the credentials. `nodes` is the cluster's nodes, each {name, role}; `apps_path` the path under which Flux's
    Kustomizations apply the apps; `annotation` the one just suspend records an app's replicas in; `database_label`
    (key=value) marks an app's database; `volumes` {path: label} names the nodes' volumes; `settings` {allowlist}
    names the cluster settings' key for Traefik's allowed range; `pins` {talos, kubernetes} the versions the
    configuration pins; `kubeconfig` the kubeconfig as the kubectl commands the report suggests name it; `now`
    seconds since the epoch.
    """
    found = found or {}
    if found.get("unread"):
        return {"unread": found["unread"]}
    results = found.get("results") or {}
    listed, why = _json(results, "nodes")
    if listed is None:
        return {"unread": f"the cluster's API: {why}"}
    errors = []

    def read(name, label):
        data, why = _json(results, name)
        if data is None and name in results:
            errors.append(f"{label}: {why}")
        return data

    deployments = _items(read("deployments", "the Deployments"))
    pods = _items(read("pods", "the pods"))
    services = _items(read("services", "the Services"))
    ingresses = _items(read("ingresses", "the Ingresses"))
    kustomizations = _items(read("kustomizations", "Flux's Kustomizations"))
    helmreleases = _items(read("helmreleases", "Flux's HelmReleases"))
    instances = _items(read("fluxinstances", "the FluxInstance"))
    certificates = _items(read("certificates", "the Certificates"))
    paths = _volume_paths(_items(read("persistentvolumes", "the volumes")))
    settings_data = (read("settings", "the cluster's settings") or {}).get("data") or {}

    roles = {n.get("name"): n.get("role", "") for n in nodes or []}
    out_nodes = []
    for node in _items(listed):
        meta, status = node.get("metadata") or {}, node.get("status") or {}
        name = meta.get("name", "")
        info = status.get("nodeInfo") or {}
        talos = re.search(r"\((v[^)]+)\)", str(info.get("osImage") or ""))
        summary, why = _json(results, f"stats-{name}")
        if summary is None and f"stats-{name}" in results:
            errors.append(f"{name}'s kubelet statistics: {why}")
        usage = node_usage(summary, paths, volumes)
        out_nodes.append(
            {
                "name": name,
                "role": roles.get(name)
                or ("controlplane" if CONTROL_PLANE_LABEL in (meta.get("labels") or {}) else "worker"),
                "ready": _condition(node).get("status") == "True",
                "pressure": [c for c in PRESSURES if _condition(node, c).get("status") == "True"],
                "talos": talos.group(1) if talos else str(info.get("osImage") or ""),
                "kubelet": str(info.get("kubeletVersion") or ""),
                **usage,
                "gpus": _gpus(node, pods),
            }
        )

    balancers = []
    for service in services:
        if (service.get("spec") or {}).get("type") != "LoadBalancer":
            continue
        ingress = ((service.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []
        balancers.append(
            {
                "name": service["metadata"]["name"],
                "namespace": service["metadata"].get("namespace", ""),
                "address": next((i.get("ip") for i in ingress if i.get("ip")), ""),
            }
        )

    certs = []
    for cert in certificates:
        status = cert.get("status") or {}
        ready = _condition(cert)
        certs.append(
            {
                "name": f"{cert['metadata'].get('namespace', '')}/{cert['metadata']['name']}",
                "names": (cert.get("spec") or {}).get("dnsNames") or [],
                "ready": ready.get("status") == "True",
                "message": _first_line(ready.get("message")),
                "not_after": status.get("notAfter", ""),
                "renewal": status.get("renewalTime", ""),
                "days_left": _days_left(status.get("notAfter"), now),
            }
        )

    remote_text, why = _out(results, "remote")
    if remote_text is None and "remote" in results:
        errors.append(f"the branch Flux syncs: {why}")
    remote = remote_text.split()[0] if remote_text and remote_text.split() else None
    etcd_text, why = _out(results, "etcd")
    if etcd_text is None and "etcd" in results:
        errors.append(f"etcd's state: {why}")
    alerts, why = _json(results, "alerts")
    if alerts is None and "alerts" in results:
        errors.append(f"Alertmanager's alerts: {why}")
    access = found.get("access") or {}
    settings = settings or {}
    return {
        "unread": "",
        "nodes": out_nodes,
        "apps": apps(
            deployments, pods, services, ingresses, kustomizations, apps_path, annotation, database_label, now
        ),
        "flux": _flux(instances, kustomizations, remote),
        "kustomizations": _flux_objects(kustomizations, "Kustomizations"),
        "helmreleases": _flux_objects(helmreleases, "HelmReleases"),
        "etcd": _etcd(etcd_text),
        "balancers": balancers,
        "certificates": certs,
        "allowlist": str(settings_data.get(settings.get("allowlist") or "", "")),
        "access": {
            "kubeconfig": access.get("kubeconfig", ""),
            "kubeconfig_days": _days_left(access.get("kubeconfig"), now),
            "talosconfig": access.get("talosconfig", ""),
            "talosconfig_days": _days_left(access.get("talosconfig"), now),
        },
        "alerts": _alerts(alerts) if alerts is not None else None,
        "pins": {
            "talos": str((pins or {}).get("talos") or ""),
            "kubernetes": str((pins or {}).get("kubernetes") or ""),
        },
        "kubeconfig": kubeconfig,
        "errors": errors,
    }


def _not_after(encoded):
    """When a base64-encoded PEM certificate expires, as RFC 3339; empty for anything that is not one."""
    if not encoded or x509 is None:
        return ""
    try:
        cert = x509.load_pem_x509_certificate(base64.b64decode(encoded))
    except (ValueError, TypeError):
        return ""
    return cert.not_valid_after_utc.isoformat()


def credentials(kubeconfig=None, talosconfig=None):
    """The server a kubeconfig names, and when its and a talosconfig's client certificates expire. Read under no_log,
    since both files hold the cluster's admin keys; nothing but these three values comes back."""
    kube = kubeconfig if isinstance(kubeconfig, dict) else {}
    clusters = kube.get("clusters") or [{}]
    users = kube.get("users") or [{}]
    talos = talosconfig if isinstance(talosconfig, dict) else {}
    context = (talos.get("contexts") or {}).get(talos.get("context") or "") or {}
    return {
        "server": str(((clusters[0] or {}).get("cluster") or {}).get("server") or ""),
        "kubeconfig": _not_after(((users[0] or {}).get("user") or {}).get("client-certificate-data")),
        "talosconfig": _not_after(context.get("crt")),
    }


# ---------------------------------------------------------------------------------------------- the NAS ---

# TrueNAS's alert levels that are a problem, and the one that is a thing to do; INFO and NOTICE are only listed, by
# their first sentence: TrueNAS follows it with advice (a pool's feature flags take four lines). A warning or worse
# keeps its whole text, which may go on to name the devices at fault.
NAS_ALERT_PROBLEM = ("ERROR", "CRITICAL", "ALERT", "EMERGENCY")
NAS_ALERT_TODO = ("WARNING",)
FIRST_SENTENCE = re.compile(r"(.+?[.!?])\s+(?=[A-Z])", re.S)
# Days past a scrub task's threshold before its pool counts as overdue: the task runs weekly, then waits for that.
SCRUB_GRACE_DAYS = 8


def _plain_text(text):
    """An alert's text without the HTML TrueNAS formats some with, on one line."""
    text = re.sub(r"<[^>]+>", "", re.sub(r"<br\s*/?>", " ", str(text or ""), flags=re.I))
    return " ".join(html.unescape(text).split())


def _every(schedule):
    """How often a periodic snapshot task's cron schedule runs: hourly, daily, weekly or monthly."""
    schedule = schedule or {}
    if str(schedule.get("hour", "*")) == "*":
        return "hourly"
    if str(schedule.get("dom", "*")) != "*":
        return "monthly"
    if str(schedule.get("dow", "*")) != "*":
        return "weekly"
    return "daily"


def _nas_unread(state):
    message = str(state.get("msg") or "not read").rstrip(".")
    errors = state.get("errors")
    if isinstance(errors, dict) and errors:
        message += ": " + "; ".join(f"{host}: {why}" for host, why in errors.items())
    return message


def nas(state, release=""):
    """What the NAS says, from truenas_info with its status option (roles/truenas's status entry point): each pool's
    health, capacity and last scrub, TrueNAS's alerts, each periodic snapshot task's last run, the NFS service, and
    the release it runs against `release`, the one its role is written for. A NAS that did not answer, refused the
    key or presented another certificate than the pinned one is {"unread": why}."""
    state = state if isinstance(state, dict) else {}
    health = state.get("status")
    if not state.get("api_host") or not isinstance(health, dict):
        return {"unread": _nas_unread(state)}
    pools = []
    for pool in health.get("pools") or []:
        used, available = pool.get("used"), pool.get("available")
        scan = pool.get("scan") or None
        pools.append(
            {
                "name": str(pool.get("name") or ""),
                "status": str(pool.get("status") or ""),
                "healthy": bool(pool.get("healthy")),
                "detail": str(pool.get("detail") or ""),
                "layout": str(pool.get("layout") or ""),
                "used": used or 0,
                "total": (used or 0) + (available or 0) if used is not None and available is not None else 0,
                "scan": {
                    k: scan.get(k) for k in ("function", "state", "start_time", "end_time", "percentage", "errors")
                }
                if scan
                else None,
                "scrub": pool.get("scrub") or None,
            }
        )
    nfs = (state.get("services") or {}).get("nfs") or {}
    return {
        "name": str(state.get("hostname") or ""),
        "address": str(state["api_host"]),
        "version": str(state.get("version") or ""),
        "release": str(release or ""),
        "ready": bool(state.get("ready")),
        "pending_network": bool(state.get("network_pending_changes")),
        "uptime": health.get("uptime_seconds"),
        "pools": pools,
        "alerts": [
            {
                "level": str(a.get("level") or "").upper(),
                "klass": str(a.get("klass") or ""),
                "text": _plain_text(a.get("text")),
                "since": a.get("datetime") or "",
            }
            for a in health.get("alerts") or []
        ],
        "snapshots": [
            {
                "dataset": str(t.get("dataset") or ""),
                "enabled": bool(t.get("enabled")),
                "every": _every(t.get("schedule")),
                "state": str(t.get("state") or ""),
                "last_run": t.get("last_run") or "",
                "error": str(t.get("error") or ""),
            }
            for t in health.get("snapshot_tasks") or []
        ],
        "nfs": {
            "enabled": bool(nfs.get("enable")),
            "state": str(nfs.get("state") or ""),
            "shares": sum(1 for share in state.get("nfs_shares") or [] if share.get("enabled", True)),
        },
    }


def _plain(url):
    return "http://" + url.split("://", 1)[1]


def probe_targets(found, types=()):
    """What status_probe asks: each app's https URL and its http one, sent to the address Traefik answers on, and
    the address of an app on a VM of its own (Home Assistant). Nothing when the apps are not shown."""
    if "apps" not in types_shown(types):
        return []
    out = []
    for app in ((found or {}).get("cluster") or {}).get("apps") or []:
        for url in app.get("urls") or []:
            out.append({"url": url["url"], "address": url.get("address", "")})
            out.append({"url": _plain(url["url"]), "address": url.get("address", "")})
    for vm in ((found or {}).get("proxmox") or {}).get("vms") or []:
        if (vm.get("app") or {}).get("url") and vm.get("status") == "running":
            out.append({"url": vm["app"]["url"], "address": ""})
    unique = {}
    for target in out:
        unique.setdefault(target["url"], target)
    return list(unique.values())


# --------------------------------------------------------------------------------------------- the report ---

# HTTP statuses that send the browser elsewhere.
REDIRECTS = (301, 302, 303, 307, 308)
# Days before a credential's or certificate's expiry when it becomes a thing to do; the talos role renews the
# credentials, and cert-manager the certificates, from 30 days before.
RENEW_DAYS = 30
CERT_PROBLEM_DAYS = 14


def _gib(value, decimals=None):
    """Bytes as GiB: one decimal under 10 (or always, with decimals=1), whole above."""
    gib = value / GIB
    if decimals is None:
        decimals = 1 if gib < 10 else 0
    return f"{gib:.{decimals}f}"


def _item(level, subject, text, command=""):
    return {"level": level, "subject": subject, "text": text, "command": command}


def _and(names):
    names = list(names)
    return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)


def _vm_attention(found):
    """Things to do about this repo's VMs: missing ones, stopped ones, settings waiting for a restart."""
    out, stopped = [], {}
    for vm in found.get("vms") or []:
        label = f"{vm['vmid']} {vm['name']}".strip()
        if vm["status"] == "missing":
            out.append(_item("todo", "missing", f"VM {label} is not on this host", vm.get("made_by", "")))
        elif vm["status"] != "running":
            stopped.setdefault(vm.get("start", ""), []).append(label)
        elif vm.get("pending"):
            keys = ", ".join(vm["pending"])
            out.append(
                _item("todo", "restart", f"{label} waits for a restart to apply {keys}", f"qm reboot {vm['vmid']}")
            )
    for start, labels in stopped.items():
        out.append(_item("todo", "stopped", f"{_and(labels)} {'is' if len(labels) == 1 else 'are'} stopped", start))
    return out


def stamp_reads(found, deploy, checkout):
    """The reads that place each machine's version stamp against this checkout's commit: for every stamped commit but
    the checkout's own, whether it comes before it (ancestor-<commit>) and whether it comes after it
    (descendant-<commit>), each a git merge-base --is-ancestor in `checkout`. None without a version for the
    checkout, as for a run of just status from a tree that is no git checkout, and none for a stamp nothing places
    (one from uncommitted files, a VM no command of this repo deploys)."""
    current = (deploy or {}).get("commit") or ""
    if not STAMP_COMMIT.fullmatch(current) or not checkout:
        return {}
    prox = (found or {}).get("proxmox") or {}
    stamps = [vm.get("stamp") for vm in prox.get("vms") or [] if vm.get("made_by")]
    stamps.append((prox.get("host") or {}).get("stamp"))
    placed = [s for s in stamps if s and s.get("commit") and not s["describe"].endswith("-dirty")]
    out = {}
    for commit in sorted({s["commit"] for s in placed if s["commit"] != current}):
        out[f"ancestor-{commit}"] = ["git", "-C", checkout, "merge-base", "--is-ancestor", commit, current]
        out[f"descendant-{commit}"] = ["git", "-C", checkout, "merge-base", "--is-ancestor", current, commit]
    return out


def _git_said(result):
    """What a git read said when it failed: its error, or the last line it wrote to stderr."""
    said = [line.strip() for line in str(result.get("stderr") or "").splitlines() if line.strip()]
    return result.get("error") or (said[-1] if said else f"rc {result.get('rc')}")


def deployed(found_stamp, deploy, ancestry, command, unread=""):
    """Where a machine's version stamp lies against this checkout's commit: {commit, describe, date, state, command},
    the state current, behind (this checkout's commit comes after it), ahead (before it), elsewhere (neither: another
    branch's), unknown (a commit this checkout does not have, as git says of one it never fetched), unplaced (git
    could not say: `why` says what it said), dirty (deployed from uncommitted files), broken (Notes the next deploy
    refuses: `why` says how), never (no stamp yet), unread (its Notes could not be read: `unread` says why), or ""
    when this checkout has no version to compare with, whatever the stamp says. checkout_dirty marks a stamp at this
    checkout's commit when the checkout has uncommitted changes, which the machine does not run. `command` is what
    deploys the machine."""
    none = {"commit": "", "describe": "", "date": "", "command": command}
    if unread:
        return none | {"state": "unread"}
    if not found_stamp:
        return none | {"state": "never"}
    if found_stamp.get("broken"):
        return none | {"state": "broken", "why": found_stamp["broken"]}
    current = (deploy or {}).get("commit") or ""
    commit, why = found_stamp["commit"], ""
    if not current:
        state = ""
    elif found_stamp["describe"].endswith("-dirty"):
        state = "dirty"
    elif commit == current:
        state = "current"
    else:
        reads = [(ancestry or {}).get(f"{side}-{commit}") or {} for side in ("ancestor", "descendant")]
        failed = [r for r in reads if r.get("rc") not in (0, 1)]
        if any(r.get("rc") == 0 for r in reads):
            state = "behind" if reads[0].get("rc") == 0 else "ahead"
        elif not failed:
            state = "elsewhere"
        elif all(r.get("rc") == 128 and "Not a valid commit name" in str(r.get("stderr")) for r in failed):
            state = "unknown"
        else:
            state, why = "unplaced", _git_said(failed[0])
    out = {**found_stamp, "state": state, "command": command}
    if why:
        out["why"] = why
    if state == "current" and str((deploy or {}).get("describe") or "").endswith("-dirty"):
        out["checkout_dirty"] = True
    return out


def _stamp_attention(prox, checkout):
    """Machines a deploy would bring to this checkout's commit: those behind it, and those deployed from uncommitted
    files, which only a deploy from a commit names again, each group of machines that one command brings from one
    version said once; and Notes the next deploy would refuse. From a checkout with uncommitted changes, which just
    deploy refuses, each says to commit them first."""
    machines = [("the host", (prox.get("host") or {}).get("deployed"))]
    machines += [(f"{vm['vmid']} {vm['name']}".strip(), vm.get("deployed")) for vm in prox.get("vms") or []]
    first = " (commit this checkout's changes first: just deploy refuses them)"
    first = first if checkout.get("describe", "").endswith("-dirty") else ""
    groups, out = {}, []
    for label, d in machines:
        if d and d["state"] in ("behind", "dirty"):
            groups.setdefault((d["state"], d["command"], d["describe"]), []).append(label)
        elif d and d["state"] == "broken":
            text = f"{label}'s Notes hold a broken version stamp ({d['why']}): mend or remove it by hand, or the "
            text += "next deploy refuses them"
            out.append(_item("todo", "version", text))
    for (state, command, describe), labels in groups.items():
        was = "was" if len(labels) == 1 else "were"
        if state == "behind":
            text = f"{_and(labels)} {was} deployed from {describe}; this checkout is at {checkout.get('describe', '')}"
        else:
            text = f"{_and(labels)} {was} deployed from uncommitted files ({describe})"
        out.append(_item("todo", "version", text + first, command))
    return out


def _fill_attention(subject, used, total):
    fraction = used / total if total else 0
    text = f"{fraction:.0%} full: {_gib(used)} of {_gib(total)} GiB"
    if fraction >= STORAGE_PROBLEM:
        return [_item("problem", subject, text)]
    if fraction >= STORAGE_TODO:
        return [_item("todo", subject, text)]
    return []


def _storage_attention(storages):
    out = []
    for s in storages:
        if s["status"] != "available":
            out.append(_item("problem", s["id"], f"the storage is {s['status'] or 'not available'}"))
            continue
        out += _fill_attention(s["id"], s["used"], s["total"])
        budget = s.get("budget")
        if budget and budget["provisioned"] > budget["limit"]:
            out.append(
                _item(
                    "todo",
                    s["id"],
                    f"its disks are provisioned at {_gib(budget['provisioned'])} GiB, more than the "
                    f"{_gib(budget['limit'])} GiB it can hold: as they fill, every VM on it gets I/O errors",
                    "keep its use well below 100% (pvesm status)",
                )
            )
    return out


def _host_attention(found, commands):
    out = []
    if found.get("reboot"):
        out.append(
            _item("todo", "reboot", "the host waits for a reboot an upgrade asked for", "reboot it at a quiet time")
        )
    if found.get("upgrades"):
        count = len(found["upgrades"])
        text = f"{count} package upgrade{'s' if count != 1 else ''} wait"
        out.append(_item("todo", "upgrades", text, commands.get("upgrade", "")))
    return out


# A VM's newest backup may be this much older than the run that was due, which starts at its time: a daylight-saving
# change between the next two runs moves the time worked out for the last one by an hour.
BACKUP_SLACK = 7200
ETCD_SNAPSHOT = re.compile(r"-(\d{8}T\d{6}Z)\.db$")


def _due(job, now):
    """When the job's last run was due: its next run less its period, or its next run when that has passed."""
    if not job or not job.get("next_run") or not job.get("period"):
        return None
    return job["next_run"] - job["period"] if job["next_run"] > now else job["next_run"]


def _backup_state(vm, job, unknown, running, now):
    if vm["status"] == "missing":
        return "missing"
    if unknown or (vm["job"] and not vm["read"]):
        return "unknown"
    if not vm["job"]:
        return "left out"
    if vm["failure"]:
        return "failed"
    if vm["newest"] is None:
        return "none"
    due = _due(job, now)
    if due and running is None and vm["newest"] < due - min(BACKUP_SLACK, job["period"] // 2):
        return "missed"
    return "ok"


def _backups_model(prox, snapshots, now, commands):
    """The backups section: each job with when it runs next, each VM's state (ok, failed, missed, none yet, left out
    of its job, missing, unknown) and ages, and the cluster's etcd snapshots. None when they were not read."""
    found = prox.get("backups")
    if found is None:
        return None
    jobs = [j | {"next_in": j["next_run"] - now if j["next_run"] else None} for j in found["jobs"]]
    by_id = {j["id"]: j for j in jobs}
    unknown = any(j["on_host"] is None for j in jobs)
    vms = []
    for vm in found["vms"]:
        failure = vm["failure"]
        if failure and vm["newest"] is not None and vm["newest"] >= failure["time"]:
            failure = None  # a backup made since
        vm = vm | {"failure": failure}
        state = _backup_state(vm, by_id.get(vm["job"]), unknown, found["running"], now)
        vms.append(
            vm
            | {
                "state": state,
                "age": now - vm["newest"] if vm["newest"] is not None else None,
                "failed_ago": now - failure["time"] if failure else None,
                "due_ago": now - _due(by_id.get(vm["job"]), now) if state == "missed" else None,
            }
        )
    etcd = None
    if snapshots is not None:
        times = []
        for name in snapshots.get("files") or []:
            match = ETCD_SNAPSHOT.search(str(name))
            if match:
                times.append(int(datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).timestamp()))
        etcd = {
            "dir": snapshots.get("dir", ""),
            "count": len(times),
            "age": now - max(times) if times else None,
            "command": commands.get("etcd_snapshot", ""),
        }
    running = found["running"]
    return {"jobs": jobs, "vms": vms, "running": now - running if running is not None else None, "etcd": etcd}


def _labels(vms):
    return _and(f"{vm['vmid']} {vm['name']}".strip() for vm in vms)


def _backup_attention(model, commands):
    out = []
    jobs, vms = model["jobs"], [vm for vm in model["vms"] if vm["state"] != "missing"]
    if not jobs:
        if vms:
            text = f"nothing is backed up: no backup job covers {_labels(vms)}"
            out.append(_item("todo", "backup", text, commands.get("backup_target", "")))
        return out
    for job in jobs:
        if job["on_host"] is None:
            continue
        if not job["on_host"]:
            text = f"the backup job {job['id']} is not on the host"
        elif not job["enabled"]:
            text = f"the backup job {job['id']} is disabled"
        elif job["storage"] != job["wanted_storage"]:
            text = f"the backup job {job['id']} writes to {job['storage']}, not {job['wanted_storage']}"
        else:
            left = [vm for vm in vms if vm["state"] == "left out" and vm["vmid"] in job["wanted_vmids"]]
            text = f"the backup job {job['id']} leaves out {_labels(left)}" if left else ""
        if text:
            out.append(_item("todo", "backup", text, commands.get("backup", "")))
    failed = {}
    for vm in vms:
        if vm["state"] == "failed":
            failed.setdefault((vm["failure"]["reason"], vm["failed_ago"]), []).append(vm)
    for (reason, ago), group in failed.items():
        text = f"the backup{'s' if len(group) > 1 else ''} of {_labels(group)} failed {_ago(ago)} ago: {reason}"
        if len(group) == 1 and group[0]["age"] is not None:
            text += f"; the last good one is {_ago(group[0]['age'])} old"
        elif len(group) == 1:
            text += "; it has none"
        out.append(_item("problem", "backup", text))
    for job in jobs:
        missed = [vm for vm in vms if vm["state"] == "missed" and vm["job"] == job["id"]]
        if missed:
            text = f"{job['id']}'s run due {_ago(missed[0]['due_ago'])} ago made no backup of {_labels(missed)}"
            out.append(_item("problem", "backup", text))
        none = [vm for vm in vms if vm["state"] == "none" and vm["job"] == job["id"]]
        if none:
            text = f"{_labels(none)} {'has' if len(none) == 1 else 'have'} no backup yet"
            if job["next_in"] is not None and job["next_in"] > 0:
                text += f": {job['id']} runs next in {_uptime(job['next_in'])}"
            out.append(_item("todo", "backup", text))
    return out


def unknown_types(types):
    """The names in `types` that are no section of the report."""
    return [t for t in types or [] if t not in TYPES]


def types_shown(types):
    """The sections `types` asks for, in the report's order; none asked for means every one."""
    return [t for t in TYPES if not types or t in types]


# What the report is worked out from: the Proxmox host's findings (proxmox, and tailscale, its Tailscale), the
# cluster's (cluster, and etcd_snapshots, the snapshot files on this machine), the NAS's (nas), and from this machine
# the apps' probes and its own Tailscale's view of the tailnet's DNS (tailnet_dns). Each host reads only the sources
# the sections shown need, so just status nas reads neither the Proxmox host nor the cluster.
SOURCES = ("proxmox", "tailscale", "tailnet_dns", "cluster", "etcd_snapshots", "probes", "nas")
HOST_SOURCES = {"proxmox": ("proxmox", "tailscale"), "cluster": ("cluster", "etcd_snapshots"), "nas": ("nas",)}


def section_sources(section, types=()):
    """The sources a section of the report is worked out from, when `types` are the sections shown. The apps list Home
    Assistant's VM and say when the cluster's VMs are stopped, as the cluster does; the VMs and the storage show what
    the cluster says of its nodes; the cluster's TLS line comes from the probes, which ask only when the apps are
    shown; the apps say how they answer from the tailnet when Tailscale is shown too."""
    shown = types_shown(types)
    tailscale = ("tailscale", "tailnet_dns")
    probes = ("probes",) if "apps" in shown else ()
    return {
        "apps": ("proxmox", "cluster", "probes", *(tailscale if "tailscale" in shown else ())),
        "admin": ("tailscale",),
        "tailscale": (*tailscale, "cluster"),
        "vms": ("proxmox", "cluster"),
        "storage": ("proxmox", "cluster"),
        "backups": ("proxmox", "etcd_snapshots"),
        "cluster": ("proxmox", "cluster", *probes),
        "nas": ("nas",),
    }[section]


def sources(types=()):
    """The sources the sections `types` asks for are worked out from, in SOURCES' order."""
    needed = {s for t in types_shown(types) for s in section_sources(t, types)}
    return [s for s in SOURCES if s in needed]


def host_reads(source, types=()):
    """What the host whose status_source is `source` reads for the sections `types` asks for: none, some or all of
    the sources it has."""
    needed = sources(types)
    return [s for s in HOST_SOURCES.get(source, ()) if s in needed]


# Whose findings say each source, by the label just status gives each host (proxmox, cluster, nas): the Proxmox
# host's say its Tailscale and this machine's view of the tailnet's DNS (in status_early, before its own reads), the
# probes come from the cluster's (its apps) and the Proxmox host's (Home Assistant's VM), both asked from this machine.
SOURCE_HOSTS = {
    "proxmox": ("proxmox",),
    "tailscale": ("proxmox",),
    "tailnet_dns": ("proxmox",),
    "cluster": ("cluster",),
    "etcd_snapshots": ("cluster",),
    "probes": ("cluster", "proxmox"),
    "nas": ("nas",),
}


def merge(found):
    """What the hosts found (each one's status_found, in a list), as one {source: findings} for status_report, every
    host's probes together."""
    out, probes = {}, []
    for each in found or []:
        for key, value in (each or {}).items():
            if key == "probes":
                probes += value or []
            else:
                out[key] = value
    return out | {"probes": probes}


def sections_expected(setup, types=()):
    """The sections shown that this setup can have: the NAS's only with a NAS, the cluster's only with a cluster, the
    apps only with a cluster or Home Assistant."""
    setup = setup or {}
    can = {
        "nas": setup.get("nas"),
        "cluster": setup.get("cluster"),
        "apps": setup.get("cluster") or setup.get("home_assistant"),
    }
    return [t for t in types_shown(types) if can.get(t, True)]


def _host_of(url):
    return url.split("://", 1)[-1].split("/", 1)[0].rsplit("@", 1)[-1].split(":", 1)[0]


def _app_rows(clu, prox, probes):
    """Each app's row: its state and version, and its URLs with what they answered here. An https URL's http twin is
    listed too when it answers by itself rather than redirecting to https (Jellyfin's, for TV apps)."""
    by_url = {p.get("url"): p for p in probes or []}
    rows = []
    for app in clu.get("apps") or []:
        urls = []
        for url in app["urls"]:
            probe = by_url.get(url["url"]) or {}
            urls.append({"url": url["url"], "http": probe.get("status"), "error": probe.get("error", "")})
            plain = by_url.get(_plain(url["url"])) or {}
            code = plain.get("status")
            redirected = code in REDIRECTS and str(plain.get("location", "")).startswith("https://")
            if code and code < 400 and not redirected:
                urls.append({"url": _plain(url["url"]), "http": code, "error": ""})
        urls += [{"url": stream, "http": None, "error": ""} for stream in app.get("streams") or []]
        keep = ("name", "group", "deployment", "state", "detail", "version", "wanted", "ready", "restarts", "since")
        rows.append({k: app.get(k) for k in keep} | {"urls": urls, "databases": app.get("databases") or []})
    for vm in prox.get("vms") or []:
        app = vm.get("app")
        if not app:
            continue
        probe = by_url.get(app["url"]) or {}
        release = re.search(r"\d+(?:\.\d+)+", vm.get("os") or "")
        rows.append(
            {
                "name": app["name"],
                "group": f"VM {vm['vmid']}",
                "deployment": "",
                "state": vm["status"] if vm["status"] in ("running", "missing") else "stopped",
                "detail": "",
                "version": release.group(0) if release else "",
                "wanted": 1,
                "ready": 1 if vm["status"] == "running" else 0,
                "restarts": 0,
                "since": "",
                "urls": [{"url": app["url"], "http": probe.get("status"), "error": probe.get("error", "")}]
                if app["url"]
                else [],
                "databases": [],
            }
        )
    return rows


def _dns(clu, probes):
    """Whether this machine resolves each app's host name to the address Traefik answers on."""
    expected = {}
    for app in clu.get("apps") or []:
        for url in app["urls"]:
            if url.get("address"):
                expected.setdefault(url["host"], url["address"])
    if not expected:
        return None
    resolved = {}
    for probe in probes or []:
        resolved.setdefault(_host_of(probe.get("url", "")), probe)
    missing, wrong = [], {}
    for host, address in sorted(expected.items()):
        probe = resolved.get(host)
        if probe is None:
            continue
        if not probe.get("resolved"):
            missing.append(host)
        elif address not in probe["resolved"]:
            wrong[host] = probe["resolved"]
    addresses = sorted(set(expected.values()))
    asked = [h for h in expected if h in resolved]
    if not asked:
        return None
    state = "ok" if not missing and not wrong else "missing" if len(missing) == len(asked) else "partly"
    return {"state": state, "address": addresses[0], "asked": len(asked), "missing": missing, "wrong": wrong}


def _traefik(clu):
    """The address the apps' Ingresses say Traefik answers on, the most common if they differ."""
    addresses = [u["address"] for a in clu.get("apps") or [] for u in a["urls"] if u.get("address")]
    return max(sorted(set(addresses)), key=addresses.count) if addresses else ""


def _closed(allowlist):
    """Whether an allowed range lets in nobody but this machine itself (Traefik's start, 127.0.0.1/32)."""
    ranges = [part.strip() for part in str(allowlist or "").split(",") if part.strip()]
    return bool(ranges) and all(r.startswith("127.") or r == "::1/128" for r in ranges)


def _tls(clu, probes, now):
    """The certificate Traefik serves, and which Certificate it is, matched by its expiry."""
    served = next((p["certificate"] for p in probes or [] if p.get("certificate")), None)
    if served is None:
        return None
    expiry = _when(served.get("not_after"))
    match = next(
        (
            c
            for c in clu.get("certificates") or []
            if expiry and _when(c.get("not_after")) and abs(_when(c["not_after"]) - expiry) < 2
        ),
        None,
    )
    return {
        "issuer": served.get("issuer", ""),
        "not_after": served.get("not_after", ""),
        "days_left": _days_left(served.get("not_after"), now),
        "certificate": match["name"] if match else "",
        "renewal": match["renewal"] if match else "",
    }


def _apps_attention(rows, dns, clu, tls, commands, kubeconfig):
    out, suspended = [], []
    kubectl = f"kubectl --kubeconfig {kubeconfig}" if kubeconfig else "kubectl"
    for row in rows:
        for part in [row, *({**db, "group": row["group"], "name": f"{row['name']} db"} for db in row["databases"])]:
            if part["state"] == "failing":
                crashed = "CrashLoopBackOff" in part["detail"] or "RunContainerError" in part["detail"]
                command = (
                    f"{kubectl} -n {part['group']} logs deploy/{part['deployment']} --previous"
                    if crashed
                    else f"{kubectl} -n {part['group']} get events --sort-by=.lastTimestamp"
                )
                out.append(_item("problem", part["name"], part["detail"], command))
            elif part["state"] == "not applied":
                out.append(_item("problem", part["name"], f"Flux did not apply it: {part['detail']}"))
            elif part["state"] == "waiting":
                out.append(_item("todo", part["name"], part["detail"], commands.get("secrets", "")))
            elif part["state"] == "stopped" and part.get("deployment"):
                out.append(_item("todo", part["name"], "is scaled to zero, though not by just suspend"))
        if row["state"] == "suspended":
            suspended.append(row)
        elif row["state"] == "running" and row.get("deployment"):
            failed = [u for u in row["urls"] if u["url"].startswith("https://") and (u["http"] or 0) >= 500]
            if failed:
                text = f"answers {failed[0]['http']} through Traefik, though its pods are ready"
                out.append(_item("problem", row["name"], text))
    if suspended:
        names = [r["name"] for r in suspended]
        dates = sorted(r["since"][:10] for r in suspended if r.get("since"))
        text = f"{_and(names)} {'is' if len(names) == 1 else 'are'} suspended"
        text += f" since {dates[0]}" if dates else ""
        command = commands.get("resume", "").replace("{names}", ",".join(names))
        out.append(_item("todo", "suspended", text, command))
    https = [u for r in rows if r.get("deployment") for u in r["urls"] if u["url"].startswith("https://")]
    if https and all(u["error"] for u in https) and dns:
        out.append(_item("problem", "traefik", f"{dns['address']} did not answer from here: {https[0]['error']}"))
    elif _closed(clu.get("allowlist")):
        text = f"Traefik lets in {clu['allowlist']} alone, so every app answers 403 until first visits are done"
        out.append(_item("todo", "allowlist", text, commands.get("allowlist", "")))
    if dns and dns["missing"]:
        count, total = len(dns["missing"]), dns["asked"]
        names = (
            "the apps' names do not"
            if dns["state"] == "missing"
            else f"{_and(dns['missing'])} {'does' if count == 1 else 'do'} not"
            if count <= 2
            else f"{count} of the apps' {total} names do not"
        )
        out.append(
            _item(
                "todo",
                "dns",
                f"{names} resolve on this machine",
                f"a wildcard record for {dns['address']} in the router's DNS, or the /etc/hosts lines under APPS",
            )
        )
    for host, found in sorted((dns or {}).get("wrong", {}).items()):
        out.append(_item("todo", "dns", f"{host} resolves to {', '.join(found)} here, not Traefik's {dns['address']}"))
    if tls and clu.get("certificates") and not tls["certificate"]:
        text = f"Traefik serves a certificate from {tls['issuer'] or 'an unknown issuer'}, not the cluster's own"
        out.append(_item("todo", "tls", text))
    return out


def _nas_model(found, now):
    """The NAS's findings with the ages the report shows: each pool's last scrub and each snapshot task's last run,
    seconds before `now`."""

    def age(when):
        seconds = _when(when)
        return None if seconds is None or not now else max(int(now - seconds), 0)

    pools = []
    for pool in found.get("pools") or []:
        scan = pool.get("scan") or {}
        pools.append(pool | {"scanned_ago": age(scan.get("end_time")) if scan.get("state") == "FINISHED" else None})
    snapshots = [t | {"ago": age(t["last_run"])} for t in found.get("snapshots") or []]
    return found | {"pools": pools, "snapshots": snapshots}


def _nas_attention(found, commands):
    out = []
    if not found["ready"]:
        out.append(_item("problem", "nas", "TrueNAS is not ready: its middleware is still starting"))
    release, version = found.get("release"), found.get("version", "")
    if release and version and ".".join(version.split(".")[:2]) != release:
        text = f"the NAS runs TrueNAS {version}, and its configuration is written for {release}"
        out.append(_item("todo", "nas", text, commands.get("nas_release", "")))
    if found.get("pending_network"):
        text = "TrueNAS holds interface changes that were never committed"
        out.append(_item("todo", "nas", text, commands.get("nas_network", "")))
    for pool in found.get("pools") or []:
        name = pool["name"]
        if not pool["healthy"] or pool["status"] != "ONLINE":
            text = f"the pool is {pool['status'] or 'not healthy'}" + (f": {pool['detail']}" if pool["detail"] else "")
            out.append(_item("problem", name, text))
        fraction = pool["used"] / pool["total"] if pool["total"] else 0
        if fraction >= STORAGE_TODO:
            text = f"the pool is {fraction:.0%} full: {_sizes(pool['used'], pool['total'])}"
            out.append(_item("problem" if fraction >= STORAGE_PROBLEM else "todo", name, text))
        scan, scrub = pool.get("scan") or {}, pool.get("scrub") or {}
        if scan.get("function") == "SCRUB" and scan.get("state") == "FINISHED" and scan.get("errors"):
            errors = scan["errors"]
            text = f"its last scrub found {errors} error{'s' if errors != 1 else ''}"
            out.append(_item("problem", name, text))
        if not scrub.get("enabled"):
            out.append(_item("todo", name, "no scrub task scrubs it", commands.get("nas", "")))
        elif pool.get("scanned_ago") is not None and scrub.get("threshold"):
            days = pool["scanned_ago"] // DAY
            if days > scrub["threshold"] + SCRUB_GRACE_DAYS:
                text = f"its last scrub was {days} d ago, though its task scrubs every {scrub['threshold']} d"
                out.append(_item("todo", name, text))
    for task in found.get("snapshots") or []:
        if task["enabled"] and task["state"] == "ERROR":
            text = f"the {task['every']} snapshots of {task['dataset']} failed: {task['error'] or 'no reason given'}"
            out.append(_item("problem", "snapshots", text))
    for alert in found.get("alerts") or []:
        if alert["level"] in NAS_ALERT_PROBLEM + NAS_ALERT_TODO:
            level = "problem" if alert["level"] in NAS_ALERT_PROBLEM else "todo"
            out.append(_item(level, "nas", f"TrueNAS alerts ({alert['level'].lower()}): {alert['text']}"))
    nfs = found.get("nfs") or {}
    if nfs.get("shares") and nfs.get("state") != "RUNNING":
        count = nfs["shares"]
        text = f"the NFS service is {nfs.get('state', '').lower() or 'not running'}, so its {count} share"
        text += f"{'s are' if count != 1 else ' is'} not served"
        out.append(_item("problem", "nfs", text, commands.get("nas", "")))
    return out


def _network_of(address, networks):
    """The network of `networks` (CIDR strings) that holds `address`, or "": also for a name, which none holds."""
    try:
        ip = ipaddress.ip_address(str(address))
    except ValueError:
        return ""
    for network in networks:
        try:
            if ip in ipaddress.ip_network(str(network).strip(), strict=False):
                return network
        except ValueError:
            continue
    return ""


def _tailnet_dns(read, suffix, domain):
    """Where the tailnet's split DNS sends the apps' domain (or a parent of it), as this machine's Tailscale has it
    from the coordination server: a list of resolvers, empty for none, or None when this machine runs no Tailscale
    on the node's tailnet, so that nothing can be said."""
    if not domain or not suffix or not isinstance(read, dict) or read.get("rc") != 0:
        return None
    try:
        data = json.loads(read.get("stdout") or "{}")
    except ValueError:
        return None
    if not isinstance(data, dict) or (data.get("CurrentTailnet") or {}).get("MagicDNSSuffix") != suffix:
        return None
    routes = data.get("SplitDNSRoutes") or {}
    labels = domain.strip(".").split(".")
    for i in range(len(labels)):
        resolvers = routes.get(".".join(labels[i:]) + ".")
        if resolvers:
            return [r.get("Addr", "") for r in resolvers if isinstance(r, dict)]
    return []


def _tailscale_model(state, admin, traefik, domain, allowlist, workstation):
    """The Proxmox host's Tailscale as roles/proxmox_tailscale's status entry point read it, with how the apps answer
    from the tailnet: through a route this host serves that holds Traefik's address, tailnet clients arriving as the
    host's own address (which admin's tailscale_node page gives) when it masquerades them, which Traefik's allowed
    range must let in; and where the tailnet's split DNS sends the apps' domain, from this machine's Tailscale."""
    if not state:
        return None
    model = dict(state)
    if state.get("unread") or not state.get("running"):
        return model
    advertised = [r["route"] for r in state.get("routes") or [] if r["advertised"]]
    served = [r["route"] for r in state.get("routes") or [] if r["served"]]
    node = next((a for a in admin or [] if a.get("tailscale_node")), None)
    model["node_address"] = _host_of(node["url"]) if node else ""
    reach = None
    if traefik:
        route = _network_of(traefik, advertised)
        if not advertised:
            reach = {"state": "no route"}
        elif not route:
            reach = {"state": "outside", "address": traefik}
        elif route not in served:
            reach = {"state": "not served", "route": route}
        else:
            snat = state.get("snat", True) is not False
            source = model["node_address"] if snat else ""
            reach = {
                "state": "served",
                "route": route,
                "source": source,
                "allowed": bool(_network_of(source, str(allowlist or "").split(","))) if source else None,
            }
        if advertised:
            reach["split_dns"] = _tailnet_dns(workstation, state.get("suffix"), domain)
            reach["domain"] = domain
    model["reach"] = reach
    return model


def _admin_pages(admin, tail):
    """The admin pages, each with how it answers from the tailnet when the host runs Tailscale: the page the node
    serves itself at its tailnet name, every other through a route this host serves, on the ports its guard allows."""
    pages = [dict(a) for a in admin or []]
    if not tail or not tail.get("running") or tail.get("backend") != "Running":
        return pages
    advertised = [r["route"] for r in tail.get("routes") or [] if r["advertised"]]
    served = [r["route"] for r in tail.get("routes") or [] if r["served"]]
    guard = tail.get("guard") or {}
    for page in pages:
        host = _host_of(page.get("url", ""))
        if page.get("tailscale_node"):
            page["tailnet"] = page["url"].replace(host, tail["dns_name"], 1) if tail.get("dns_name") else ""
            continue
        route = _network_of(host, advertised)
        if not advertised:
            page["tailnet"] = "not without a route"
        elif not route:
            page["tailnet"] = "outside the routes"
        elif route not in served:
            page["tailnet"] = "route not served by this host"
        elif host in (guard.get("allow") or {}):
            page["tailnet"] = f"through the route, {', '.join(guard['allow'][host])} only"
        elif host in (guard.get("guarded") or []):
            page["tailnet"] = "through the route, nothing: guarded"
        else:
            page["tailnet"] = "through the route"
    return pages


def _tailscale_attention(tail, commands):
    """Problems and things to do about the Proxmox host's Tailscale. What only the admin console can fix comes from
    roles/proxmox_tailscale's own judgment, word for word as just deploy reports it; Tailscale's health warnings are
    only shown."""
    out = []
    deploy = commands.get("tailscale", "")
    if tail.get("unread"):
        return out
    if not tail.get("installed"):
        return [_item("todo", "tailscale", "Tailscale is not on the Proxmox host yet", deploy)]
    if not tail.get("running"):
        return [_item("problem", "tailscale", f"tailscaled does not answer on the Proxmox host: {tail['why']}", deploy)]
    backend = tail.get("backend")
    if backend == "NeedsLogin":
        out.append(
            _item(
                "todo",
                "tailscale",
                "the Proxmox host is not logged in to the tailnet",
                commands.get("tailscale_login", ""),
            )
        )
    elif backend == "Stopped":
        out.append(_item("problem", "tailscale", "Tailscale is stopped on the Proxmox host (tailscale down)", deploy))
    elif backend != "Running":
        out.append(
            _item("problem", "tailscale", f"Tailscale's backend on the Proxmox host is {backend or 'unknown'}", deploy)
        )
    guard = tail.get("guard") or {}
    if not guard.get("loaded"):
        out.append(
            _item(
                "problem", "tailscale", "the tailnet guard's table is not loaded, so tailscaled runs unguarded", deploy
            )
        )
    elif sorted(guard.get("routes") or []) != sorted(tail.get("wanted_routes") or []) or sorted(
        guard.get("guarded") or []
    ) != sorted(set(tail.get("guard_hosts") or [])):
        out.append(
            _item(
                "todo", "tailscale", "the tailnet guard on the host is not the one this configuration describes", deploy
            )
        )
    advertised = [r["route"] for r in tail.get("routes") or [] if r["advertised"]]
    if advertised and tail.get("forwarding") is False:
        out.append(
            _item(
                "problem",
                "tailscale",
                f"the host forwards no IPv4, so {_and(advertised)} lead{'s' if len(advertised) == 1 else ''} nowhere",
                deploy,
            )
        )
    if tail.get("differ"):
        out.append(
            _item("todo", "tailscale", f"the host's Tailscale preferences differ: {', '.join(tail['differ'])}", deploy)
        )
    if backend == "Running":
        for item in tail.get("console") or []:
            if item["kind"] != "health":
                out.append(_item("todo", "tailscale", item["text"]))
    reach = tail.get("reach") or {}
    if reach.get("split_dns") == []:
        text = (
            f"the tailnet has no split DNS for {reach['domain']}: away from home, its clients do not resolve the apps"
        )
        out.append(_item("todo", "tailscale", text, commands.get("split_dns", "")))
    return out


def _version(text):
    return str(text or "").removeprefix("v")


def _cluster_attention(clu, commands):
    out = []
    for node in clu.get("nodes") or []:
        if not node["ready"]:
            out.append(_item("problem", node["name"], "the node is not Ready"))
        for pressure in node["pressure"]:
            out.append(_item("problem", node["name"], f"the node reports {pressure}"))
    etcd = clu.get("etcd")
    if etcd and (etcd["state"] != "Running" or etcd["health"] != "OK"):
        out.append(
            _item("problem", "etcd", f"etcd is {etcd['state'] or 'unknown'}, its health {etcd['health'] or '?'}")
        )
    flux = clu.get("flux")
    if flux and not flux["ready"]:
        out.append(_item("problem", "flux", f"the FluxInstance is not ready: {flux['message'] or 'no reason given'}"))
    for kind in ("kustomizations", "helmreleases"):
        for failing in (clu.get(kind) or {}).get("failing") or []:
            out.append(_item("problem", "flux", f"{failing['name']} is not ready: {failing['message']}"))
    pins, nodes = clu.get("pins") or {}, clu.get("nodes") or []
    talos = sorted({n["talos"] for n in nodes if n["talos"]})
    kubelet = sorted({_version(n["kubelet"]) for n in nodes if n["kubelet"]})
    behind = []
    if pins.get("talos") and talos and talos != [pins["talos"]]:
        behind.append(f"Talos {', '.join(talos)} where the pins say {pins['talos']}")
    if pins.get("kubernetes") and kubelet and kubelet != [_version(pins["kubernetes"])]:
        behind.append(f"Kubernetes {', '.join(kubelet)} where the pins say {_version(pins['kubernetes'])}")
    if behind:
        out.append(_item("todo", "talos", f"the nodes run {' and '.join(behind)}", commands.get("talos_upgrade", "")))
    for balancer in clu.get("balancers") or []:
        if not balancer["address"]:
            name = f"{balancer['namespace']}/{balancer['name']}"
            out.append(
                _item("problem", balancer["name"], f"the LoadBalancer {name} has no address: MetalLB gave it none")
            )
    for cert in clu.get("certificates") or []:
        if not cert["ready"]:
            out.append(_item("problem", "tls", f"{cert['name']} is not ready: {cert['message'] or 'no reason given'}"))
        elif cert["days_left"] is not None and cert["days_left"] < CERT_PROBLEM_DAYS:
            out.append(_item("problem", "tls", f"{cert['name']} expires in {cert['days_left']} d and was not renewed"))
    access = clu.get("access") or {}
    for which in ("talosconfig", "kubeconfig"):
        days = access.get(f"{which}_days")
        if days is None:
            continue
        if days < 0:
            out.append(
                _item(
                    "problem",
                    "access",
                    f"the {which}'s client certificate has expired",
                    commands.get("credentials", ""),
                )
            )
        elif days < RENEW_DAYS:
            text = f"the {which}'s client certificate expires in {days} d"
            out.append(_item("todo", "access", text, commands.get("credentials", "")))
    alerts = {}
    for alert in clu.get("alerts") or []:
        alerts.setdefault((alert["name"], alert["severity"]), []).append(alert)
    for (name, severity), group in alerts.items():
        subjects = _and(sorted({a["subject"] for a in group if a["subject"]})) or "the cluster"
        level = "problem" if severity == "critical" else "todo"
        out.append(_item(level, "alert", f"{name} ({severity}) fires for {subjects}"))
    return out


def _with_guest_view(prox, clu):
    """The cluster's nodes with the guest's own memory and filesystems, from kubelet, in place of Proxmox's view."""
    if not prox or prox.get("unread") or not clu or clu.get("unread"):
        return prox
    nodes = {n["name"]: n for n in clu.get("nodes") or []}
    vms = []
    for vm in prox.get("vms") or []:
        node = nodes.get(vm.get("node"))
        if node and vm["status"] == "running":
            vm = dict(vm)
            if node.get("memory"):
                vm["memory"] = node["memory"] | {"view": "guest"}
            if node.get("filesystems"):
                vm["filesystems"] = [{k: f[k] for k in ("mount", "used", "total")} for f in node["filesystems"]]
        vms.append(vm)
    return prox | {"vms": vms}


def _volumes(clu):
    """The nodes' own volumes (Talos user volumes), as storages: every filesystem kubelet reports but /var."""
    out = []
    for node in (clu or {}).get("nodes") or []:
        for fs in node.get("filesystems") or []:
            if fs["mount"] != "/var":
                out.append(
                    {
                        "id": f"{node['name']} {fs['mount']}",
                        "type": "talos",
                        "status": "available",
                        "used": fs["used"],
                        "total": fs["total"],
                        "budget": None,
                        "claims": fs.get("claims") or [],
                    }
                )
    return out


def _with_deployed(prox, deploy, ancestry, commands):
    """The Proxmox host's findings with each stamp placed against this checkout's commit (deployed): the host's,
    deployed by commands' deploy_host, and each of this repo's VMs', by what makes it. A VM another project made has
    no stamp of this repo's to place, and Notes that were not read (a section not shown) none to place either."""
    if not prox:
        return prox

    errors = list(prox.get("errors") or [])

    def place(entry, command, label):
        unread = entry.get("stamp_unread", "")
        found_deployed = deployed(entry["stamp"], deploy, ancestry, command, unread)
        if found_deployed["state"] == "unplaced":
            errors.append(f"where {label}'s version stamp lies against this checkout: {found_deployed['why']}")
        return entry | {"deployed": found_deployed}

    host = prox.get("host") or {}
    if "stamp" in host:
        host = place(host, commands.get("deploy_host", ""), "the host")
    vms = [
        place(vm, vm["made_by"], f"VM {vm['vmid']}") if "stamp" in vm and vm.get("made_by") else vm
        for vm in prox.get("vms") or []
    ]
    return prox | {"host": host, "vms": vms, "errors": errors}


def report(
    found,
    setup=None,
    types=(),
    read_at="",
    probes=None,
    admin=(),
    commands=None,
    now=0,
    tailnet_dns=None,
    deploy=None,
    ancestry=None,
):
    """The report: every source's findings, the attention list ranked (sources not read, problems, things to do),
    how many problems and things to do each section has, and the exit code. `found` is {source: findings}, as
    status_merge puts the hosts' together; a source without findings was never read. `types` narrows the sections;
    empty means every one. `probes` is what status_probe saw, `found`'s own probes when not given, `admin` the admin
    pages ({name, url, and tailscale_node for the page the Proxmox host serves itself}) to list, `commands` the
    commands to suggest by purpose (upgrade, resume with {names}, talos_upgrade, credentials, allowlist, secrets, nas,
    nas_release, nas_network, backup, backup_target, etcd_snapshot, tailscale, tailscale_login, split_dns,
    deploy_host), `now` seconds since the epoch, `tailnet_dns` what tailscale dns status --json said on this machine,
    if it runs Tailscale, `found`'s own when not given. `deploy` is this checkout's version, {commit, describe,
    date}, which each machine's version stamp is placed against, with what the reads stamp_reads names found
    (`ancestry`), the Proxmox host's own when not given."""
    shown, setup, commands = types_shown(types), dict(setup or {}), dict(commands or {})
    needed = sources(types)
    found = found or {}
    probes = found.get("probes") if probes is None else probes
    tailnet_dns = found.get("tailnet_dns") if tailnet_dns is None else tailnet_dns
    attention, unread = {t: [] for t in TYPES}, []
    prox = found.get("proxmox")
    if "proxmox" in needed and prox is None:
        unread.append(_item("unread", "proxmox", "the Proxmox host was not read"))
    elif "proxmox" in needed and prox.get("unread"):
        unread.append(_item("unread", "proxmox", f"the Proxmox host could not be read: {prox['unread']}"))
    checkout = {k: str((deploy or {}).get(k) or "") for k in ("commit", "describe", "date")}
    ancestry = (prox or {}).get("ancestry") if ancestry is None else ancestry
    prox = _with_deployed(prox or {}, checkout, ancestry, commands) if prox and not prox.get("unread") else prox or {}
    clu = found.get("cluster")
    if setup.get("cluster") and "cluster" in needed:
        stopped = [
            f"{vm['vmid']} {vm['name']}" for vm in prox.get("vms") or [] if vm.get("node") and vm["status"] != "running"
        ]
        if clu is None:
            unread.append(_item("unread", "cluster", "the cluster was not read"))
        elif clu.get("unread") and stopped:
            # Its VMs being stopped says why, and that is a thing to do already.
            clu = {"unread": clu["unread"], "stopped": stopped}
        elif clu.get("unread"):
            unread.append(_item("unread", "cluster", f"the cluster could not be read: {clu['unread']}"))
    clu = clu or {}
    readable = clu and not clu.get("unread")
    nas_found = found.get("nas")
    if setup.get("nas") and "nas" in needed:
        if nas_found is None:
            unread.append(_item("unread", "nas", "the NAS was not read"))
        elif nas_found.get("unread"):
            unread.append(_item("unread", "nas", f"the NAS could not be read: {nas_found['unread']}"))
    nas_found = nas_found or {}
    nas_read = nas_found and not nas_found.get("unread")
    if nas_read:
        nas_found = _nas_model(nas_found, now)
    rows = _app_rows(clu if readable else {}, prox, probes)
    dns = _dns(clu, probes) if readable else None
    tls = _tls(clu, probes, now) if readable else None
    prox_read = prox and not prox.get("unread")
    if "apps" in shown and readable:
        attention["apps"] += _apps_attention(rows, dns, clu, tls, commands, clu.get("kubeconfig", ""))
    if "vms" in shown and prox_read:
        attention["vms"] += _host_attention(prox, commands) + _vm_attention(prox) + _stamp_attention(prox, checkout)
    volumes = _volumes(clu) if readable else []
    if "storage" in shown:
        attention["storage"] += _storage_attention((prox.get("storages") or []) if prox_read else [])
        attention["storage"] += _storage_attention(volumes)
        for node in (clu.get("nodes") or []) if readable else []:
            for fs in node.get("filesystems") or []:
                if fs["mount"] == "/var":
                    attention["storage"] += _fill_attention(f"{node['name']} /var", fs["used"], fs["total"])
    backed_up = None
    if "backups" in shown and prox_read:
        backed_up = _backups_model(prox, found.get("etcd_snapshots"), now, commands)
        attention["backups"] += _backup_attention(backed_up, commands) if backed_up else []
    if "cluster" in shown and readable:
        attention["cluster"] += _cluster_attention(clu, commands)
    if "nas" in shown and nas_read:
        attention["nas"] += _nas_attention(nas_found, commands)
    tail = _tailscale_model(
        found.get("tailscale"),
        admin,
        _traefik(clu) if readable else "",
        _domain(rows),
        clu.get("allowlist") if readable else "",
        tailnet_dns,
    )
    if "tailscale" in shown and tail:
        attention["tailscale"] += _tailscale_attention(tail, commands)
    # In the order they were worked out, the Proxmox host's Tailscale last.
    flat = [a for t in (*(t for t in TYPES if t != "tailscale"), "tailscale") for a in attention[t]]
    ranked = unread + [a for a in flat if a["level"] == "problem"] + [a for a in flat if a["level"] == "todo"]
    problems = sum(1 for a in ranked if a["level"] == "problem")
    return {
        "read_at": read_at,
        "setup": setup,
        "checkout": checkout,
        "types": shown,
        "exit": 2 if unread else 1 if problems else 0,
        "attention": ranked,
        "counts": {
            t: {level: sum(1 for a in attention[t] if a["level"] == level) for level in ("problem", "todo")}
            for t in shown
        },
        "apps": rows,
        "traefik": _traefik(clu) if readable else "",
        "dns": dns,
        "tls": tls,
        "admin": _admin_pages(admin, tail),
        "proxmox": _with_guest_view(
            {k: v for k, v in prox.items() if k not in ("backups", "ancestry")}, clu if readable else None
        )
        or {},
        "volumes": volumes,
        "backups": backed_up,
        "cluster": clu,
        "nas": nas_found,
        "tailscale": tail,
    }


# -------------------------------------------------------------------------------------------- the text ---

STYLES = {"b": "1", "d": "90", "k": "37", "h": "1;34", "g": "32", "y": "33", "r": "31", "c": "36", "u": "36"}
WIDTH = 100
# How each app state is drawn: its dot, and the style of the dot and the word.
STATE_STYLES = {
    "running": ("●", "g"),
    "starting": ("●", "y"),
    "waiting": ("●", "y"),
    "failing": ("●", "r"),
    "not applied": ("●", "r"),
    "missing": ("○", "r"),
    "suspended": ("○", "d"),
    "stopped": ("○", "d"),
}


class _Line:
    """A line of text in styled segments, padded on the text alone so that columns line up with or without colour.
    A URL (style u) is also a link a terminal can open, in colour (OSC 8)."""

    def __init__(self):
        self.parts = []

    def add(self, text, style=""):
        self.parts.append((str(text), style))
        return self

    def cell(self, text, width, style="", right=False):
        text = str(text)
        pad = " " * max(width - len(text), 0)
        return self.add(pad).add(text, style) if right else self.add(text, style).add(pad)

    def to(self, column):
        return self.add(" " * max(column - len(self), 0))

    def __len__(self):
        return sum(len(t) for t, _ in self.parts)

    def text(self, color):
        out = []
        for t, s in self.parts:
            if not (color and s and t.strip()):
                out.append(t)
            elif s == "u":
                out.append(f"\033]8;;{t}\033\\\033[{STYLES[s]}m{t}\033[0m\033]8;;\033\\")
            else:
                out.append(f"\033[{STYLES[s]}m{t}\033[0m")
        return "".join(out).rstrip()


def _setup_text(setup):
    parts = ["NAS" if setup.get("nas") else "Proxmox only"]
    if setup.get("cluster"):
        parts.append("cluster")
    if setup.get("home_assistant"):
        parts.append("Home Assistant")
    return " + ".join(parts)


def _uptime(seconds):
    days, rest = divmod(int(seconds or 0), 86400)
    hours, minutes = rest // 3600, rest % 3600 // 60
    if days >= 7:
        return f"{days} d"
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} m"
    return f"{minutes} m"


def _spark(values):
    """A sparkline of fractions from 0 to 1, a blank where there is no data, and the bucket that stands out."""
    out = []
    for v in values or []:
        if v is None:
            out.append((" ", ""))
        else:
            level = min(len(SPARK) - 1, int(max(v, 0) * len(SPARK)))
            out.append((SPARK[level], "y" if v >= 0.75 else "c"))
    return out


def _bar(fraction, cells=12):
    full = min(cells, round(max(fraction, 0) * cells))
    return "█" * full + "░" * (cells - full)


def _bar_style(fraction):
    return "r" if fraction >= STORAGE_PROBLEM else "y" if fraction >= STORAGE_TODO else "c"


def _wrap(text, width):
    """A message in lines that fit, never breaking a word, such as a path, in two."""
    return textwrap.wrap(str(text), width, break_long_words=False, break_on_hyphens=False) or [""]


def _phrases(key, phrases, indent=1, width=11):
    """A key and its phrases (each a list of (text, style)), joined by " · " and wrapped between phrases."""
    lines = [_Line().add(" " * indent).cell(key, width, "k")]
    for i, phrase in enumerate(p for p in phrases if p):
        size = sum(len(t) for t, _ in phrase)
        if i and len(lines[-1]) + 3 + size > WIDTH:
            lines.append(_Line().add(" " * (indent + width)))
        elif i:
            lines[-1].add(" · ", "d")
        for text, style in phrase:
            lines[-1].add(text, style)
    return lines


def _attention_lines(model):
    lines = []
    problems = sum(1 for a in model["attention"] if a["level"] in ("problem", "unread"))
    todos = sum(1 for a in model["attention"] if a["level"] == "todo")
    head = _Line().add("ATTENTION", "h").add("  ")
    head.add(f"{problems} problem{'s' if problems != 1 else ''}", "r" if problems else "d")
    head.add(" · ", "d").add(f"{todos} to do", "y" if todos else "d")
    lines.append(head)
    for a in model["attention"]:
        line = _Line().add(" ").add("●", "y" if a["level"] == "todo" else "r").add(" ").cell(a["subject"], 12, "b")
        for i, part in enumerate(_wrap(a["text"], WIDTH - 15)):
            lines.append((line if i == 0 else _Line().add(" " * 15)).add(part))
        for i, part in enumerate(_wrap(a["command"], WIDTH - 17) if a["command"] else []):
            lines.append(_Line().add(" " * 15).add("→ " if i == 0 else "  ", "d").add(part, "c"))
    return lines


def _http_style(code):
    if code is None:
        return "d"
    if code >= 500:
        return "r"
    if code in (403, 404):
        return "y"
    return "g"


def _domain(rows):
    """The domain the apps' names share, for the section's first line."""
    hosts = [_host_of(u["url"]) for r in rows if r.get("deployment") for u in r["urls"] if u["url"].startswith("https")]
    if not hosts:
        return ""
    split = [h.split(".") for h in hosts]
    common = []
    for labels in zip(*(list(reversed(s)) for s in split), strict=False):
        if len(set(labels)) != 1:
            break
        common.insert(0, labels[0])
    return ".".join(common)


def _apps_lines(model):
    rows, dns, clu = model["apps"], model.get("dns"), model.get("cluster") or {}
    head = _Line().add("APPS", "h")
    facts = []
    domain, traefik = _domain(rows), model.get("traefik")
    if domain:
        facts.append([(f"https://<app>.{domain}" + (f" → Traefik {traefik}" if traefik else ""), "d")])
    if dns:
        facts.append([("DNS ", "d"), (dns["state"], "g" if dns["state"] == "ok" else "y")])
    if clu.get("allowlist"):
        facts.append([("allowlist ", "d"), (clu["allowlist"], "y" if _closed(clu["allowlist"]) else "d")])
    if clu.get("stopped"):
        facts = [[(f"not read: {_and(clu['stopped'])} {'is' if len(clu['stopped']) == 1 else 'are'} stopped", "d")]]
    elif clu.get("unread"):
        facts = [[("the cluster could not be read", "d")]]
    for i, fact in enumerate(facts):
        head.add("  " if i == 0 else " · ", "d")
        for text, style in fact:
            head.add(text, style)
    lines = [head]
    if not rows:
        return lines
    name_width = max([len(r["name"]) for r in rows] + [13]) + 2
    group_width = max([len(r["group"]) for r in rows] + [6]) + 2
    url_width = max([len(u["url"]) for r in rows for u in r["urls"]] + [20]) + 2
    columns = [("APP", name_width), ("GROUP", group_width), ("STATE", 14), ("VERSION", 10), ("URL", url_width)]
    header = _Line().add(" ")
    for title, width in columns:
        header.cell(title, width, "k")
    lines.append(header.add("HTTP", "k"))
    url_column = 1 + sum(width for _, width in columns[:4])

    def state_cells(line, state, version):
        dot, style = STATE_STYLES.get(state, ("●", "y"))
        line.add(dot, style).add(" ").cell(state, 12, "" if state == "running" else style).cell(version or "", 10)

    def url_cells(line, url):
        line.to(url_column).cell(url["url"], url_width, "u")
        if url["http"] is not None:
            line.add(url["http"], _http_style(url["http"]))
        elif url["error"]:
            line.add("no answer", "r")
        else:
            line.add("—", "d")

    for row in rows:
        line = _Line().add(" ").cell(row["name"], name_width, "b").cell(row["group"], group_width, "d")
        state_cells(line, row["state"], row["version"])
        if row["urls"]:
            url_cells(line, row["urls"][0])
        elif row["state"] == "not applied":
            line.to(url_column).add(_wrap(row["detail"], WIDTH - url_column)[0], "d")
        lines.append(line)
        for url in row["urls"][1:]:
            line = _Line()
            url_cells(line, url)
            lines.append(line)
        for db in row["databases"]:
            line = _Line().add("   ").cell("database", name_width - 2, "d").cell("", group_width)
            state_cells(line, db["state"], db["version"])
            lines.append(line)
    if "tailscale" in model["types"]:
        lines += _reach_line(model)
    if dns and dns["missing"]:
        lines.append(_Line())
        lines.append(_Line().add(" Until DNS has them, these lines in /etc/hosts reach the apps from here:", "d"))
        prefix = f"   {dns['address']}  "
        for part in _wrap(" ".join(dns["missing"]), WIDTH - len(prefix)):
            lines.append(_Line().add(prefix + part, "c"))
    return lines


def _admin_lines(model):
    pages = model["admin"]
    width = max([len(a.get("name", "")) for a in pages] + [14]) + 3
    tailnet = any("tailnet" in a for a in pages)
    url_width = max([len(a.get("url", "")) for a in pages] + [3]) + 3
    head = _Line().add("ADMIN", "h")
    if tailnet:
        head.to(width + 1).cell("LAN", url_width, "k").add("TAILNET", "k")
    lines = [head]
    for page in pages:
        line = _Line().add(" ").cell(page.get("name", ""), width, "b")
        if not tailnet:
            lines.append(line.add(page.get("url", ""), "u"))
            continue
        there = page.get("tailnet", "")
        line.cell(page.get("url", ""), url_width, "u").add(there, "u" if "://" in there else "d")
        lines.append(line)
    return lines


TAILSCALE_BACKENDS = {"Running": ("running", "g"), "NeedsLogin": ("needs login", "y"), "Stopped": ("stopped", "r")}


def _on_off(value):
    return "on" if value is True else "off" if value is False else str(value)


def _tailscale_lines(model):
    tail = model["tailscale"]
    head = _Line().add("TAILSCALE", "h").add("  ")
    if tail.get("unread"):
        return [head.add("not read: the Proxmox host did not answer", "d")]
    role = (
        "subnet router on the Proxmox host" if tail.get("wanted_routes") else "a device of its own on the Proxmox host"
    )
    head.add(role, "d")
    if not tail.get("installed"):
        return [head.add(" · ", "d").add("not installed", "y")]
    if not tail.get("running"):
        return [head.add(" · ", "d").add("● ", "r").add("not running", "r"), *_phrases("Why", [[(tail["why"], "d")]])]
    word, style = TAILSCALE_BACKENDS.get(tail.get("backend"), (str(tail.get("backend") or "unknown").lower(), "r"))
    head.add(f" · {tail.get('version') or '?'} · ", "d").add("● ", style).add(word, style)
    lines = [head]
    ipv4 = [a for a in tail.get("addresses") or [] if ":" not in a]
    expiry = f"key expires {tail['key_expiry']}" if tail.get("key_expiry") else "key expiry off"
    node = [[(tail.get("dns_name") or "no name yet", "b")], [(", ".join(ipv4[:1]), "")]]
    node += [[(", ".join(tail.get("tags") or []) or "no tags", "" if tail.get("tags") else "y")], [(expiry, "d")]]
    lines += _phrases("Node", node)
    routes = []
    for r in tail.get("routes") or []:
        if r["served"]:
            routes.append([(r["route"], "b"), (" advertised, served by this host", "g")])
        elif r["advertised"] and r["wanted"]:
            routes.append([(r["route"], "b"), (" advertised, not served by this host", "y")])
        elif r["advertised"]:
            routes.append([(r["route"], "b"), (" advertised, not in this configuration", "y")])
        else:
            routes.append([(r["route"], "b"), (" not advertised", "y")])
    lines += _phrases("Routes", routes or [[("none, so nothing is forwarded from the tailnet", "d")]])
    guard = tail.get("guard") or {}
    if not guard.get("loaded"):
        phrases = [[("not loaded", "r")]]
    else:
        phrases = [[("loaded", "g")]]
        allow = guard.get("allow") or {}
        for host in guard.get("guarded") or []:
            phrases.append(
                [(f"guards {host} ", ""), (f"({', '.join(allow[host])} only)" if host in allow else "(all)", "d")]
            )
    if tail.get("forwarding") is not None:
        phrases.append([("forwarding ", "d"), (_on_off(tail["forwarding"]), "")])
    lines += _phrases("Guard", phrases)
    if tail.get("prefs_read"):
        prefs = [[(f"{k} {_on_off(v)}", "")] for k, v in (tail.get("settings") or {}).items()]
        differ = tail.get("differ") or []
        prefs.append([(f"differs: {', '.join(differ)}", "y")] if differ else [("as configured", "d")])
        lines += _phrases("Prefs", prefs)
    if tail.get("serve"):
        lines += _phrases("Serve", [[("configured: it connects from the host itself, past the guard", "y")]])
    health = tail.get("health") or []
    for i, warning in enumerate(health or ["no warnings"]):
        for j, part in enumerate(_wrap(warning, WIDTH - 12)):
            key = "Health" if i == 0 and j == 0 else ""
            lines.append(_Line().add(" ").cell(key, 11, "k").add(part, "y" if health else "d"))
    return lines


def _reach_line(model):
    """How the apps answer from the tailnet, under the apps."""
    tail = model.get("tailscale") or {}
    reach = tail.get("reach")
    if not reach:
        return []
    state = reach["state"]
    if state == "no route":
        phrases = [[("no route is advertised, so from the tailnet only the Proxmox host answers", "d")]]
    elif state == "outside":
        phrases = [[(f"Traefik's address {reach['address']} is in no advertised route", "y")]]
    elif state == "not served":
        phrases = [[(f"through {reach['route']}, which this host does not serve", "y")]]
    else:
        phrases = [[(f"the same URLs, through {reach['route']}", "")]]
        if reach.get("source"):
            allowed = reach.get("allowed")
            inside = [(", inside the allowlist", "d")] if allowed else [(", outside the allowlist", "y")]
            phrases.append([(f"clients arrive as {reach['source']}", "d"), *(inside if allowed is not None else [])])
        else:
            phrases.append([("clients keep their tailnet addresses", "d")])
    dns = reach.get("split_dns")
    if dns:
        phrases.append([(f"split DNS sends {reach['domain']} to {', '.join(dns)}", "d")])
    elif dns == []:
        phrases.append([(f"no split DNS for {reach['domain']}", "y")])
    return _phrases("Tailnet", phrases)


# How each state of a version stamp is said after where it came from, and in which style.
DEPLOYED_STATES = {
    "current": ("this checkout's", "g"),
    "behind": ("behind this checkout", "y"),
    "ahead": ("ahead of this checkout", "d"),
    "elsewhere": ("from another branch", "d"),
    "unknown": ("not in this checkout: git fetch", "d"),
    "unplaced": ("not placed against this checkout", "d"),
    "dirty": ("from uncommitted files", "y"),
    # The state current, from a checkout with uncommitted changes.
    "current-dirty": ("this commit, without the edits here", "y"),
    "": ("", ""),
}
# Where a stamp's line starts: under the machine's name.
DEPLOYED_INDENT = 9


def _deployed_lines(found_deployed):
    """A machine's version stamp, under its name: where its last deploy came from, the commit's date, and how that
    stands against this checkout."""
    if not found_deployed:
        return []
    line = _Line().add(" " * DEPLOYED_INDENT)
    if found_deployed["state"] == "never":
        return [line.add("no version stamp yet (a whole just deploy leaves one)", "d")]
    if found_deployed["state"] == "unread":
        return [line.add("version stamp not read", "y")]
    if found_deployed["state"] == "broken":
        return [line.add("version stamp broken: mend its Notes by hand", "y")]
    committed = f" ({found_deployed['date']})" if found_deployed.get("date") else ""
    line.add(f"deployed from {found_deployed['describe']}{committed}", "d")
    state = "current-dirty" if found_deployed.get("checkout_dirty") else found_deployed["state"]
    said, style = DEPLOYED_STATES.get(state, ("", ""))
    if said:
        line.add(" · ", "d").add(said, style)
    return [line]


def _vm_lines(found):
    lines = []
    host = found["host"]
    facts = [
        found["node"],
        f"Proxmox VE {found['version']}" if found["version"] else "",
        f"kernel {found['kernel']}" if found["kernel"] else "",
        f"up {_uptime(host['uptime'])}",
    ]
    lines.append(_Line().add("VMS", "h").add("  ").add(" · ".join(p for p in facts if p), "d"))
    vms = found.get("vms") or []
    name_width = max([len(vm["name"]) for vm in vms] + [16]) + 2
    columns = [("VMID", 6), ("NAME", name_width + 2), ("UP", 9)]
    header = _Line().add(" ")
    for title, width in columns:
        header.cell(title, width, "k")
    header.cell("CPU", 4, "k", right=True).add("  ").cell("LAST 24 H", 14, "k").cell("MEM GiB", 13, "k")
    lines.append(header.add("DISKS GiB, used / size", "k"))
    disks_column = 1 + 6 + name_width + 2 + 9 + 4 + 2 + 14 + 13

    def row(vmid, name, state, up, cpu, spark, memory, disks):
        line = _Line().add(" ").cell(vmid, 4, "d", right=True).add("  ")
        dot = {"running": ("●", "g"), "missing": ("○", "r")}.get(state, ("○", "d"))
        line.add(*dot).add(" ").cell(name, name_width, "b")
        if state != "running":
            return [line.add(state, "y" if state == "missing" else "d")]
        line.cell(up, 9).cell(cpu, 4, right=True).add("  ")
        for char, style in spark:
            line.add(char, style)
        line.add(" " * (14 - len(spark)))
        line.cell(memory, 13)
        out = [line]
        # Disks that do not fit go on lines of their own, under the column.
        for disk in disks:
            if len(out[-1]) > disks_column and len(out[-1]) + 3 + len(disk) > WIDTH:
                out.append(_Line().to(disks_column))
            elif len(out[-1]) > disks_column:
                out[-1].add(" · ", "d")
            out[-1].add(disk)
        return out

    memory = host["memory"]
    lines += row(
        "",
        "host",
        "running",
        _uptime(host["uptime"]),
        f"{host['cpu']:.0%}",
        _spark(host["cpu_24h"]),
        f"{_gib(memory['used'], 1):>4} / {_gib(memory['total'], 1)}",
        [f"root {_gib(host['root']['used'])} / {_gib(host['root']['total'])}"] if host["root"]["total"] else [],
    )
    lines += _deployed_lines(host.get("deployed"))
    host_view = False
    for vm in vms:
        mem = vm.get("memory") or {}
        marked = mem.get("view") == "host"
        host_view = host_view or (marked and vm["status"] == "running")
        disks = [f"{fs['mount']} {_gib(fs['used'])} / {_gib(fs['total'])}" for fs in vm.get("filesystems", [])]
        if not disks:
            disks = [f"guest agent: {vm['agent']}" if vm.get("agent") else "—"]
        lines += row(
            vm["vmid"],
            vm["name"],
            vm["status"],
            _uptime(vm.get("uptime")),
            f"{vm.get('cpu', 0):.0%}",
            _spark(vm.get("cpu_24h")),
            f"{_gib(mem.get('used', 0), 1):>4} / {_gib(mem.get('total', 0), 1)}{'*' if marked else ''}",
            disks,
        )
        if vm.get("os"):
            lines.append(_Line().add(" " * (7 + name_width + 2)).add(vm["os"], "d"))
        lines += _deployed_lines(vm.get("deployed"))
    other = found.get("others") or {}
    if other.get("vmids") or other.get("templates"):
        parts = []
        if other.get("vmids"):
            count = len(other["vmids"])
            parts.append(
                f"{count} other VM{'s' if count != 1 else ''} ({', '.join(map(str, other['vmids']))}), "
                f"{other['cores']} cores, {_gib(other['memory'], 1)} GiB, {other['running']} running"
            )
        if other.get("templates"):
            count = len(other["templates"])
            parts.append(f"{count} template{'s' if count != 1 else ''} ({', '.join(map(str, other['templates']))})")
        for i, part in enumerate(_wrap(" and ".join(parts) + ", not this repo's", WIDTH - 9)):
            lines.append(_Line().add(" " * 7).add("+ " if i == 0 else "  ", "k").add(part, "d"))
    if host_view:
        lines.append(
            _Line().add(
                " * the host's view: ballooning is off, so Proxmox counts all the memory the VM has touched", "d"
            )
        )
    return lines


def _storage_lines(storages):
    lines = [_Line().add("STORAGE", "h")]
    width = max([len(s["id"]) for s in storages] + [12]) + 2
    for s in storages:
        line = _Line().add(" ").cell(s["id"], width, "b").cell(SHORT_TYPES.get(s["type"], s["type"]), 8, "d")
        if s["status"] != "available":
            lines.append(line.add(s["status"] or "not available", "r"))
            continue
        fraction = s["used"] / s["total"] if s["total"] else 0
        line.add(_bar(fraction), _bar_style(fraction)).add(" ").cell(f"{fraction:.0%}", 4, right=True).add("  ")
        line.add(f"{_gib(s['used'])} / {_gib(s['total'])} GiB")
        budget = s.get("budget")
        if budget:
            over = budget["provisioned"] > budget["limit"]
            line.add(" · ", "d").add(
                f"disks {_gib(budget['provisioned'])} of {_gib(budget['limit'])} GiB budgeted", "y" if over else ""
            )
        elif s.get("claims"):
            count = len(s["claims"])
            line.add(" · ", "d").add(f"{count} volume{'s' if count != 1 else ''} in use", "d")
        lines.append(line)
    return lines


def _keep_text(keep):
    if keep.get("all"):
        return "keeps all"
    return "keeps " + ", ".join(f"{keep[k]} {k}" for k in KEEP_KINDS if keep.get(k)) if keep else ""


# How each backup state is drawn, and the words for it.
BACKUP_STATES = {
    "ok": ("ok", "g"),
    "failed": ("failed", "r"),
    "missed": ("missed", "r"),
    "none": ("none yet", "y"),
    "left out": ("left out", "y"),
    "missing": ("no VM", "d"),
    "unknown": ("not read", "d"),
}


def _backup_lines(model):
    found = model.get("backups") or {}
    jobs, vms = found.get("jobs") or [], found.get("vms") or []
    lines = []
    for i, job in enumerate(jobs):
        line = _Line().add("BACKUPS", "h").add("  ") if i == 0 else _Line().add(" " * 9)
        line.add(job["id"], "b").add(f" → {job['storage'] or job['wanted_storage']}", "d")
        if job["on_host"] is False:
            line.add(" · ", "d").add("not on the host", "y")
        elif job["on_host"] and not job["enabled"]:
            line.add(" · ", "d").add("disabled", "y")
        else:
            facts = [job["schedule"], _keep_text(job["keep"])]
            if found.get("running") is not None:
                facts.append(f"running for {_uptime(found['running'])}")
            elif job["next_in"] is not None:
                facts.append(f"next in {_uptime(job['next_in'])}" if job["next_in"] > 0 else "due now")
            line.add("".join(f" · {f}" for f in facts if f), "d")
        lines.append(line)
    if not jobs:
        names = _and(str(vm["vmid"]) for vm in vms)
        line = _Line().add("BACKUPS", "h").add("  ").add("no backup job: ", "y")
        lines.append(line.add(f"{names} {'is' if len(vms) == 1 else 'are'} not backed up", "d"))
    name_width = max([len(vm["name"]) for vm in vms] + [16]) + 2

    def row(vmid, name, ago, state, detail, detail_style=""):
        line = _Line().add(" ").cell(vmid, 4, "d", right=True).add("  ").cell(name, name_width, "b")
        line.cell(ago, 9, right=True).add("  ")
        word, style = BACKUP_STATES.get(state, (state, "d"))
        line.cell(word, 10, style)
        parts = _wrap(detail, WIDTH - len(line)) if detail else [""]
        out = [line.add(parts[0], detail_style)]
        out += [_Line().to(len(line) - len(parts[0])).add(part, detail_style) for part in parts[1:]]
        return out

    def kept(vm):
        return f"{_gib(vm['size'], 1)} GiB · {vm['kept']} kept" if vm["size"] is not None else ""

    for vm in vms if jobs else []:
        age = f"{_ago(vm['age'])} ago" if vm["age"] is not None else "—"
        state = vm["state"]
        if state == "failed":
            lines += row(vm["vmid"], vm["name"], f"{_ago(vm['failed_ago'])} ago", state, vm["failure"]["reason"], "r")
            if vm["age"] is not None:
                lines += row("", "", age, "ok", kept(vm))
        elif state == "missed":
            lines += row(vm["vmid"], vm["name"], age, state, f"none since the run due {_ago(vm['due_ago'])} ago")
        elif state == "missing":
            lines += row(vm["vmid"], vm["name"], age, state, "the VM is not on this host")
        else:
            lines += row(vm["vmid"], vm["name"], age, state, kept(vm))
    etcd = found.get("etcd")
    if etcd is not None:
        line = _Line().add(" " * 7).cell("etcd snapshot", name_width, "b")
        if etcd["count"]:
            line.cell(f"{_ago(etcd['age'])} ago", 9, right=True).add("  ").cell("", 10)
            line.add(f"{etcd['count']} in {etcd['dir']}", "d")
        else:
            line.cell("—", 9, right=True).add("  ").cell("none yet", 10, "y").add(f"in {etcd['dir']}", "d")
            if etcd["command"]:
                line.add("  → ", "d").add(etcd["command"], "c")
        lines.append(line)
    return lines


def _days(days):
    return "?" if days is None else f"{days} d"


def _cluster_lines(model):
    clu = model.get("cluster") or {}
    if clu.get("stopped"):
        text = f"not read: {_and(clu['stopped'])} {'is' if len(clu['stopped']) == 1 else 'are'} stopped"
        return [_Line().add("CLUSTER", "h").add("  ").add(text, "d")]
    if clu.get("unread"):
        return [_Line().add("CLUSTER", "h").add("  ").add("not read", "d")]
    nodes, flux = clu.get("nodes") or [], clu.get("flux")
    talos = sorted({n["talos"] for n in nodes if n["talos"]})
    kubelet = sorted({n["kubelet"] for n in nodes if n["kubelet"]})
    facts = [f"Talos {', '.join(talos)}" if talos else "", f"Kubernetes {', '.join(kubelet)}" if kubelet else ""]
    facts.append(f"Flux {flux['version']}" if flux and flux["version"] else "")
    lines = [_Line().add("CLUSTER", "h").add("  ").add(" · ".join(f for f in facts if f), "d")]

    phrases = [
        [
            ("●" if n["ready"] else "○", "g" if n["ready"] else "r"),
            (f" {n['name']} ", ""),
            ("Ready" if n["ready"] else "not Ready", "" if n["ready"] else "r"),
        ]
        for n in nodes
    ]
    etcd = clu.get("etcd")
    if etcd:
        healthy = etcd["state"] == "Running" and etcd["health"] == "OK"
        phrases.append(
            [("etcd ", ""), ("healthy" if healthy else f"{etcd['state']}, {etcd['health']}", "" if healthy else "r")]
        )
    lines += _phrases("Nodes", phrases)

    pins = clu.get("pins") or {}
    pin_phrases = []
    if pins.get("talos"):
        same = talos == [pins["talos"]]
        pin_phrases.append([(f"Talos {pins['talos']}", ""), (" as pinned" if same else " waits", "d" if same else "y")])
    if pins.get("kubernetes"):
        same = sorted({_version(k) for k in kubelet}) == [_version(pins["kubernetes"])]
        pin_phrases.append(
            [
                (f"Kubernetes {_version(pins['kubernetes'])}", ""),
                (" as pinned" if same else " waits", "d" if same else "y"),
            ]
        )
    if pin_phrases:
        lines += _phrases("Pins", pin_phrases)

    flux_phrases = []
    if flux:
        branch = flux["ref"].removeprefix("refs/heads/")
        applied = f"{branch}@{flux['applied'][:7]}" if flux["applied"] else branch
        if flux["remote"] is None:
            flux_phrases.append([(f"{applied} applied", "")])
        elif flux["remote"] == flux["applied"]:
            flux_phrases.append([(f"{applied}, the same as the branch", "")])
        else:
            flux_phrases.append([(f"{applied} applied; the branch is at {flux['remote'][:7]}", "y")])
    for kind in ("kustomizations", "helmreleases"):
        counts = clu.get(kind)
        if counts and counts["total"]:
            good = not counts["failing"]
            text = f"{counts['ready']} of {counts['total'] - len(counts['suspended'])} {counts['kind']} ready"
            phrase = [(text, "g" if good else "r")]
            if counts["suspended"]:
                phrase.append((f", {len(counts['suspended'])} suspended", "d"))
            flux_phrases.append(phrase)
    if flux_phrases:
        lines += _phrases("Flux", flux_phrases)

    balancers = clu.get("balancers") or []
    if balancers:
        lines += _phrases(
            "Addresses",
            [[(f"{b['name']} ", ""), (b["address"] or "none", "" if b["address"] else "r")] for b in balancers],
        )
    gpus = [(n["name"], g) for n in nodes for g in n.get("gpus") or []]
    for name, gpu in gpus:
        users = f" ({', '.join(gpu['used_by'])})" if gpu["used_by"] else ""
        text = f"{gpu['resource']} on {name}: {len(gpu['used_by'])} of {gpu['count']} in use{users}"
        lines += _phrases("GPU", [[(text, "")]])
    tls = model.get("tls")
    if tls:
        what = tls["certificate"] or f"a certificate from {tls['issuer']}"
        phrase = [(f"Traefik serves {what}, until {tls['not_after'][:10]} ({_days(tls['days_left'])})", "")]
        lines += _phrases(
            "TLS",
            [phrase] + ([[(f"cert-manager renews it from {tls['renewal'][:10]}", "d")]] if tls["renewal"] else []),
        )
    access = clu.get("access") or {}
    access_phrases = [
        [
            (
                f"{which} valid for {_days(access.get(which + '_days'))}",
                "y" if (access.get(which + "_days") or 99) < RENEW_DAYS else "",
            )
        ]
        for which in ("talosconfig", "kubeconfig")
        if access.get(which)
    ]
    if access_phrases:
        lines += _phrases("Access", access_phrases)
    alerts = clu.get("alerts")
    if alerts is not None:
        if not alerts:
            lines += _phrases("Alerts", [[("none firing", "d")]])
        else:
            lines += _phrases(
                "Alerts",
                [
                    [(a["name"], "r" if a["severity"] == "critical" else "y"), (f" {a['subject']}".rstrip(), "")]
                    for a in alerts
                ],
            )
    return lines


def _since(seconds):
    return "?" if seconds is None else f"{_ago(seconds)} ago"


def _sizes(used, total):
    """Used and total bytes as "2.2 / 7.1 TiB", or in GiB below a TiB."""
    if total >= 2**40:
        return f"{used / 2**40:.1f} / {total / 2**40:.1f} TiB"
    return f"{_gib(used)} / {_gib(total)} GiB"


def _scan_text(pool):
    scan = pool.get("scan") or {}
    doing = {"SCRUB": "scrubbing", "RESILVER": "resilvering"}.get(scan.get("function"), "scanning")
    if scan.get("state") == "SCANNING":
        return f"{doing} {scan.get('percentage') or 0:.0f}%", "y"
    if not scan or scan.get("function") != "SCRUB" or scan.get("state") != "FINISHED":
        return "never scrubbed", "d"
    errors = scan.get("errors") or 0
    text = f"scrub {_since(pool.get('scanned_ago'))}, {errors} error{'s' if errors != 1 else ''}"
    return text, "r" if errors else "d"


def _nas_lines(model):
    found = model.get("nas") or {}
    if not found or found.get("unread"):
        return [_Line().add("NAS", "h").add("  ").add("not read", "d")]
    facts = [found["name"], f"TrueNAS {found['version']}" if found["version"] else ""]
    facts.append(f"up {_uptime(found['uptime'])}" if found.get("uptime") is not None else "")
    lines = [_Line().add("NAS", "h").add("  ").add(" · ".join(f for f in facts if f), "d")]
    pools = found.get("pools") or []
    width = max([len(p["name"]) for p in pools] + [10]) + 2
    for i, pool in enumerate(pools):
        line = _Line().add(" ").cell("Pools" if i == 0 else "", 11, "k").cell(pool["name"], width, "b")
        line.cell(pool["layout"], 8, "d")
        good = pool["healthy"] and pool["status"] == "ONLINE"
        line.cell((pool["status"] or "unknown").lower(), 9, "g" if good else "r")
        if pool["total"]:
            fraction = pool["used"] / pool["total"]
            line.add(_bar(fraction), _bar_style(fraction)).add(" ").cell(f"{fraction:.0%}", 4, right=True).add("  ")
            line.cell(_sizes(pool["used"], pool["total"]), 15)
        line.add(*_scan_text(pool))
        lines.append(line)
    datasets = {}
    for task in found.get("snapshots") or []:
        datasets.setdefault(task["dataset"], []).append(task)
    phrases = []
    for dataset, tasks in datasets.items():
        short = dataset.split("/", 1)[1] if "/" in dataset else dataset
        kinds = ", ".join(t["every"] + ("" if t["enabled"] else " (off)") for t in tasks)
        failed = [t for t in tasks if t["enabled"] and t["state"] == "ERROR"]
        ages = [t["ago"] for t in tasks if t["enabled"] and t.get("ago") is not None]
        if failed:
            outcome = (" failed", "r")
        elif ages:
            outcome = (f" {_since(min(ages))}", "d")
        else:
            outcome = (" not run yet", "d")
        phrases.append([(short, "b"), (f" {kinds}", ""), outcome])
    if phrases:
        lines += _phrases("Snapshots", phrases)
    nfs = found.get("nfs") or {}
    running = nfs.get("state") == "RUNNING"
    count = nfs.get("shares") or 0
    lines += _phrases(
        "NFS",
        [
            [((nfs.get("state") or "unknown").lower(), "g" if running else "r" if count else "d")],
            [(f"{count} share{'s' if count != 1 else ''}", "")],
        ],
    )
    alerts = found.get("alerts") or []
    if not alerts:
        lines += _phrases("Alerts", [[("none", "d")]])
    for i, alert in enumerate(alerts):
        style = "r" if alert["level"] in NAS_ALERT_PROBLEM else "y" if alert["level"] in NAS_ALERT_TODO else "d"
        text = alert["text"]
        if style == "d" and (first := FIRST_SENTENCE.match(text)):
            text = first.group(1)
        for j, part in enumerate(_wrap(text, WIDTH - 22)):
            line = _Line().add(" ").cell("Alerts" if i == 0 and j == 0 else "", 11, "k")
            lines.append(line.cell(alert["level"].lower() if j == 0 else "", 10, style).add(part))
    return lines


def _error_lines(errors):
    lines = []
    for error in errors:
        for i, part in enumerate(_wrap(f"not read: {error}", WIDTH - 3)):
            lines.append(_Line().add(" " if i == 0 else "   ").add(part, "d"))
    return [*lines, _Line()] if lines else []


def blocks(model, color=False):
    """The report's text in blocks, in order, each (name, lines), with ANSI colours when `color`: the head, each section
    shown that has anything to show, in TYPES' order, then the reads that failed (errors), what needs attention, last,
    where a terminal leaves it in view, and the exit line. just status's callback prints them one by one, each section
    as soon as the sources it is worked out from are read (section_sources), and text() joins them."""
    out = []
    head = _Line().add("homeserver", "b")
    if model.get("read_at"):
        head.add(f"  {model['read_at']}", "d")
    out.append(("head", [head.add(f" · {_setup_text(model.get('setup') or {})}", "d"), _Line()]))
    types = model["types"]
    prox = model.get("proxmox") or {}
    clu = model.get("cluster") or {}
    prox_read = prox and not prox.get("unread")
    storages = ((prox.get("storages") or []) if prox_read else []) + (model.get("volumes") or [])
    backed_up = model.get("backups") or {}
    sections = {
        "apps": (model.get("apps") or clu) and _apps_lines,
        "admin": model.get("admin") and _admin_lines,
        "tailscale": model.get("tailscale") and _tailscale_lines,
        "vms": prox_read and (lambda m: _vm_lines(prox)),
        "storage": storages and (lambda m: _storage_lines(storages)),
        "backups": (backed_up.get("jobs") or backed_up.get("vms")) and _backup_lines,
        "cluster": clu and _cluster_lines,
        "nas": (model.get("setup") or {}).get("nas") and _nas_lines,
    }
    for name in TYPES:
        if name in types and sections[name]:
            out.append((name, [*sections[name](model), _Line()]))
    errors = _error_lines((prox.get("errors") or []) + (clu.get("errors") or []))
    if errors:
        out.append(("errors", errors))
    if model["attention"]:
        out.append(("attention", [*_attention_lines(model), _Line()]))
    problems = sum(1 for a in model["attention"] if a["level"] == "problem")
    unread = sum(1 for a in model["attention"] if a["level"] == "unread")
    summary = (
        f"{unread} source{'s' if unread != 1 else ''} not read"
        if unread
        else f"{problems} problem{'s' if problems != 1 else ''}"
        if problems
        else "nothing is wrong"
    )
    foot = _Line().add(f"exit {model['exit']}", "r" if model["exit"] else "g")
    out.append(("exit", [foot.add(f" · {summary} · sections: {' '.join(TYPES)} · --json", "d")]))
    return [(name, [line.text(color) for line in lines]) for name, lines in out]


def text(model, color=False):
    """The report as the terminal shows it, with ANSI colours when `color`: its blocks, joined."""
    return "\n".join(line for _, lines in blocks(model, color) for line in lines) + "\n"


class FilterModule:
    def filters(self):
        return {
            "status_proxmox": proxmox,
            "status_stamp_reads": stamp_reads,
            "status_backup_failed_runs": backup_failed_runs,
            "status_cluster": cluster,
            "status_nas": nas,
            "status_credentials": credentials,
            "status_probe_targets": probe_targets,
            "status_unknown_types": unknown_types,
            "status_types_shown": types_shown,
            "status_sources": sources,
            "status_host_reads": host_reads,
            "status_merge": merge,
            "status_report": report,
            "status_text": text,
        }
