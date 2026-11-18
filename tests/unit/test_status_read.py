"""roles/status's module status_read: its read list, and running commands at once.

The idempotence tests run it inside ansible-playbook against the fake Proxmox host; these cover what they never
do: every form a write could take being refused, a command that times out, one that cannot start, and the commands
really running at the same time.
"""

import importlib.util
import json
import socket
import ssl
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import certs

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
        # Which VMs run: qemu-server's pid files, on the host and where a test keeps its own.
        ["ls", "/run/qemu-server"],
        ["ls", "/tmp/pytest-0/test_x/qemu-server"],
        # The two listings Proxmox gives no read-only token stay pvesh's, as root.
        ["pvesh", "get", "/nodes/pve/storage/local-lvm/content", *JSON],
        ["pvesh", "get", "/nodes/pve/storage/homeserver-backup/content", "--content", "backup", *JSON],
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
        # ls of the VMs' run directory alone: no other directory, no option, no file of it.
        ["ls", "/etc/pve/priv"],
        ["ls", "/run/qemu-server/../../etc/pve/priv"],
        ["ls", "/var/run/../qemu-server"],
        ["ls", "/etc/pve/priv/../../run/qemu-server"],
        ["ls", "/./qemu-server"],
        ["ls", "-la", "/run/qemu-server"],
        ["ls", "/run/qemu-server/310.pid"],
        ["ls", "qemu-server"],
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
        # Root's reads that the API with the token replaced: refused, so no later edit slips back to them.
        ["pvesh", "get", "/cluster/resources", *JSON],
        ["pvesh", "get", "/nodes/pve/status", *JSON],
        ["pvesh", "get", "/nodes/pve/rrddata", *DAY, *JSON],
        ["pvesh", "get", "/nodes/pve/qemu/301/rrddata", *DAY, *JSON],
        ["pvesh", "get", "/nodes/pve/qemu/310/pending", *JSON],
        ["pvesh", "get", "/cluster/backup", *JSON],
        ["pvesh", "get", "/cluster/jobs/schedule-analyze", "--schedule", "02:00", "--iterations", "2", *JSON],
        ["pvesh", "get", "/nodes/pve/tasks", "--typefilter", "vzdump", "--source", "all", "--limit", "20", *JSON],
        ["pvesh", "get", "/nodes/pve/tasks/UPID:pve:002BE7DF:04A56412:6AD87F11:vzdump::root@pam:/log", "--limit", "50"],
        ["qm", "guest", "cmd", "310", "get-fsinfo"],
        ["qm", "guest", "cmd", "310", "get-osinfo"],
        ["qm", "guest", "cmd", "310", "network-get-interfaces"],
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
        self.params = {
            "parallel": None,
            "timeout": 30,
            "timeouts": {},
            "api": {},
            "api_url": "",
            "api_token_id": "",
            "api_token_secret": "",
            "api_cert_files": [],
            **params,
        }
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
    state, result = run_main(monkeypatch, commands={"node": ["hostname", "-s"], "running": ["ls", "/run/qemu-server"]})
    assert state == "ok" and result["changed"] is False
    assert json.loads(json.dumps(result["results"])) == {"node": {"rc": 0}, "running": {"rc": 0}}


def test_reads_run_as_many_at_once_as_the_machine_has_cpus_unless_told(monkeypatch):
    """Unset, parallel is the machine's CPU count, 8 at most: each pvesh call is a second of CPU and about 180 MB."""
    widths = []
    monkeypatch.setattr(status_read, "run_all", lambda commands, parallel, *_: widths.append(parallel) or {})
    for cpus in (20, 4, None):
        monkeypatch.setattr(status_read.os, "cpu_count", lambda cpus=cpus: cpus)
        assert run_main(monkeypatch, commands={"node": ["hostname", "-s"]})[0] == "ok"
    assert run_main(monkeypatch, commands={"node": ["hostname", "-s"]}, parallel=32)[0] == "ok"
    assert widths == [8, 4, 1, 32]
    for bad in ({"parallel": 0}, {"timeouts": {"node": 0}}, {"timeouts": {"node": "5"}}):
        state, _ = run_main(monkeypatch, commands={"node": ["hostname", "-s"]}, **bad)
        assert state == "failed", bad


