"""just status's callback on a terminal: which blocks of the report it prints as the hosts are read, always in the
report's own order, what the lines below them say meanwhile, and that what it prints in the end is the report play's
own text. The hosts' findings are test_status_filters.py's, split between the hosts as status.yml's plays leave them.
"""

import importlib.util
from pathlib import Path

import pytest
import test_status_filters as tsf
import yaml

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("homeserver_status", ROOT / "callback_plugins" / "homeserver_status.py")
callback = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(callback)
fs = tsf.fs

HOSTS = {"proxmox": ["homeserver"], "cluster": ["homeserver-k8s"], "nas": ["truenas"]}
SETTINGS = {
    "setup": tsf.EVERY_SETUP,
    "types": [],
    "read_at": "2026-10-21 14:32 PDT",
    "now": tsf.NOW,
    "admin": tsf.ADMIN_PAGES,
    "commands": tsf.COMMANDS | tsf.TAILSCALE_COMMANDS,
    "hosts": HOSTS,
}


def findings(**changes):
    """What each host's play leaves, by host: the Proxmox host's (its Tailscale early too), the cluster's, the NAS's,
    the probes split between the first two as each asks them."""
    full = tsf.everything() | changes
    vm = [p for p in full["probes"] if "homeserver.internal" not in p["url"]]
    early = {"tailscale": full["tailscale"], "tailnet_dns": full["tailnet_dns"]}
    return {
        "homeserver": ({**early, "proxmox": full["proxmox"], "probes": vm}, early),
        "homeserver-k8s": (
            {
                "cluster": full["cluster"],
                "etcd_snapshots": full["etcd_snapshots"],
                "probes": [p for p in full["probes"] if p not in vm],
            },
            None,
        ),
        "truenas": ({"nas": full["nas"]}, None),
    }


def report_text(hosts, settings=SETTINGS):
    """The report play's own text, from the same findings."""
    found = fs.merge([found for found, _ in hosts.values()])
    model = fs.report(
        found,
        settings["setup"],
        settings["types"],
        settings["read_at"],
        admin=settings["admin"],
        commands=settings["commands"],
        now=settings["now"],
    )
    return fs.text(model)


def printed(progress, blocks, end, reading="reading  proxmox ok 4.1 s"):
    """The text the callback prints, head first, with its line on what was read above the exit line."""
    lines = [*progress.head()]
    for block in blocks:
        lines += block
    if end:
        before, last = end
        lines += [*before, reading, *last]
    return "\n".join(lines) + "\n"


def with_reading(text, reading="reading  proxmox ok 4.1 s"):
    body, _, last = text.rstrip("\n").rpartition("\n")
    return f"{body}\n{reading}\n{last}\n"


def states(progress):
    return [(state, section) for state, section, _ in progress.lines()]


def test_the_sections_come_in_order_whatever_host_answers_first():
    hosts = findings()
    progress = callback.Progress(fs, SETTINGS)
    seen = []

    # The NAS first: its section waits for every section before it, the apps first, which need both other hosts.
    progress.update("truenas", found=hosts["truenas"][0])
    blocks, end = progress.advance()
    assert blocks == [] and end is None
    assert states(progress)[0] == ("waiting", "apps") and ("todo", "nas") in states(progress)
    assert progress.lines()[0][2] == "waiting for the Proxmox host and the cluster"
    assert dict((s, t) for _, s, t in progress.lines())["nas"] == "read · 1 to do"

    # The Proxmox host's Tailscale, early: the admin pages are read, and wait for the apps.
    progress.update("homeserver", early=hosts["homeserver"][1])
    blocks, end = progress.advance()
    assert blocks == [] and ("read", "admin") in states(progress)

    progress.update("homeserver-k8s", found=hosts["homeserver-k8s"][0])
    blocks, end = progress.advance()
    assert blocks == [] and progress.lines()[0][2] == "waiting for the Proxmox host"
    # The cluster's own section waits for the Proxmox host too, which says whether its VMs are stopped.
    assert ("waiting", "cluster") in states(progress)

    # The last: every section, what needs attention and the exit line, the report play's own text.
    progress.update("homeserver", found=hosts["homeserver"][0])
    blocks, end = progress.advance()
    seen += blocks
    assert end is not None and progress.lines() == []
    assert printed(progress, seen, end) == with_reading(report_text(hosts))
    assert progress.advance() == ([], None)


