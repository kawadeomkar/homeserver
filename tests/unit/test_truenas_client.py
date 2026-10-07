"""What the TLS fake never does, done in process: TrueNAS refusing, failing or going away.

The idempotence suite runs every module against tests/integration/fake_truenas.py, which always answers. Here
websocket.create_connection is replaced by a scripted NAS, and the clock by one whose sleep() returns at once,
to cover the client's retries and error handling, a failed job, and the interface commit's refusal, rollback
and check-in.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
from ansible.module_utils.testing import patch_module_args

ROLE = Path(__file__).resolve().parents[2] / "roles" / "truenas"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The modules import their client as ansible.module_utils.truenas_api, as Ansible ships it.
api = load("ansible.module_utils.truenas_api", ROLE / "module_utils" / "truenas_api.py")
interface = load("truenas_interface", ROLE / "library" / "truenas_interface.py")

NAS = "nas.invalid"


class Refuse:
    """An error answer, as TrueNAS sends one."""

    def __init__(self, reason, extra=None):
        self.error = {"code": -32001, "message": "Method call error", "data": {"reason": reason, "extra": extra}}


# The connection closes instead of answering.
DROP = object()


class FakeNAS:
    """Answers JSON-RPC calls from a script: method -> outcomes, used in order, the last one repeating.

    An outcome is a result, a Refuse, or DROP; a list is a list of outcomes, so a result that is a list is
    written inside one. Connections to a host in `unreachable` are refused, as are the
    first `refusals` connections to any host.
    """

    def __init__(self, script, unreachable=(), refusals=0, notify=False):
        self.script = {
            method: list(outcomes) if isinstance(outcomes, list) else [outcomes] for method, outcomes in script.items()
        }
        self.unreachable = set(unreachable)
        self.refusals = refusals
        self.notify = notify
        self.calls = []
        self.sockets = []

    def create_connection(self, url, sslopt=None, timeout=None):
        host = url.split("://", 1)[1].rsplit(":", 1)[0].split("/")[0]
        if host in self.unreachable or self.refusals:
            self.refusals = max(self.refusals - 1, 0)
            raise ConnectionRefusedError(61, "Connection refused")
        sock = FakeSocket(self, host)
        self.sockets.append(sock)
        return sock

    def answer(self, host, method, params):
        self.calls.append((host, method, params))
        outcomes = self.script.get(method)
        if outcomes is None:
            return Refuse(f"[ENOMETHOD] the scripted NAS has no answer for {method}")
        return outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]

    def called(self, method):
        return [params for _, name, params in self.calls if name == method]


class FakeSocket:
    def __init__(self, nas, host):
        self.nas = nas
        self.host = host
        self.inbox = []
        self.closed = False

    def settimeout(self, timeout):
        pass

    def send(self, text):
        request = json.loads(text)
        outcome = self.nas.answer(self.host, request["method"], request["params"])
        if self.nas.notify:
            # What a real server may interleave: a notification, which has no id, and another call's answer.
            self.inbox.append({"jsonrpc": "2.0", "method": "collection_update", "params": {}})
            self.inbox.append({"jsonrpc": "2.0", "id": request["id"] + 1000, "result": "not this one"})
        if outcome is DROP:
            self.inbox.append(DROP)
        elif isinstance(outcome, Refuse):
            self.inbox.append({"jsonrpc": "2.0", "id": request["id"], "error": outcome.error})
        else:
            self.inbox.append({"jsonrpc": "2.0", "id": request["id"], "result": outcome})

    def recv(self):
        message = self.inbox.pop(0)
        if message is DROP:
            raise api.websocket.WebSocketConnectionClosedException("Connection to remote host was lost.")
        return json.dumps(message)

    def close(self):
        self.closed = True


class Clock:
    """Stands in for the time module: sleep() moves monotonic() on at once.

    A wait that never ends would hang the test, so waiting more than an hour fails it instead.
    """

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        if sum(self.slept) > 3600:
            raise AssertionError("waited more than an hour: a retry or poll loop never ends")


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(api, "time", clock)
    monkeypatch.setattr(interface, "time", clock)
    return clock


@pytest.fixture
def nas(monkeypatch, clock):
    def start(script, **options):
        fake = FakeNAS(script, **options)
        monkeypatch.setattr(api.websocket, "create_connection", fake.create_connection)
        return fake

    return start


def client(**options):
    return api.TrueNASClient(NAS, "a-key", **options)


class TestLogin:
    def test_a_rate_limited_login_waits_for_the_window_and_retries(self, nas, clock):
        fake = nas({"auth.login_with_api_key": [Refuse("Rate Limit Exceeded")] * 2 + [True]})
        client(login_wait=75)
        assert len(fake.called("auth.login_with_api_key")) == 3
        assert clock.slept == [10, 10]

    def test_a_rate_limited_login_gives_up_after_login_wait(self, nas, clock):
        fake = nas({"auth.login_with_api_key": Refuse("Rate Limit Exceeded")})
        with pytest.raises(
            api.TrueNASError, match=re.escape("refused the login: auth.login_with_api_key: Rate Limit Exceeded")
        ):
            client(login_wait=25)
        assert sum(clock.slept) == 30
        assert fake.sockets[0].closed

    def test_a_refused_login_is_not_retried(self, nas, clock):
        fake = nas({"auth.login_with_api_key": Refuse("[EINVAL] Invalid API key")})
        with pytest.raises(api.TrueNASError, match="refused the login"):
            client()
        assert len(fake.called("auth.login_with_api_key")) == 1
        assert clock.slept == []

    def test_a_rejected_key_fails(self, nas):
        fake = nas({"auth.login_with_api_key": False})
        with pytest.raises(api.TrueNASError, match="rejected the API key"):
            client()
        assert fake.sockets[0].closed


class TestConnection:
    def test_a_refused_connection_is_retried(self, nas, clock):
        nas({"auth.login_with_api_key": True}, refusals=2)
        client(connect_wait=60)
        assert clock.slept == [2, 2]

    def test_a_refused_connection_fails_after_connect_wait(self, nas, clock):
        nas({}, unreachable={NAS})
        with pytest.raises(
            api.TrueNASConnectionError, match=re.escape(f"cannot connect to wss://{NAS}:443/api/current")
        ):
            client(connect_wait=10)
        assert sum(clock.slept) == 10

    def test_a_connection_lost_mid_call_is_a_connection_error(self, nas):
        nas({"auth.login_with_api_key": True, "system.info": DROP})
        with pytest.raises(
            api.TrueNASConnectionError, match=re.escape("system.info: connection to nas.invalid failed")
        ):
            client().call("system.info")

    def test_notifications_and_other_answers_are_skipped(self, nas):
        nas({"auth.login_with_api_key": True, "system.info": {"version": "25.10.7"}}, notify=True)
        assert client().call("system.info") == {"version": "25.10.7"}

    def test_an_error_answer_carries_the_validation_details(self, nas):
        nas(
            {
                "auth.login_with_api_key": True,
                "pool.dataset.create": Refuse("[EINVAL] bad", [["pool.name", "nope", 22]]),
            }
        )
        with pytest.raises(api.TrueNASCallError, match=r"^pool.dataset.create: \[EINVAL\] bad \(pool.name: nope\)$"):
            client().call("pool.dataset.create", {})


class TestJobs:
    def job_nas(self, nas, *states, **job):
        jobs = [[{"id": 7, "state": state, **job}] for state in states]
        return nas({"auth.login_with_api_key": True, "pool.import_pool": 7, "core.get_jobs": jobs})

    def test_a_job_is_polled_until_it_succeeds(self, nas, clock):
        self.job_nas(nas, "RUNNING", "RUNNING", "SUCCESS", result=True)
        assert client().job("pool.import_pool", {}, poll=3) is True
        assert clock.slept == [3, 3]

    @pytest.mark.parametrize("state", ["FAILED", "ABORTED"])
    def test_a_failed_job_raises_its_error(self, nas, state):
        self.job_nas(
            nas, "RUNNING", state, error="[EFAULT] Pool import failed\n", exc_info={"extra": [["pool", "busy", 16]]}
        )
        with pytest.raises(
            api.TrueNASCallError, match=r"^pool.import_pool: \[EFAULT\] Pool import failed \(pool: busy\)$"
        ):
            client().job("pool.import_pool", {})

    def test_a_job_that_outlives_its_timeout_fails(self, nas):
        self.job_nas(nas, "RUNNING")
        with pytest.raises(api.TrueNASError, match=re.escape("job 7 still RUNNING after 10s")):
            client().job("pool.import_pool", {}, timeout=10)

    def test_a_job_that_disappears_fails(self, nas):
        nas({"auth.login_with_api_key": True, "pool.import_pool": 7, "core.get_jobs": [[]]})
        with pytest.raises(api.TrueNASError, match=re.escape("job 7 disappeared")):
            client().job("pool.import_pool", {})


@pytest.fixture
def run(capsys):
    """Run a module's main() in process and return its JSON result."""

    def run(module, args, check_mode=False):
        # _ansible_no_log keeps AnsibleModule from writing the run to this machine's syslog.
        arguments = {**args, "_ansible_check_mode": check_mode, "_ansible_no_log": True}
        with patch_module_args(arguments), pytest.raises(SystemExit):
            module.main()
        return json.loads(capsys.readouterr().out)

    return run


