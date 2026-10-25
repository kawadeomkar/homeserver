"""The judgments roles/proxmox_tailscale makes in Python, so that its own runs and just status say the same thing:

- proxmox_tailscale_differ: which of the node's preferences differ from the role's, which node.yml then sets;
- proxmox_tailscale_console: what only the tailnet's admin console, or the owner on the node, can fix, which node.yml
  reports on every run and just status lists;
- proxmox_tailscale_status: the node's Tailscale, its guard and its forwarding, for just status (tasks/status.yml).

tailscale status --json lists every device and login on the tailnet. Only the node's own entry is ever kept: no other
device, login or the tailnet's name (a login's address, for a personal tailnet) leaves these functions.
"""

import json
import re

# A set of the guard's table and its elements, as nft lists it and as the role writes it (one element a line, each
# followed by a comma). The lines before the elements hold no braces.
TABLE_SET = re.compile(r"set\s+(\w+)\s*\{[^{}]*?elements\s*=\s*\{([^{}]*)\}", re.S)


def _json(value):
    """A command's JSON output as a dict, {} for anything else (no output, an error, a list)."""
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _advertised(prefs):
    """advertise-routes, whether tailscale writes it as a list or a comma-separated string."""
    routes = prefs.get("advertise-routes") or ""
    routes = routes.split(",") if isinstance(routes, str) else routes
    return [str(r).strip() for r in routes if str(r).strip()]


def differ(prefs, routes, settings):
    """The preferences that differ from the role's, by tailscale set's flag name: advertise-routes compared as a set
    of routes, then every setting the role keeps. A setting tailscale does not know reads as different, and tailscale
    set then names it."""
    prefs = _json(prefs)
    out = []
    if sorted(_advertised(prefs)) != sorted(str(r) for r in routes or []):
        out.append("advertise-routes")
    return out + [key for key, value in (settings or {}).items() if key not in prefs or prefs[key] != value]


def console(status, serve, routes, tags, guard_hosts):
    """What only the admin console, or the owner on the node, can fix, each {kind, text}: routes the node advertises
    but does not serve, tags other than the role's, a node key that expires, a Serve configuration, and Tailscale's
    own health warnings. `status` and `serve` are tailscale status --json's and serve status --json's output."""
    status, serve = _json(status), _json(serve)
    running = status.get("BackendState") == "Running"
    me = status.get("Self") or {}
    primary = me.get("PrimaryRoutes") or []
    node_tags = me.get("Tags") or []
    tags = list(tags or [])
    out = []
    unapproved = [str(r) for r in routes or [] if str(r) not in primary]
    if running and unapproved:
        out.append(
            {
                "kind": "routes",
                "text": "Advertised but not approved, or served by another subnet router: "
                + ", ".join(unapproved)
                + ". Approve them in the admin console, or add the auto-approver from scripts/runbooks/tailscale.md.",
            }
        )
    if running and sorted(node_tags) != sorted(tags):
        out.append(
            {
                "kind": "tags",
                "text": f"The node is tagged {', '.join(node_tags) if node_tags else 'with nothing'}, not "
                f"{', '.join(tags)}. Tags change only by re-authenticating: edit them in the admin console (Machines, "
                "Edit ACL tags).",
            }
        )
    if running and me.get("KeyExpiry"):
        out.append(
            {
                "kind": "expiry",
                "text": f"The node key expires at {me['KeyExpiry']}, and remote access with it: disable key expiry for "
                "the machine in the admin console.",
            }
        )
    if serve:
        out.append(
            {
                "kind": "serve",
                "text": "Tailscale Serve or Funnel is configured on the node. It connects from the node itself, with "
                "the address the guarded hosts trust, which the guard cannot see: check that no target is one of "
                + (", ".join(guard_hosts or []) or "them")
                + ".",
            }
        )
    out += [{"kind": "health", "text": f"Tailscale reports: {warning}"} for warning in status.get("Health") or []]
    return out


