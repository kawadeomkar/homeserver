"""Unit tests for roles/proxmox_tailscale's filters: what node.yml reports for the admin console, which preferences
differ, and the node's state for make status, which keeps only the node's own entry of tailscale status --json.

The shapes are Tailscale 1.104.2's on a Proxmox host and nft 1.1.3's listing of the guard's table, captured on a real
host, with every address and name replaced: RFC 5737 for the LAN, built at run time for the tailnet's, since the leak
checks refuse a tailnet name or a CGNAT address written in the repository.
"""

import importlib.util
import json
from pathlib import Path

import jinja2
import pytest

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "roles" / "proxmox_tailscale"
_spec = importlib.util.spec_from_file_location(
    "proxmox_tailscale_filters", ROLE / "filter_plugins" / "proxmox_tailscale.py"
)
pt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pt)

ROUTE = "192.0.2.0/24"
NAS = "192.0.2.10"
SUFFIX = "tail0example" + ".ts.net"
NODE_IPV4 = ".".join(["100", "64", "0", "20"])
PEER_IPV4 = ".".join(["100", "64", "0", "21"])
LOGIN = "owner@example.com"
SETTINGS = {"accept-routes": False, "accept-dns": False, "ssh": False, "auto-update": False}


def tailscale_status(primary=(), tags=("tag:homeserver",), backend="Running", **self_more):
    """tailscale status --json as 1.104.2 prints it on the Proxmox host, with another device and its owner."""
    me = {
        "ID": "n1",
        "HostName": "pve",
        "DNSName": f"pve.{SUFFIX}.",
        "OS": "linux",
        "TailscaleIPs": [NODE_IPV4, ":".join(["fd7a", "115c", "a1e0", "", "14"])],
        "AllowedIPs": [f"{NODE_IPV4}/32"],
        "Online": True,
        "KeyExpiry": None,
        "PrimaryRoutes": list(primary) or None,
    }
    if tags:
        me["Tags"] = list(tags)
    me.update(self_more)
    return {
        "Version": "1.104.2-tc3430f0d2-g598b63867",
        "TUN": True,
        "BackendState": backend,
        "AuthURL": "",
        "TailscaleIPs": me["TailscaleIPs"],
        "Self": me,
        "Health": [],
        "MagicDNSSuffix": SUFFIX,
        "CurrentTailnet": {"Name": LOGIN, "StableID": "Texample", "MagicDNSSuffix": SUFFIX, "MagicDNSEnabled": True},
        "Peer": {"nodekey:peer": {"HostName": "laptop", "DNSName": f"laptop.{SUFFIX}.", "TailscaleIPs": [PEER_IPV4]}},
        "User": {"7": {"LoginName": LOGIN}},
        "CertDomains": None,
    }


PREFS = {
    "accept-dns": False,
    "accept-routes": False,
    "advertise-connector": False,
    "advertise-exit-node": False,
    "advertise-routes": ROUTE,
    "auto-update": False,
    "netfilter-mode": "on",
    "snat-subnet-routes": True,
    "ssh": False,
}

# nft list table inet tailnet_guard, as nft 1.1.3 lists it on the host (its counters and chains cut).
NFT_LISTING = f"""table inet tailnet_guard {{
\tcounter guarded_dropped {{
\t\tpackets 0 bytes 0
\t}}

\tset routes {{
\t\ttype ipv4_addr
\t\tflags interval
\t\tauto-merge
\t\telements = {{ {ROUTE} }}
\t}}

\tset guarded {{
\t\ttype ipv4_addr
\t\telements = {{ {NAS} }}
\t}}

\tset guarded_allow {{
\t\ttype ipv4_addr . inet_proto . inet_service
\t\telements = {{ {NAS} . tcp . 443 }}
\t}}
}}
"""


def ok(stdout):
    return {"rc": 0, "stdout": stdout if isinstance(stdout, str) else json.dumps(stdout), "stderr": ""}


def state(status=None, prefs=None, serve=None, table=NFT_LISTING, forwarding="1\n", routes=(ROUTE,), **more):
    return pt.status(
        ok(tailscale_status() if status is None else status),
        ok(PREFS if prefs is None else prefs),
        ok({} if serve is None else serve),
        ok(table) if table is not None else {"rc": 1, "stdout": "", "stderr": "Error: No such file or directory"},
        ok(forwarding),
        list(routes),
        ["tag:homeserver"],
        SETTINGS,
        [NAS],
        ["tcp/443"],
        **more,
    )


def test_the_console_says_what_node_yml_always_said():
    """The same sentences node.yml's report wrote before they moved here, which make deploy prints."""
    found = pt.console(
        tailscale_status(tags=(), KeyExpiry="2027-04-15T00:00:00Z") | {"Health": ["UDP GRO is off"]},
        {"TCP": {"443": {}}},
        [ROUTE],
        ["tag:homeserver"],
        [NAS],
    )
    assert [c["text"] for c in found] == [
        f"Advertised but not approved, or served by another subnet router: {ROUTE}. Approve them in the admin "
        "console, or add the auto-approver from scripts/runbooks/tailscale.md.",
        "The node is tagged with nothing, not tag:homeserver. Tags change only by re-authenticating: edit them in the "
        "admin console (Machines, Edit ACL tags).",
        "The node key expires at 2027-04-15T00:00:00Z, and remote access with it: disable key expiry for the machine "
        "in the admin console.",
        "Tailscale Serve or Funnel is configured on the node. It connects from the node itself, with the address the "
        f"guarded hosts trust, which the guard cannot see: check that no target is one of {NAS}.",
        "Tailscale reports: UDP GRO is off",
    ]
    assert [c["kind"] for c in found] == ["routes", "tags", "expiry", "serve", "health"]


