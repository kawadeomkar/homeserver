"""What make status reports, worked out from what roles/status read, and the report as text.

The role's tasks read each host through status_read, pass the results to status_proxmox here, and the report play
passes what every host found to status_report, then the report to status_text. Everything here is pure: it runs on
the controller, never connects anywhere, and is unit-tested in tests/unit/test_status_filters.py, with the shapes
captured from a Proxmox VE 9.2 host.

The report is a mapping that JSON=1 prints as it is: every source's findings, the attention list (problems, things
to do, and sources that could not be read, each with the command that deals with it), and the exit code make status
returns: 2 when a source could not be read, else 1 when there is a problem, else 0.
"""

import json
import re
import textwrap

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
# The sections make status knows, in the order it shows them.
TYPES = ("vms", "storage")


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


def pending_keys(entries):
    """The settings that wait for the VM's next start, from .../pending."""
    return sorted(e["key"] for e in entries or [] if isinstance(e, dict) and ("pending" in e or "delete" in e))


INST = re.compile(r"^Inst (\S+) (?:\[(\S+)\] )?\((\S+)")


def upgrades(text):
    """What apt-get -s dist-upgrade would upgrade or install, each {package, old, new}; old is "" for a new one."""
    out = []
    for line in (text or "").splitlines():
        found = INST.match(line)
        if found:
            out.append({"package": found.group(1), "old": found.group(2) or "", "new": found.group(3)})
    return out


def proxmox(found, vms, budget_storages=(), headroom_gib=0, reboot_file=""):
    """What the Proxmox host's reads say, as the report needs it.

    `found` is {unread, node, results}: why the host could not be read at all (else ""), the node's name, and
    status_read's results by name (resources, node, rrd-node, rrd-<vmid>, pending-<vmid>, fsinfo-<vmid>,
    osinfo-<vmid>, content-<storage>, updates, reboot). `vms` is this repo's VMs, each {vmid, name, made_by, start,
    guest_agent}; every other VM on the host is only counted. Storages in `budget_storages` show their disk budget.
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
        "errors": errors,
    }


# --------------------------------------------------------------------------------------------- the report ---


def _gib(value, decimals=None):
    """Bytes as GiB: one decimal under 10 (or always, with decimals=1), whole above."""
    gib = value / GIB
    if decimals is None:
        decimals = 1 if gib < 10 else 0
    return f"{gib:.{decimals}f}"


def _item(level, subject, text, command=""):
    return {"level": level, "subject": subject, "text": text, "command": command}


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
        names = " and ".join([", ".join(labels[:-1]), labels[-1]] if len(labels) > 1 else labels)
        out.append(_item("todo", "stopped", f"{names} {'is' if len(labels) == 1 else 'are'} stopped", start))
    return out


def _storage_attention(found):
    out = []
    for s in found.get("storages") or []:
        if s["status"] != "available":
            out.append(_item("problem", s["id"], f"the storage is {s['status'] or 'not available'}"))
            continue
        used = s["used"] / s["total"] if s["total"] else 0
        if used >= STORAGE_PROBLEM:
            out.append(_item("problem", s["id"], f"{used:.0%} full: {_gib(s['used'])} of {_gib(s['total'])} GiB"))
        elif used >= STORAGE_TODO:
            out.append(_item("todo", s["id"], f"{used:.0%} full: {_gib(s['used'])} of {_gib(s['total'])} GiB"))
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


def _host_attention(found):
    out = []
    if found.get("reboot"):
        out.append(
            _item(
                "todo",
                "reboot",
                "the host waits for a reboot an upgrade asked for",
                "reboot it at a quiet time",
            )
        )
    if found.get("upgrades"):
        count = len(found["upgrades"])
        out.append(
            _item("todo", "upgrades", f"{count} package upgrade{'s' if count != 1 else ''} wait", "make proxmox")
        )
    return out


def unknown_types(types):
    """The names in `types` that are no section of the report."""
    return [t for t in types or [] if t not in TYPES]


def types_shown(types):
    """The sections `types` asks for, in the report's order; none asked for means every one."""
    return [t for t in TYPES if not types or t in types]


def report(found, setup=None, types=(), read_at=""):
    """The report: every source's findings, the attention list ranked (sources not read, problems, things to
    do), and the exit code. `found` is {source: findings}; a source without findings was never read. `types` narrows
    the sections; empty means every one."""
    shown = types_shown(types)
    attention, unread = [], []
    prox = (found or {}).get("proxmox")
    if prox is None:
        unread.append(_item("unread", "proxmox", "the Proxmox host was not read"))
    elif prox.get("unread"):
        unread.append(_item("unread", "proxmox", f"the Proxmox host could not be read: {prox['unread']}"))
    else:
        if "vms" in shown:
            attention += _host_attention(prox) + _vm_attention(prox)
        if "storage" in shown:
            attention += _storage_attention(prox)
    ranked = unread + [a for a in attention if a["level"] == "problem"] + [a for a in attention if a["level"] == "todo"]
    problems = sum(1 for a in ranked if a["level"] == "problem")
    return {
        "read_at": read_at,
        "setup": dict(setup or {}),
        "types": shown,
        "exit": 2 if unread else 1 if problems else 0,
        "attention": ranked,
        "proxmox": prox or {},
    }


