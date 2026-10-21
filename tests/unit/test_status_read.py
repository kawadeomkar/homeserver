"""roles/status's module status_read: its read list, and running commands at once.

The idempotence tests run it inside ansible-playbook against the fake Proxmox host; these cover what they never
do: every form a write could take being refused, a command that times out, one that cannot start, and the commands
really running at the same time.
"""

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "roles" / "status" / "library" / "status_read.py"
_spec = importlib.util.spec_from_file_location("status_read", MODULE)
status_read = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(status_read)

JSON = ["--output-format", "json"]
DAY = ["--timeframe", "day", "--cf", "AVERAGE"]


@pytest.mark.parametrize(
    "argv",
    [
        ["hostname", "-s"],
        ["test", "-e", "/run/reboot-required"],
        ["pvesh", "get", "/cluster/resources", *JSON],
        ["pvesh", "get", "/nodes/pve/status", *JSON],
        ["pvesh", "get", "/nodes/pve/rrddata", *DAY, *JSON],
        ["pvesh", "get", "/nodes/pve/qemu/301/rrddata", *DAY, *JSON],
        ["pvesh", "get", "/nodes/pve/qemu/310/pending", *JSON],
        ["pvesh", "get", "/nodes/pve/storage/local-lvm/content", *JSON],
        ["qm", "guest", "cmd", "310", "get-fsinfo"],
        ["qm", "guest", "cmd", "310", "get-osinfo"],
        ["qm", "guest", "cmd", "310", "network-get-interfaces"],
        ["apt-get", "-q", "-s", "dist-upgrade"],
    ],
)
def test_reads_are_allowed(argv):
    assert status_read.allowed(argv)


@pytest.mark.parametrize(
    "argv",
    [
        # Writes, and reads that could return a secret.
        ["pvesh", "set", "/nodes/pve/config", "--description", "x"],
        ["pvesh", "create", "/nodes/pve/qemu/310/status/stop"],
        ["pvesh", "get", "/access/users", *JSON],
        ["pvesh", "get", "/cluster/backup", *JSON],
        ["pvesh", "get", "/nodes/pve/qemu/310/config", *JSON],
        ["pvesh", "get", "/nodes/pve/status"],
        ["pvesh", "get", "/nodes/pve/storage/local-lvm/content", "--content", "backup", *JSON],
        ["qm", "guest", "cmd", "310", "fsfreeze-freeze"],
        ["qm", "guest", "cmd", "310", "shutdown"],
        ["qm", "guest", "exec", "310", "--", "cat", "/etc/shadow"],
        ["qm", "reboot", "310"],
        ["apt-get", "-q", "dist-upgrade"],
        ["apt-get", "-q", "-s", "dist-upgrade", "-y"],
        ["test", "-e", "relative/path"],
        ["test", "-e", "/run/x; rm -rf /"],
        # A path's parts must be what they say.
        ["pvesh", "get", "/nodes/pve/qemu/../../access/users/pending", *JSON],
        ["pvesh", "get", "/nodes/pve/qemu/1/pending", *JSON],
        ["hostname", "new-name"],
        ["sh", "-c", "hostname -s"],
        [],
    ],
)
def test_anything_else_is_refused(argv):
    assert not status_read.allowed(argv)


def test_commands_run_at_once():
    commands = {f"sleep-{i}": [sys.executable, "-c", "import time; time.sleep(0.6)"] for i in range(4)}
    started = time.monotonic()
    results = status_read.run_all(commands, parallel=4, timeout=10)
    assert time.monotonic() - started < 2.0, "four 0.6 s commands at once took more than one would"
    assert sorted(results) == sorted(commands)
    assert all(r["rc"] == 0 and r["error"] == "" for r in results.values())


def test_results_carry_output_status_and_time():
    script = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    result = status_read.run_one([sys.executable, "-c", script], timeout=10)
    assert (result["rc"], result["stdout"], result["stderr"], result["error"]) == (3, "out\n", "err\n", "")
    assert result["seconds"] >= 0


def test_a_command_that_runs_too_long_is_stopped():
    result = status_read.run_one([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
    assert result["rc"] is None and result["error"] == "timed out after 1 s"


def test_a_command_that_cannot_start_is_reported():
    result = status_read.run_one(["/nonexistent/status-read-test"], timeout=1)
    assert result["rc"] is None and result["error"]


def test_output_is_never_translated():
    script = "import os; print(os.environ['LC_ALL'], os.environ['LANG'])"
    assert status_read.run_one([sys.executable, "-c", script], timeout=10)["stdout"] == "C.UTF-8 C.UTF-8\n"


class _Exit(Exception):
    pass


class _Module:
    """AnsibleModule as far as main() uses it, recording what it ends with."""

    def __init__(self, params):
        self.params = {"parallel": 8, "timeout": 30, **params}
        self.ended = None

    def fail_json(self, **kwargs):
        self.ended = ("failed", kwargs)
        raise _Exit

    def exit_json(self, **kwargs):
        self.ended = ("ok", kwargs)
        raise _Exit


def run_main(monkeypatch, **params):
    module = _Module(params)
    monkeypatch.setattr(status_read, "AnsibleModule", lambda **_: module)
    with pytest.raises(_Exit):
        status_read.main()
    return module.ended


def test_one_command_off_the_list_refuses_the_whole_task(monkeypatch):
    ran = []
    monkeypatch.setattr(status_read, "run_all", lambda *a, **k: ran.append(a) or {})
    state, result = run_main(
        monkeypatch,
        commands={"name": ["hostname", "-s"], "stop": ["qm", "stop", "310"], "odd": "hostname -s"},
    )
    assert state == "failed" and ran == []
    assert "stop: qm stop 310" in result["msg"] and "odd: not a list of arguments" in result["msg"]
    assert "name:" not in result["msg"]


def test_reads_report_no_change(monkeypatch):
    monkeypatch.setattr(status_read, "run_all", lambda commands, *_: {n: {"rc": 0} for n in commands})
    state, result = run_main(
        monkeypatch, commands={"node": ["hostname", "-s"], "vm": ["qm", "guest", "cmd", 310, "get-osinfo"]}
    )
    assert state == "ok" and result["changed"] is False
    assert json.loads(json.dumps(result["results"])) == {"node": {"rc": 0}, "vm": {"rc": 0}}
