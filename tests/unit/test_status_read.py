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
KUBECTL = ["kubectl", "--kubeconfig", "/home/me/.talos/homeserver/kubeconfig", "--request-timeout=10s"]
AMTOOL = ["--", "amtool", "alert", "query", "-o", "json", "--alertmanager.url=http://127.0.0.1:9093"]


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
        ["pvesh", "get", "/nodes/pve/storage/homeserver-backup/content", "--content", "backup", *JSON],
        ["pvesh", "get", "/cluster/backup", *JSON],
        ["pvesh", "get", "/cluster/jobs/schedule-analyze", "--schedule", "02:00", "--iterations", "2", *JSON],
        ["pvesh", "get", "/cluster/jobs/schedule-analyze", "--schedule", "mon..fri */30", "--iterations", "2", *JSON],
        ["pvesh", "get", "/nodes/pve/tasks", "--typefilter", "vzdump", "--source", "all", "--limit", "20", *JSON],
        [
            "pvesh",
            "get",
            "/nodes/pve/tasks/UPID:pve:002BE7DF:04A56412:6AD87F11:vzdump::root@pam:/log",
            "--limit",
            "5000",
            *JSON,
        ],
        [
            "pvesh",
            "get",
            "/nodes/pve/tasks/UPID:pve:0000A1B2:1004A56412:6AD87F11:vzdump:301:root@pam!ci:/log",
            "--limit",
            "50",
            *JSON,
        ],
        ["qm", "guest", "cmd", "310", "get-fsinfo"],
        ["qm", "guest", "cmd", "310", "get-osinfo"],
        ["qm", "guest", "cmd", "310", "network-get-interfaces"],
        ["apt-get", "-q", "-s", "dist-upgrade"],
        *(
            [*KUBECTL, "get", kind, "-A", "-o", "json"]
            for kind in (
                "nodes",
                "pods",
                "services",
                "ingresses",
                "persistentvolumes",
                "kustomizations",
                "helmreleases",
                "fluxinstances",
                "certificates",
            )
        ),
        [*KUBECTL, "get", "deployments", "-A", "-o", "json", "--show-managed-fields"],
        [*KUBECTL, "get", "configmap", "cluster-settings", "-n", "flux-system", "-o", "json"],
        [*KUBECTL, "get", "--raw", "/api/v1/nodes/homeserver-worker-1/proxy/stats/summary"],
        [
            *KUBECTL,
            "exec",
            "-n",
            "monitoring",
            "alertmanager-kube-prometheus-stack-alertmanager-0",
            "-c",
            "alertmanager",
            *AMTOOL,
        ],
        # A credentials file is wherever its owner keeps it, spaces and all: no shell reads it.
        [
            "kubectl",
            "--kubeconfig",
            "/Users/A Person/.talos/homeserver/kubeconfig",
            "--request-timeout=10s",
            "get",
            "nodes",
            "-A",
            "-o",
            "json",
        ],
        ["talosctl", "--talosconfig", "/home/me/.talos/homeserver/talosconfig", "service", "etcd"],
        ["git", "ls-remote", "https://github.com/example/homeserver.git", "refs/heads/main"],
        ["git", "ls-remote", "git@github.com:example/homeserver.git", "refs/heads/deploy/nuc"],
        ["git", "ls-remote", "ssh://git@example.com/homeserver.git", "refs/tags/v1"],
        ["tailscale", "dns", "status", "--json"],
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
        ["pvesh", "get", "/nodes/pve/qemu/310/config", *JSON],
        ["pvesh", "get", "/nodes/pve/status"],
        ["pvesh", "get", "/nodes/pve/storage/local-lvm/content", "--content", "images", *JSON],
        # The backup jobs are read, never made, changed or run; a task's log only a backup's, and its other files
        # never.
        ["pvesh", "create", "/cluster/backup", "--id", "x", "--schedule", "02:00", "--storage", "local"],
        ["pvesh", "set", "/cluster/backup/vm-nightly", "--enabled", "0"],
        ["pvesh", "create", "/nodes/pve/vzdump", "--vmid", "301"],
        [
            "pvesh",
            "get",
            "/cluster/jobs/schedule-analyze",
            "--schedule",
            "--output-format=yaml",
            "--iterations",
            "2",
            *JSON,
        ],
        ["pvesh", "get", "/cluster/jobs/schedule-analyze", "--schedule", "02:00", "--iterations", "999", *JSON],
        ["pvesh", "get", "/nodes/pve/tasks", "--limit", "20", *JSON],
        ["pvesh", "get", "/nodes/pve/tasks", "--typefilter", "qmstart", "--source", "all", "--limit", "20", *JSON],
        [
            "pvesh",
            "get",
            "/nodes/pve/tasks/UPID:pve:002BE7DF:04A56412:6AD87F11:qmstart:301:root@pam:/log",
            "--limit",
            "50",
            *JSON,
        ],
        ["pvesh", "get", "/nodes/pve/tasks/UPID:pve:002BE7DF:04A56412:6AD87F11:vzdump::root@pam:/status", *JSON],
        [
            "pvesh",
            "get",
            "/nodes/pve/tasks/UPID:pve:002BE7DF:04A56412:6AD87F11:vzdump::root@pam:/log",
            "--download",
            "1",
        ],
        ["pvesh", "get", "/nodes/pve/tasks/../../access/users/log", "--limit", "50", *JSON],
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
        # The cluster: never a Secret, never a write, never another --raw path, never talosctl get.
        [*KUBECTL, "get", "secrets", "-A", "-o", "json"],
        [*KUBECTL, "get", "secret", "sops-age", "-n", "flux-system", "-o", "json"],
        [*KUBECTL, "get", "nodes,secrets", "-A", "-o", "json"],
        [*KUBECTL, "get", "deployments", "-A", "-o", "yaml"],
        [*KUBECTL, "get", "--raw", "/api/v1/namespaces/flux-system/secrets"],
        [*KUBECTL, "get", "--raw", "/api/v1/nodes/../../namespaces/flux-system/secrets/proxy/stats/summary"],
        [*KUBECTL, "delete", "pods", "-A", "-o", "json"],
        [*KUBECTL, "apply", "-f", "/tmp/x.yaml"],
        [*KUBECTL, "exec", "-n", "monitoring", "alertmanager-0", "-c", "alertmanager", "--", "sh", "-c", "id"],
        [*KUBECTL, "exec", "-n", "monitoring", "prometheus-0", "-c", "prometheus", *AMTOOL],
        ["kubectl", "get", "nodes", "-A", "-o", "json"],
        ["kubectl", "--kubeconfig", "relative/kubeconfig", "--request-timeout=10s", "get", "nodes", "-A", "-o", "json"],
        ["talosctl", "--talosconfig", "/home/me/.talos/homeserver/talosconfig", "get", "machineconfig"],
        ["talosctl", "--talosconfig", "/home/me/.talos/homeserver/talosconfig", "service", "etcd", "restart"],
        ["talosctl", "--talosconfig", "/home/me/.talos/homeserver/talosconfig", "service", "kubelet"],
        # git lists a branch over HTTPS or SSH, never through a transport that runs a command, nor fetches or pushes.
        ["git", "ls-remote", "ext::sh -c touch% /tmp/pwned", "refs/heads/main"],
        ["git", "ls-remote", "--upload-pack=touch /tmp/x", "refs/heads/main"],
        ["git", "ls-remote", "/srv/repo.git", "refs/heads/main"],
        ["git", "ls-remote", "https://github.com/example/homeserver.git", "HEAD"],
        # This machine's Tailscale: its DNS alone, never the tailnet's devices, a login or a change.
        ["tailscale", "status", "--json"],
        ["tailscale", "dns", "status", "--all", "--json"],
        ["tailscale", "up"],
        ["tailscale", "set", "--accept-dns=false"],
        ["git", "fetch", "https://github.com/example/homeserver.git", "refs/heads/main"],
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


def test_git_never_waits_for_a_password():
    result = status_read.run_one([sys.executable, "-c", "import os; print(os.environ['GIT_TERMINAL_PROMPT'])"], 10)
    assert result["stdout"].strip() == "0"


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