# -------------------------------------------------------------------------------------------- the text ---

STYLES = {"b": "1", "d": "90", "k": "37", "h": "1;34", "g": "32", "y": "33", "r": "31", "c": "36"}
WIDTH = 100


class _Line:
    """A line of text in styled segments, padded on the text alone so that columns line up with or without colour."""

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
        out = "".join(f"\033[{STYLES[s]}m{t}\033[0m" if color and s and t.strip() else t for t, s in self.parts)
        return out.rstrip()


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
        if a["command"]:
            lines.append(_Line().add(" " * 15).add("→ ", "d").add(a["command"], "c"))
    return lines


def _wrap(text, width):
    """A message in lines that fit, never breaking a word, such as a path, in two."""
    return textwrap.wrap(str(text), width, break_long_words=False, break_on_hyphens=False) or [""]


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

    def row(vmid, name, state, up, cpu, spark, memory, disks):
        line = _Line().add(" ").cell(vmid, 4, "d", right=True).add("  ")
        dot = {"running": ("●", "g"), "missing": ("○", "r")}.get(state, ("○", "d"))
        line.add(*dot).add(" ").cell(name, name_width, "b")
        if state != "running":
            return line.add(state, "y" if state == "missing" else "d")
        line.cell(up, 9).cell(cpu, 4, right=True).add("  ")
        for char, style in spark:
            line.add(char, style)
        line.add(" " * (14 - len(spark)))
        line.cell(memory, 13)
        return line.add(disks)

    memory = host["memory"]
    lines.append(
        row(
            "",
            "host",
            "running",
            _uptime(host["uptime"]),
            f"{host['cpu']:.0%}",
            _spark(host["cpu_24h"]),
            f"{_gib(memory['used'], 1):>4} / {_gib(memory['total'], 1)}",
            f"root {_gib(host['root']['used'])} / {_gib(host['root']['total'])}" if host["root"]["total"] else "",
        )
    )
    host_view = False
    for vm in vms:
        mem = vm.get("memory") or {}
        marked = mem.get("view") == "host"
        host_view = host_view or (marked and vm["status"] == "running")
        disks = " · ".join(
            f"{fs['mount']} {_gib(fs['used'])} / {_gib(fs['total'])}" for fs in vm.get("filesystems", [])
        )
        if not disks:
            disks = f"guest agent: {vm['agent']}" if vm.get("agent") else "—"
        lines.append(
            row(
                vm["vmid"],
                vm["name"],
                vm["status"],
                _uptime(vm.get("uptime")),
                f"{vm.get('cpu', 0):.0%}",
                _spark(vm.get("cpu_24h")),
                f"{_gib(mem.get('used', 0), 1):>4} / {_gib(mem.get('total', 0), 1)}{'*' if marked else ''}",
                disks,
            )
        )
        if vm.get("os"):
            lines.append(_Line().add(" " * (7 + name_width + 2)).add(vm["os"], "d"))
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
        lines.append(_Line().add(" " * 7).add("+ ", "k").add(" and ".join(parts) + ", not this repo's", "d"))
    if host_view:
        lines.append(
            _Line().add(
                " * the host's view: ballooning is off, so Proxmox counts all the memory the VM has touched", "d"
            )
        )
    return lines


def _storage_lines(found):
    lines = [_Line().add("STORAGE", "h")]
    storages = found.get("storages") or []
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
        lines.append(line)
    return lines


def text(model, color=False):
    """The report as the terminal shows it, with ANSI colours when `color`."""
    lines = []
    head = _Line().add("homeserver", "b")
    if model.get("read_at"):
        head.add(f"  {model['read_at']}", "d")
    lines += [head.add(f" · {_setup_text(model.get('setup') or {})}", "d"), _Line()]
    if model["attention"]:
        lines += [*_attention_lines(model), _Line()]
    prox = model.get("proxmox") or {}
    if prox and not prox.get("unread"):
        if "vms" in model["types"]:
            lines += [*_vm_lines(prox), _Line()]
        if "storage" in model["types"]:
            lines += [*_storage_lines(prox), _Line()]
        for error in prox.get("errors") or []:
            for i, part in enumerate(_wrap(f"not read: {error}", WIDTH - 3)):
                lines.append(_Line().add(" " if i == 0 else "   ").add(part, "d"))
        if prox.get("errors"):
            lines.append(_Line())
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
    lines.append(foot.add(f" · {summary} · make status TYPE={'|'.join(TYPES)} · JSON=1", "d"))
    return "\n".join(line.text(color) for line in lines) + "\n"


class FilterModule:
    def filters(self):
        return {
            "status_proxmox": proxmox,
            "status_unknown_types": unknown_types,
            "status_types_shown": types_shown,
            "status_report": report,
            "status_text": text,
        }