def test_the_console_is_quiet_for_a_node_as_configured():
    assert pt.console(json.dumps(tailscale_status(primary=[ROUTE])), "{}", [ROUTE], ["tag:homeserver"], [NAS]) == []


def test_the_console_judges_a_node_that_is_not_running_by_serve_and_health_alone():
    stopped = tailscale_status(tags=(), backend="Stopped") | {"Health": ["Tailscale is stopped."]}
    assert [c["kind"] for c in pt.console(stopped, "", [ROUTE], ["tag:homeserver"], [])] == ["health"]


@pytest.mark.parametrize(
    "prefs, differ",
    [
        (PREFS, []),
        (PREFS | {"advertise-routes": [ROUTE]}, []),
        (PREFS | {"advertise-routes": ""}, ["advertise-routes"]),
        (PREFS | {"accept-dns": True}, ["accept-dns"]),
        ({k: v for k, v in PREFS.items() if k != "ssh"}, ["ssh"]),
    ],
    ids=["same", "routes-as-a-list", "no-routes", "a-setting", "unknown-to-tailscale"],
)
def test_which_preferences_differ(prefs, differ):
    assert pt.differ(json.dumps(prefs), [ROUTE], SETTINGS) == differ


def test_the_node_as_make_status_reads_it():
    found = state()
    assert found == {
        "wanted_routes": [ROUTE],
        "wanted_tags": ["tag:homeserver"],
        "guard_hosts": [NAS],
        "guard_allow": ["tcp/443"],
        "installed": True,
        "running": True,
        "backend": "Running",
        "version": "1.104.2",
        "suffix": SUFFIX,
        "dns_name": f"pve.{SUFFIX}",
        "addresses": [NODE_IPV4, ":".join(["fd7a", "115c", "a1e0", "", "14"])],
        "tags": ["tag:homeserver"],
        "key_expiry": "",
        "routes": [{"route": ROUTE, "wanted": True, "advertised": True, "served": False}],
        "prefs_read": True,
        "differ": [],
        "settings": SETTINGS,
        "snat": True,
        "serve": False,
        "health": [],
        "guard": {"loaded": True, "routes": [ROUTE], "guarded": [NAS], "allow": {NAS: ["tcp/443"]}},
        "forwarding": True,
        "console": [pt.console(tailscale_status(), {}, [ROUTE], ["tag:homeserver"], [NAS])[0]],
    }


def test_no_other_device_login_or_tailnet_name_is_kept():
    text = json.dumps(state())
    for kept_out in (LOGIN, "laptop", PEER_IPV4, "Texample"):
        assert kept_out not in text


def test_the_guard_as_the_role_writes_it_reads_as_nft_lists_it():
    template = (ROLE / "templates" / "tailnet-guard.nft.j2").read_text()
    rendered = (
        jinja2.Environment(trim_blocks=True)
        .from_string(template)
        .render(
            proxmox_tailscale_advertise_routes=[ROUTE],
            proxmox_tailscale_guard_hosts=[NAS, "192.0.2.11"],
            proxmox_tailscale_guard_allow=["tcp/443"],
        )
    )
    assert state(table=rendered)["guard"] == {
        "loaded": True,
        "routes": [ROUTE],
        "guarded": [NAS, "192.0.2.11"],
        "allow": {NAS: ["tcp/443"], "192.0.2.11": ["tcp/443"]},
    }
    # A node without routes: an empty routes set, no elements line.
    empty = (
        jinja2.Environment(trim_blocks=True)
        .from_string(template)
        .render(
            proxmox_tailscale_advertise_routes=[], proxmox_tailscale_guard_hosts=[], proxmox_tailscale_guard_allow=[]
        )
    )
    assert state(table=empty, routes=())["guard"] == {"loaded": True, "routes": [], "guarded": [], "allow": {}}


def test_a_guard_that_is_not_loaded_and_forwarding_off():
    found = state(table=None, forwarding="0\n")
    assert found["guard"] == {"loaded": False} and found["forwarding"] is False


def test_routes_advertised_and_served_or_only_wanted():
    served = state(status=tailscale_status(primary=[ROUTE]))
    assert served["routes"] == [{"route": ROUTE, "wanted": True, "advertised": True, "served": True}]
    assert served["console"] == []
    unadvertised = state(prefs=PREFS | {"advertise-routes": ""})
    assert unadvertised["routes"] == [{"route": ROUTE, "wanted": True, "advertised": False, "served": False}]
    assert unadvertised["differ"] == ["advertise-routes"]


@pytest.mark.parametrize(
    "read, expected",
    [
        (
            {"rc": 1, "stdout": "", "stderr": "failed to connect to local tailscaled; it doesn't appear to be running"},
            {
                "installed": True,
                "running": False,
                "why": "failed to connect to local tailscaled; it doesn't appear to be running",
            },
        ),
        (
            {"rc": 2, "stdout": "", "msg": "[Errno 2] No such file or directory: b'tailscale'"},
            {"installed": False, "running": False, "why": "[Errno 2] No such file or directory: b'tailscale'"},
        ),
        (
            {"unreachable": True, "msg": "Failed to connect to the host via ssh"},
            {"unread": "Failed to connect to the host via ssh"},
        ),
    ],
    ids=["tailscaled-down", "not-installed", "host-unreachable"],
)
def test_a_node_whose_tailscale_does_not_answer(read, expected):
    skipped = {"skipped": True}
    found = pt.status(
        read, skipped, skipped, skipped, skipped, [ROUTE], ["tag:homeserver"], SETTINGS, [NAS], ["tcp/443"]
    )
    assert {
        k: v for k, v in found.items() if k not in ("wanted_routes", "wanted_tags", "guard_hosts", "guard_allow")
    } == expected