def test_a_command_can_have_a_time_of_its_own():
    """This machine's tailnet DNS gives up as a probe does, in the cluster's batch, whose reads may take longer."""
    commands = {"quick": [sys.executable, "-c", "import time; time.sleep(5)"], "slow": [sys.executable, "-c", "pass"]}
    started = time.monotonic()
    results = status_read.run_all(commands, parallel=2, timeout=30, timeouts={"quick": 1})
    assert results["quick"]["error"] == "timed out after 1 s" and results["slow"]["rc"] == 0
    assert time.monotonic() - started < 4


# A token's secret as Proxmox makes them, a UUID, made here: gitleaks would take a written one for a leak.
SECRET = str(uuid.uuid4())


@pytest.fixture
def api(tmp_path):
    """Proxmox's API as this module meets it, in this process: GETs answered by path, anything without the token 401,
    each request recorded with its Authorization header."""
    cert, key = certs.self_signed(tmp_path, "pve")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            if self.headers.get("Authorization") != f"PVEAPIToken=homeserver@pve!homeserver={SECRET}":
                self.send_response(401, "permission denied - invalid PVE ticket")
                self.end_headers()
                return
            path = self.path.split("?")[0]
            if path.endswith("/status"):
                self.send_response(500, "hostname lookup 'nope' failed")
                self.end_headers()
                return
            if path.endswith("/pending"):
                self.send_response(403, "Permission check failed (/vms/310, VM.Audit)")
                self.end_headers()
                return
            if path.endswith("/backup"):
                body = b'{"data": null}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            data = {"result": [{"mountpoint": "/mnt/data"}]} if "/agent/" in path else [{"path": self.path}]
            body = json.dumps({"data": data}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"https://127.0.0.1:{server.server_address[1]}/api2/json", "cert": cert, "requests": requests}
    server.shutdown()


def settings(api, **more):
    return status_read.api_settings(
        {
            "api_url": api["url"],
            "api_token_id": "homeserver@pve!homeserver",
            "api_token_secret": SECRET,
            "api_cert_files": [str(api["cert"].with_name("missing.pem")), str(api["cert"])],
        }
        | more
    )


def test_api_reads_return_the_data_as_pvesh_prints_it(api):
    """The API's data member, as pvesh get --output-format json prints it; a guest agent's reply unwrapped, as qm guest
    cmd prints it; the query in the read list's order, a task's id as pvesh's own requests send it."""
    upid = "UPID:pve:0001A2B3:0C4D5E6F:6730A1B2:vzdump::root@pam:"
    reads = {
        "rrd": ["/nodes/pve/rrddata", "timeframe=day", "cf=AVERAGE"],
        "fsinfo": ["/nodes/pve/qemu/310/agent/get-fsinfo"],
        "schedule": ["/cluster/jobs/schedule-analyze", "schedule=mon..fri 02:00", "iterations=2"],
        "log": [f"/nodes/pve/tasks/{upid}/log", "limit=5000"],
    }
    results = status_read.run_all({}, 4, 10, reads=reads, api=settings(api))
    assert {name: r["rc"] for name, r in results.items()} == {"rrd": 0, "fsinfo": 0, "schedule": 0, "log": 0}
    assert json.loads(results["rrd"]["stdout"]) == [{"path": "/api2/json/nodes/pve/rrddata?timeframe=day&cf=AVERAGE"}]
    assert json.loads(results["fsinfo"]["stdout"]) == [{"mountpoint": "/mnt/data"}]
    assert json.loads(results["schedule"]["stdout"])[0]["path"].endswith("schedule=mon..fri%2002%3A00&iterations=2")
    assert json.loads(results["log"]["stdout"])[0]["path"] == f"/api2/json/nodes/pve/tasks/{upid}/log?limit=5000"


