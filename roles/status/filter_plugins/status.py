"""What make status reports, worked out from what roles/status read, and the report as text.

The role's tasks read each host through status_read and pass the results here: status_proxmox for the Proxmox host,
status_cluster for the cluster (with status_credentials for its credentials' expiry, read under no_log). The report
play asks the apps' addresses (status_probe_targets, then the module status_probe), passes what every host found and
what the probes saw to status_report, then the report to status_text. Everything here is pure: it runs on the
controller, never connects anywhere, and is unit-tested in tests/unit/test_status_filters.py, with the shapes
captured from a Proxmox VE 9.2 host and from the cluster on it.

The report is a mapping that JSON=1 prints as it is: every source's findings, the attention list (problems, things
to do, and sources that could not be read, each with the command that deals with it), and the exit code make status
returns: 2 when a source could not be read, else 1 when there is a problem, else 0. The commands it suggests are
configuration (status_commands), so the role names none of this repo's make targets itself.
"""

import base64
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
# The sections make status knows, in the order it shows them.
TYPES = ("apps", "admin", "vms", "storage", "cluster")
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
    osinfo-<vmid>, network-<vmid>, content-<storage>, updates, reboot). `vms` is this repo's VMs, each {vmid, name,
    made_by, start, guest_agent, node, app}: `node` the cluster node it is, `app` {name, url, port} the app it serves
    (its address from its guest agent when `url` is empty); every other VM on the host is only counted. Storages in
    `budget_storages` show their disk budget.
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
    """An app's state: running, starting, waiting (for a Secret), failing, suspended (by make suspend, which records
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
    Kustomizations apply the apps; `annotation` the one make suspend records an app's replicas in; `database_label`
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


def unknown_types(types):
    """The names in `types` that are no section of the report."""
    return [t for t in types or [] if t not in TYPES]


def types_shown(types):
    """The sections `types` asks for, in the report's order; none asked for means every one."""
    return [t for t in TYPES if not types or t in types]


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
                out.append(_item("todo", part["name"], "is scaled to zero, though not by make suspend"))
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


def report(found, setup=None, types=(), read_at="", probes=None, admin=(), commands=None, now=0):
    """The report: every source's findings, the attention list ranked (sources not read, problems, things to do),
    and the exit code. `found` is {source: findings}; a source without findings was never read. `types` narrows the
    sections; empty means every one. `probes` is what status_probe saw, `admin` the admin pages ({name, url}) to
    list, `commands` the commands to suggest by purpose (upgrade, resume with {names}, talos_upgrade, credentials,
    allowlist, secrets), `now` seconds since the epoch."""
    shown, setup, commands = types_shown(types), dict(setup or {}), dict(commands or {})
    found = found or {}
    attention, unread = [], []
    prox = found.get("proxmox")
    if prox is None:
        unread.append(_item("unread", "proxmox", "the Proxmox host was not read"))
    elif prox.get("unread"):
        unread.append(_item("unread", "proxmox", f"the Proxmox host could not be read: {prox['unread']}"))
    prox = prox or {}
    clu = found.get("cluster")
    if setup.get("cluster"):
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
    rows = _app_rows(clu if readable else {}, prox, probes)
    dns = _dns(clu, probes) if readable else None
    tls = _tls(clu, probes, now) if readable else None
    prox_read = prox and not prox.get("unread")
    if "apps" in shown and readable:
        attention += _apps_attention(rows, dns, clu, tls, commands, clu.get("kubeconfig", ""))
    if "vms" in shown and prox_read:
        attention += _host_attention(prox, commands) + _vm_attention(prox)
    volumes = _volumes(clu) if readable else []
    if "storage" in shown:
        attention += _storage_attention((prox.get("storages") or []) if prox_read else []) + _storage_attention(volumes)
        for node in (clu.get("nodes") or []) if readable else []:
            for fs in node.get("filesystems") or []:
                if fs["mount"] == "/var":
                    attention += _fill_attention(f"{node['name']} /var", fs["used"], fs["total"])
    if "cluster" in shown and readable:
        attention += _cluster_attention(clu, commands)
    ranked = unread + [a for a in attention if a["level"] == "problem"] + [a for a in attention if a["level"] == "todo"]
    problems = sum(1 for a in ranked if a["level"] == "problem")
    return {
        "read_at": read_at,
        "setup": setup,
        "types": shown,
        "exit": 2 if unread else 1 if problems else 0,
        "attention": ranked,
        "apps": rows,
        "traefik": _traefik(clu) if readable else "",
        "dns": dns,
        "tls": tls,
        "admin": [dict(a) for a in admin or []],
        "proxmox": _with_guest_view(prox, clu if readable else None) or {},
        "volumes": volumes,
        "cluster": clu,
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
    if dns and dns["missing"]:
        lines.append(_Line())
        lines.append(_Line().add(" Until DNS has them, these lines in /etc/hosts reach the apps from here:", "d"))
        prefix = f"   {dns['address']}  "
        for part in _wrap(" ".join(dns["missing"]), WIDTH - len(prefix)):
            lines.append(_Line().add(prefix + part, "c"))
    return lines


def _admin_lines(model):
    lines = [_Line().add("ADMIN", "h")]
    width = max([len(a.get("name", "")) for a in model["admin"]] + [14]) + 3
    for page in model["admin"]:
        lines.append(_Line().add(" ").cell(page.get("name", ""), width, "b").add(page.get("url", ""), "u"))
    return lines


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


def _error_lines(errors):
    lines = []
    for error in errors:
        for i, part in enumerate(_wrap(f"not read: {error}", WIDTH - 3)):
            lines.append(_Line().add(" " if i == 0 else "   ").add(part, "d"))
    return [*lines, _Line()] if lines else []


def text(model, color=False):
    """The report as the terminal shows it, with ANSI colours when `color`."""
    lines = []
    head = _Line().add("homeserver", "b")
    if model.get("read_at"):
        head.add(f"  {model['read_at']}", "d")
    lines += [head.add(f" · {_setup_text(model.get('setup') or {})}", "d"), _Line()]
    if model["attention"]:
        lines += [*_attention_lines(model), _Line()]
    types = model["types"]
    prox = model.get("proxmox") or {}
    clu = model.get("cluster") or {}
    if "apps" in types and (model.get("apps") or clu):
        lines += [*_apps_lines(model), _Line()]
    if "admin" in types and model.get("admin"):
        lines += [*_admin_lines(model), _Line()]
    prox_read = prox and not prox.get("unread")
    if "vms" in types and prox_read:
        lines += [*_vm_lines(prox), _Line()]
    storages = ((prox.get("storages") or []) if prox_read else []) + (model.get("volumes") or [])
    if "storage" in types and storages:
        lines += [*_storage_lines(storages), _Line()]
    if "cluster" in types and clu:
        lines += [*_cluster_lines(model), _Line()]
    lines += _error_lines((prox.get("errors") or []) + (clu.get("errors") or []))
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
            "status_cluster": cluster,
            "status_credentials": credentials,
            "status_probe_targets": probe_targets,
            "status_unknown_types": unknown_types,
            "status_types_shown": types_shown,
            "status_report": report,
            "status_text": text,
        }