def _table(text):
    """The guard's sets as loaded: the routes, the guarded hosts, and what each guarded host still allows."""
    sets = {
        name: [e.strip() for e in body.replace("\n", " ").split(",") if e.strip()]
        for name, body in TABLE_SET.findall(text or "")
    }
    allow = {}
    for entry in sets.get("guarded_allow", []):
        parts = [p.strip() for p in entry.split(" . ")]
        if len(parts) == 3:
            allow.setdefault(parts[0], []).append(f"{parts[1]}/{parts[2]}")
    return {"routes": sets.get("routes", []), "guarded": sets.get("guarded", []), "allow": allow}


def _ok(result):
    return isinstance(result, dict) and result.get("rc") == 0


def _why(result):
    """The first line of why a read failed."""
    result = result if isinstance(result, dict) else {}
    text = (result.get("stderr") or result.get("msg") or "").strip()
    return text.splitlines()[0] if text else "it failed"


def status(
    status_read,
    prefs_read=None,
    serve_read=None,
    table_read=None,
    forwarding_read=None,
    routes=(),
    tags=(),
    settings=None,
    guard_hosts=(),
    guard_allow=(),
):
    """The node as just status reports it, from tasks/status.yml's reads (each of tailscale_read's results, or the
    task's own result when the host did not answer): tailscale status --json, get --json, serve status --json, nft list
    table inet tailnet_guard and sysctl -n net.ipv4.ip_forward. Only the node's own entry of the status is kept."""
    routes = [str(r) for r in routes or []]
    out = {
        "wanted_routes": routes,
        "wanted_tags": list(tags or []),
        "guard_hosts": list(guard_hosts or []),
        "guard_allow": list(guard_allow or []),
    }
    read = status_read if isinstance(status_read, dict) else {}
    if read.get("unreachable"):
        return {**out, "unread": read.get("msg") or "it did not answer"}
    if not _ok(read):
        why = _why(read)
        installed = "No such file" not in why and read.get("rc") != 127
        return {**out, "installed": installed, "running": False, "why": why}
    state = _json(read.get("stdout"))
    me = state.get("Self") or {}
    prefs = _json(prefs_read.get("stdout")) if _ok(prefs_read) else {}
    advertised = _advertised(prefs)
    primary = me.get("PrimaryRoutes") or []
    table = table_read if isinstance(table_read, dict) else {}
    forwarding = (forwarding_read or {}).get("stdout", "").strip() if _ok(forwarding_read) else ""
    out.update(
        {
            "installed": True,
            "running": True,
            "backend": state.get("BackendState") or "",
            "version": (state.get("Version") or "").split("-")[0],
            "suffix": state.get("MagicDNSSuffix") or "",
            "dns_name": (me.get("DNSName") or "").rstrip("."),
            "addresses": list(me.get("TailscaleIPs") or []),
            "tags": list(me.get("Tags") or []),
            "key_expiry": me.get("KeyExpiry") or "",
            "routes": [
                {"route": r, "wanted": r in routes, "advertised": r in advertised, "served": r in primary}
                for r in sorted(set(routes) | set(advertised))
            ],
            "prefs_read": _ok(prefs_read),
            "differ": differ(prefs, routes, settings) if _ok(prefs_read) else [],
            "settings": {key: prefs.get(key) for key in settings or {}} if _ok(prefs_read) else {},
            "snat": prefs.get("snat-subnet-routes", True),
            "serve": bool(_json(serve_read.get("stdout"))) if _ok(serve_read) else False,
            "health": list(state.get("Health") or []),
            "guard": {"loaded": True, **_table(table.get("stdout"))} if _ok(table) else {"loaded": False},
            "forwarding": {"1": True, "0": False}.get(forwarding),
            "console": console(
                state, (serve_read or {}).get("stdout") if _ok(serve_read) else {}, routes, tags, guard_hosts
            ),
        }
    )
    return out


class FilterModule:
    def filters(self):
        return {
            "proxmox_tailscale_differ": differ,
            "proxmox_tailscale_console": console,
            "proxmox_tailscale_status": status,
        }