def test_an_api_read_that_fails_says_why_and_never_the_token(api):
    """Its reason, and `token` when the failure is the token's: refused (401) or forbidden (403), which the filter reads
    rather than the words; 200 with no data is no answer either."""
    reads = {"node": ["/nodes/nope/status"], "pending": ["/nodes/pve/qemu/310/pending"], "jobs": ["/cluster/backup"]}
    results = status_read.run_all({}, 2, 10, reads=reads, api=settings(api))
    assert results["node"]["rc"] is None and results["node"]["error"] == "HTTP 500: hostname lookup 'nope' failed"
    assert "token" not in results["node"]
    assert results["pending"]["token"] == "forbidden"
    assert (
        results["pending"]["error"]
        == "the API token may not read this (HTTP 403: Permission check failed (/vms/310, VM.Audit))"
    )
    assert results["jobs"]["rc"] is None and results["jobs"]["error"] == "the API answered no data"
    other = str(uuid.uuid4())
    refused = status_read.run_all(
        {}, 2, 10, reads={"all": ["/cluster/resources"]}, api=settings(api, api_token_secret=other)
    )
    assert refused["all"]["error"] == "Proxmox refused the API token homeserver@pve!homeserver (HTTP 401)"
    assert refused["all"]["token"] == "refused"
    assert SECRET not in json.dumps([results, refused]) and other not in json.dumps(refused)


@pytest.mark.parametrize(
    ("more", "why"),
    [
        # A YAML block scalar leaves a line break at the end: the HTTP library's own error would quote the header.
        ({"api_token_secret": SECRET + "\n"}, "the API token's secret is not a token's"),
        ({"api_token_secret": "not-a-token secret"}, "the API token's secret is not a token's"),
        ({"api_token_id": "homeserver@pve!homeserver\n"}, "the API token's id is not <user>@<realm>!<name>"),
        ({"api_token_id": "homeserver@pve"}, "the API token's id is not <user>@<realm>!<name>"),
    ],
)
def test_a_token_of_another_shape_is_refused_before_anything_is_sent_and_never_quoted(api, more, why):
    results = status_read.run_all({}, 2, 10, reads={"all": ["/cluster/resources"]}, api=settings(api, **more))
    assert results["all"]["rc"] is None and results["all"]["error"].startswith(why)
    assert results["all"]["token"] == "missing"
    assert SECRET not in json.dumps(results) and api["requests"] == []


def test_an_address_without_a_host_or_with_a_bad_port_is_a_problem_not_an_exception(api):
    for url in (
        "https://127.0.0.1:99999/api2/json",
        "https://127.0.0.1:0/api2/json",
        "https:///api2/json",
        "not a url",
    ):
        results = status_read.run_all({}, 2, 10, reads={"all": ["/cluster/resources"]}, api=settings(api, api_url=url))
        assert results["all"]["error"] == "no https API address with a host, and a port from 1 to 65535 if it names one"
        assert "token" not in results["all"]


def test_an_address_without_a_port_means_https_own():
    """No hidden default: an address without a port is https's 443, as anywhere else; Proxmox's 8006 is named."""
    api = status_read.api_settings(
        {"api_url": "https://127.0.0.1/api2/json", "api_token_id": "", "api_token_secret": "", "api_cert_files": []}
    )
    assert (api["host"], api["port"], api["path"]) == ("127.0.0.1", 443, "/api2/json")