OLD, NEW, GATEWAY = "192.0.2.50", "192.0.2.10", "192.0.2.1"


def interface_nas(nas, commit, login=True, pending=(False, False), waiting=(None, 60), rollback=None, unreachable=()):
    """A NAS on OLD, with DHCP, being moved to NEW.

    `pending` and `waiting` answer interface.has_pending_changes and interface.checkin_waiting in turn: first the
    module's check for unfinished changes, then the commit's clean-up or the check-in on NEW.
    """
    iface = {
        "name": "eno1",
        "ipv4_dhcp": True,
        "ipv6_auto": False,
        "aliases": [{"type": "INET", "address": OLD, "netmask": 24}],
        "state": {"aliases": [{"type": "INET", "address": OLD, "netmask": 24}]},
    }
    moved = {**iface, "ipv4_dhcp": False, "aliases": [{"type": "INET", "address": NEW, "netmask": 24}]}
    return nas(
        {
            "auth.login_with_api_key": login,
            "interface.query": [[iface], [moved]],
            "network.configuration.config": {
                "ipv4gateway": GATEWAY,
                "nameserver1": GATEWAY,
                "nameserver2": "",
                "nameserver3": "",
            },
            "interface.has_pending_changes": list(pending),
            "interface.checkin_waiting": list(waiting),
            "interface.update": None,
            "interface.commit": commit,
            "interface.rollback": rollback,
            "interface.checkin": None,
        },
        unreachable=unreachable,
    )