def test_a_section_shows_once_the_sections_before_it_have():
    """With the Proxmox host and the cluster read, every section but the NAS's is printed, and the NAS's waits."""
    hosts = findings()
    progress = callback.Progress(fs, SETTINGS)
    seen = []
    for host in ("homeserver-k8s", "homeserver"):
        progress.update(host, found=hosts[host][0])
        blocks, end = progress.advance()
        seen += blocks
        assert end is None
    assert [block[0].split()[0] for block in seen] == [
        "APPS",
        "ADMIN",
        "TAILSCALE",
        "VMS",
        "STORAGE",
        "BACKUPS",
        "CLUSTER",
    ]
    assert states(progress) == [("waiting", "nas"), ("waiting", "attention")]
    assert progress.lines()[0][2] == "waiting for the NAS"
    progress.update("truenas", found=hosts["truenas"][0])
    blocks, end = progress.advance()
    seen += blocks
    assert printed(progress, seen, end) == with_reading(report_text(hosts))


def test_a_host_that_could_not_be_read_says_so():
    hosts = findings(nas={"unread": "Connection refused"})
    progress = callback.Progress(fs, SETTINGS)
    progress.update("truenas", found=hosts["truenas"][0])
    progress.advance()
    assert dict((s, (state, t)) for state, s, t in progress.lines())["nas"] == (
        "unread",
        "the NAS could not be read: Connection refused",
    )


def test_nothing_more_after_a_host_failed():
    """A host refused leaves the run without a report: nothing more is printed, and no end."""
    hosts = findings()
    progress = callback.Progress(fs, SETTINGS)
    progress.update("homeserver-k8s", failed=True)
    for host in ("homeserver", "truenas"):
        progress.update(host, found=hosts[host][0])
        assert progress.advance() == ([], None)


@pytest.mark.parametrize("types", [["vms", "nas"], ["nas"]])
def test_sections_named_and_a_setup_without_a_nas(types):
    hosts = findings()
    progress = callback.Progress(fs, SETTINGS | {"types": types})
    assert [s for _, s in states(progress)] == [*types, "attention"]
    without = callback.Progress(fs, SETTINGS | {"setup": tsf.EVERY_SETUP | {"nas": False}})
    assert "nas" not in [s for _, s in states(without)]
    # The NAS's host says it read nothing, and the Proxmox host and the cluster are all there is.
    without.update("truenas", found={})
    for host in ("homeserver-k8s", "homeserver"):
        without.update(host, found=hosts[host][0])
    blocks, end = without.advance()
    assert end is not None and not any(block[0].startswith("NAS") for block in blocks)


def test_the_live_lines_fit_the_terminal():
    """Below what is printed: a line per section still to come, then the line on what is read; on a terminal too short
    for them, only the latter."""
    module = callback.CallbackModule()
    module._progress = callback.Progress(fs, SETTINGS)
    module._hosts = {"homeserver": {"label": "proxmox", "state": "ok", "done": False, "since": 0.0, "seconds": 0.0}}
    lines = module._frame(100, 40, "⠋")
    assert len(lines) == 10 and lines[-1].startswith("reading  proxmox ⠋")
    assert lines[0].startswith(" ⠋ ") and "APPS       waiting for the Proxmox host and the cluster · " in lines[0]
    assert lines[-2].endswith("ATTENTION  once every section is read")
    assert [line[:9] for line in module._frame(100, 8, "⠋")] == ["reading  "]
    # Each cut short of the terminal's width, so that none wraps.
    assert all(len(line) <= 39 for line in module._frame(40, 40, "⠋"))


def test_the_callbacks_documentation_is_yaml():
    """Ansible reads it as it loads the callback, and ends the run with an error if it is not YAML."""
    doc = yaml.safe_load(callback.DOCUMENTATION)
    assert doc["name"] == "homeserver_status" and doc["type"] == "stdout" and len(doc["description"]) == 4