def test_an_error_no_one_foresaw_is_named_by_its_type_alone(api, monkeypatch):
    """Whatever http.client raises that is no OSError or HTTPException may quote what it was given, the Authorization
    header among it: the read fails with the error's type alone, and the task goes on."""

    def exploding(self, method, url, body=None, headers=None, **_):
        raise ValueError(f"Invalid header value {headers['Authorization']!r}")

    # On the class itself: HTTPSConnection's own __init__ names its class through the module, so a subclass put in
    # its place there would call the wrong __init__.
    monkeypatch.setattr(status_read.http.client.HTTPSConnection, "request", exploding)
    results = status_read.run_all({}, 2, 10, reads={"all": ["/cluster/resources"]}, api=settings(api))
    assert results["all"]["error"].endswith("the read failed (ValueError)")
    assert SECRET not in json.dumps(results)


def test_the_token_goes_only_to_the_nodes_own_certificate(api, tmp_path):
    """Another certificate than the first file's that exists, and no request is made at all; no file, no token, or no
    https address, and no connection either."""
    other, _ = certs.self_signed(tmp_path, "impostor")
    results = status_read.run_all(
        {}, 2, 10, reads={"all": ["/cluster/resources"]}, api=settings(api, api_cert_files=[str(other)])
    )
    assert results["all"]["error"] == (
        f"the API at {api['url'].split('/')[2]} serves another certificate than {other}'s: no token sent"
    )
    for more, why in [
        ({"api_cert_files": [str(tmp_path / "missing.pem")]}, f"the node's certificate: none of {tmp_path}"),
        ({"api_token_secret": ""}, "no API token"),
        ({"api_url": api["url"].replace("https", "http")}, "no https API address"),
    ]:
        results = status_read.run_all({}, 2, 10, reads={"all": ["/cluster/resources"]}, api=settings(api, **more))
        assert results["all"]["rc"] is None and results["all"]["error"].startswith(why)
    assert api["requests"] == []


def test_api_reads_and_commands_run_in_one_pass(api, monkeypatch):
    """main() refuses an API read off its list as it refuses a command, and makes the rest beside the commands."""
    state, result = run_main(
        monkeypatch, commands={"node": ["hostname", "-s"]}, api={"users": ["/access/users"], "ok": ["/cluster/backup"]}
    )
    assert state == "failed" and result["msg"].endswith("users: GET /access/users")
    monkeypatch.setattr(status_read.os, "cpu_count", lambda: 2)
    state, result = run_main(
        monkeypatch,
        commands={"node": ["hostname", "-s"]},
        api={"all": ["/cluster/resources"]},
        api_url=api["url"],
        api_token_id="homeserver@pve!homeserver",
        api_token_secret=SECRET,
        api_cert_files=[str(api["cert"])],
    )
    assert state == "ok" and set(result["results"]) == {"node", "all"}
    assert result["results"]["all"]["rc"] == 0 and result["results"]["node"]["rc"] == 0


def test_once_the_token_is_refused_the_reads_after_it_never_ask(api):
    """Proxmox holds each refusal 3 s: the first 401 is the only one asked for, and every read says why."""
    reads = {
        f"rrd-{vmid}": [f"/nodes/pve/qemu/{vmid}/rrddata", "timeframe=day", "cf=AVERAGE"] for vmid in range(300, 306)
    }
    results = status_read.run_all({}, 1, 10, reads=reads, api=settings(api, api_token_secret=str(uuid.uuid4())))
    assert {r["token"] for r in results.values()} == {"refused"}
    assert len(api["requests"]) == 1


def test_an_api_nothing_answers_names_pveproxy(api):
    """pveproxy serves the API and the web UI alike: when nothing listens, the read says so, and is marked for the
    report to say what to check."""
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
    url = f"https://127.0.0.1:{port}/api2/json"
    results = status_read.run_all({}, 1, 10, reads={"all": ["/cluster/resources"]}, api=settings(api, api_url=url))
    assert results["all"]["error"] == f"pveproxy does not answer at 127.0.0.1:{port} (connection refused)"
    assert results["all"]["unanswered"] is True and "token" not in results["all"]