INTERFACE_ARGS = {
    "api_host": OLD,
    "api_key": "a-key",
    "interface": "eno1",
    "address": f"{NEW}/24",
    "gateway": GATEWAY,
    "nameservers": [GATEWAY],
    "checkin_timeout": 60,
    "conflict_check": False,
}


class TestInterfaceCommit:
    def test_a_commit_and_check_in_move_the_nas(self, nas, run):
        fake = interface_nas(nas, commit=DROP)
        result = run(interface, INTERFACE_ARGS)
        assert not result.get("failed"), result
        assert result["changed"] and result["api_host"] == NEW
        assert [host for host, method, _ in fake.calls if method == "interface.checkin"] == [NEW]

    def test_a_refused_commit_discards_the_saved_change(self, nas, run):
        fake = interface_nas(nas, commit=Refuse("[EINVAL] the gateway is unreachable"), pending=(False, True))
        result = run(interface, INTERFACE_ARGS)
        assert result["failed"] and "interface.commit: [EINVAL] the gateway is unreachable" in result["msg"]
        assert len(fake.called("interface.rollback")) == 1
        assert fake.called("interface.checkin") == []

    def test_a_refused_commit_whose_clean_up_fails_reports_both(self, nas, run):
        interface_nas(
            nas,
            commit=Refuse("[EINVAL] the gateway is unreachable"),
            pending=(False, True),
            rollback=Refuse("[EFAULT] busy"),
        )
        result = run(interface, INTERFACE_ARGS)
        assert result["failed"]
        assert (
            "the gateway is unreachable; discarding the saved change also failed: interface.rollback: [EFAULT] busy"
            in (result["msg"])
        )

    def test_no_check_in_before_the_deadline_leaves_the_rollback_to_truenas(self, nas, run, clock):
        fake = interface_nas(nas, commit=DROP, unreachable={NEW})
        result = run(interface, INTERFACE_ARGS)
        assert result["failed"] and f"could not check in on {NEW} before the deadline" in result["msg"]
        assert "TrueNAS rolls the interface back by itself" in result["msg"]
        assert fake.called("interface.checkin") == [] and fake.called("interface.rollback") == []
        # The deadline is checkin_timeout less a 5-second margin, counted from the commit.
        assert 53 <= sum(clock.slept) <= 57

    def test_the_check_in_waits_out_the_login_rate_limit(self, nas, run):
        fake = interface_nas(nas, commit=DROP, login=[True, Refuse("Rate Limit Exceeded"), True])
        result = run(interface, INTERFACE_ARGS)
        assert not result.get("failed"), result
        assert [host for host, method, _ in fake.calls if method == "interface.checkin"] == [NEW]

    def test_the_check_in_waits_for_the_commit_to_finish(self, nas, run, clock):
        # Pending changes but no rollback timer yet: the commit is still applying them.
        fake = interface_nas(nas, commit=DROP, pending=(False, True, False), waiting=(None, None, 60))
        result = run(interface, INTERFACE_ARGS)
        assert not result.get("failed"), result
        assert len(fake.called("interface.checkin")) == 1 and clock.slept == [2]
