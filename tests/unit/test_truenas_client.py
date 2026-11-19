"""What the TLS fake never does, done in process: TrueNAS refusing, failing or going away.

The idempotence suite runs every module against tests/integration/fake_truenas.py, which always answers. Here
websocket.create_connection is replaced by a scripted NAS, and the clock by one whose sleep() returns at once,
to cover the client's retries and error handling, a failed job, and the interface commit's refusal, rollback
and check-in. It also shows that truenas_config never returns the UI certificate's private key, and truenas_user
no password or password hash, which no test that only reads Ansible's output can see.
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
config = load("truenas_config", ROLE / "library" / "truenas_config.py")
user = load("truenas_user", ROLE / "library" / "truenas_user.py")
permission = load("truenas_permission", ROLE / "library" / "truenas_permission.py")
resource = load("truenas_resource", ROLE / "library" / "truenas_resource.py")

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
    # Certificates count as validated: the fake has no TLS, and a client with neither validation nor a pin refuses.
    return api.TrueNASClient(NAS, "a-key", **({"validate_certs": True} | options))


class TestCertificates:
    def test_neither_validation_nor_a_pin_is_refused_before_connecting(self, nas):
        fake = nas({"auth.login_with_api_key": True})
        with pytest.raises(api.TrueNASCertificateError, match=r"nothing proves .* is the NAS.*key was not sent"):
            client(validate_certs=False)
        assert fake.sockets == []


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
    "validate_certs": True,
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


# system.general.config carries the UI certificate, private key included (roles/truenas/README.md: modules return only
# the fields they manage).
PRIVATE_KEY = "FAKE-UI-PRIVATE-KEY"
GENERAL = {
    "ui_httpsredirect": False,
    "ui_port": 80,
    "ui_certificate": {"id": 1, "name": "truenas_default", "certificate": "FAKE-CERT", "privatekey": PRIVATE_KEY},
}
CONFIG_ARGS = {
    "api_host": NAS,
    "api_key": "a-key",
    "validate_certs": True,
    "namespace": "system.general",
    "settings": {"ui_httpsredirect": True, "ui_certificate": 1},
}


class TestConfigNeverReturnsThePrivateKey:
    # Each test asserts what the run returned as well, so a run that failed early cannot pass for keeping the
    # key out.

    @pytest.mark.parametrize("check_mode", [True, False], ids=["check", "apply"])
    def test_a_run_returns_only_the_managed_fields(self, nas, run, check_mode):
        applied = {**GENERAL, "ui_httpsredirect": True}
        fake = nas(
            {
                "auth.login_with_api_key": True,
                "system.general.config": [GENERAL, applied],
                "system.general.update": None,
            }
        )
        result = run(config, CONFIG_ARGS, check_mode=check_mode)
        assert not result.get("failed"), result
        assert result["changed"]
        assert result["config"] == {"ui_httpsredirect": not check_mode, "ui_certificate": 1}
        assert len(fake.called("system.general.update")) == (0 if check_mode else 1)
        assert PRIVATE_KEY not in json.dumps(result)

    def test_a_setting_that_does_not_take_is_reported_without_the_key(self, nas, run):
        nas({"auth.login_with_api_key": True, "system.general.config": GENERAL, "system.general.update": None})
        result = run(config, CONFIG_ARGS)
        assert result["failed"] and "these fields did not take: ['ui_httpsredirect']" in result["msg"]
        assert result["config"] == {"ui_httpsredirect": False, "ui_certificate": 1}
        assert PRIVATE_KEY not in json.dumps(result)


# mail.config returns the mail server's password (TrueNAS API v25.10). The module compares it, so a rotated one is
# sent, but never returns it, and Ansible masks it as a no_log option's value.
PASSWORD = "FAKE-MAIL-PASSWORD"
MAIL = {"outgoingserver": "smtp.example.org", "port": 587, "pass": PASSWORD, "id": 1}
MAIL_ARGS = {
    "api_host": NAS,
    "api_key": "a-key",
    "validate_certs": True,
    "namespace": "mail",
    "settings": {"outgoingserver": "smtp.example.org", "port": 587},
    "secret_settings": {"pass": "FAKE-ROTATED-PASSWORD"},
}


class TestConfigNeverReturnsASecretSetting:
    @pytest.mark.parametrize("check_mode", [True, False], ids=["check", "apply"])
    def test_a_rotated_secret_is_sent_and_never_returned(self, nas, run, check_mode):
        rotated = {**MAIL, "pass": "FAKE-ROTATED-PASSWORD"}
        fake = nas({"auth.login_with_api_key": True, "mail.config": [MAIL, rotated], "mail.update": None})
        result = run(config, MAIL_ARGS, check_mode=check_mode)
        assert not result.get("failed"), result
        assert result["changed"]
        assert result["config"]["pass"] == "<secret>"
        assert result["diff"]["before"]["pass"] == "<secret>"
        assert result["diff"]["after"]["pass"] == "<secret, changed>"
        sent = fake.called("mail.update")
        assert sent == ([] if check_mode else [[{"pass": "FAKE-ROTATED-PASSWORD"}]])
        assert PASSWORD not in json.dumps(result) and "FAKE-ROTATED-PASSWORD" not in json.dumps(result)

    def test_an_unchanged_secret_changes_nothing(self, nas, run):
        fake = nas({"auth.login_with_api_key": True, "mail.config": MAIL, "mail.update": None})
        result = run(config, {**MAIL_ARGS, "secret_settings": {"pass": PASSWORD}})
        assert not result.get("failed"), result
        assert not result["changed"] and fake.called("mail.update") == []
        assert result["diff"]["after"]["pass"] == "<secret>"
        assert PASSWORD not in json.dumps(result)

    def test_a_secret_that_does_not_take_is_reported_without_its_value(self, nas, run):
        nas({"auth.login_with_api_key": True, "mail.config": MAIL, "mail.update": None})
        result = run(config, MAIL_ARGS)
        assert result["failed"] and "these fields did not take: ['pass']" in result["msg"]
        assert PASSWORD not in json.dumps(result) and "FAKE-ROTATED-PASSWORD" not in json.dumps(result)


# user.query returns each user's password hashes, and user.create and user.update echo the password they were given
# (TrueNAS 25.10.7's plugins/account.py). truenas_user asks for named fields alone, but a NAS that ignored select is
# scripted here: neither a hash nor a password may come back in the result.
SMB_PASSWORD = "FAKE-SMB-PASSWORD"
HASHES = {"unixhash": "$6$FAKE-UNIX-HASH", "smbhash": "FAKE-NT-HASH", "password_history": ["FAKE-OLD-HASH"]}
LOGIN = {
    "id": 7,
    "uid": 3101,
    "username": "tm-laptop",
    "full_name": "tm-laptop",
    "smb": True,
    "shell": "/usr/sbin/nologin",
    "home": "/var/empty",
    "locked": False,
    "password_disabled": False,
    "builtin": False,
    "immutable": False,
    **HASHES,
}
USER_ARGS = {
    "api_host": NAS,
    "api_key": "a-key",
    "validate_certs": True,
    "users": [{"username": "tm-laptop", "uid": 3101, "smb": True, "shell": "/usr/sbin/nologin"}],
    "passwords": {"tm-laptop": SMB_PASSWORD},
}


def no_secret(result):
    text = json.dumps(result)
    return SMB_PASSWORD not in text and not [value for value in HASHES.values() if str(value).strip("[]'") in text]


class TestUserNeverReturnsAPassword:
    @pytest.mark.parametrize("check_mode", [True, False], ids=["check", "apply"])
    def test_a_new_user_gets_its_password_and_group_and_returns_neither(self, nas, run, check_mode):
        fake = nas(
            {
                "auth.login_with_api_key": True,
                "user.query": [[]],
                "user.create": {**LOGIN, "password": SMB_PASSWORD},
            }
        )
        result = run(user, USER_ARGS, check_mode=check_mode)
        assert not result.get("failed"), result
        assert result["changed"] and result["actions"] == {"tm-laptop": "create"}
        assert result["diff"]["after"]["tm-laptop"]["password"] == "<secret, changed>"
        sent = fake.called("user.create")
        assert sent == (
            []
            if check_mode
            else [
                [
                    {
                        "username": "tm-laptop",
                        "full_name": "tm-laptop",
                        "smb": True,
                        "shell": "/usr/sbin/nologin",
                        "group_create": True,
                        "uid": 3101,
                        "password": SMB_PASSWORD,
                    }
                ]
            ]
        )
        assert fake.called("user.query")[0][1] == {"select": user.SELECT}
        assert no_secret(result)

    def test_an_existing_user_is_left_alone_and_its_hashes_never_returned(self, nas, run):
        fake = nas({"auth.login_with_api_key": True, "user.query": [[LOGIN]]})
        result = run(user, USER_ARGS)
        assert not result.get("failed"), result
        assert not result["changed"] and fake.called("user.update") == []
        assert no_secret(result)

    def test_a_reset_sends_the_password_and_never_returns_it(self, nas, run):
        fake = nas({"auth.login_with_api_key": True, "user.query": [[LOGIN]], "user.update": LOGIN})
        result = run(user, {**USER_ARGS, "users_reset": ["tm-laptop"]})
        assert result["changed"] and result["actions"] == {"tm-laptop": "reset password"}
        assert fake.called("user.update") == [[7, {"password": SMB_PASSWORD}]]
        assert no_secret(result)

    def test_smb_turned_on_sends_the_password_with_it(self, nas, run):
        closed = {**LOGIN, "smb": False, "locked": True, "password_disabled": True}
        fake = nas({"auth.login_with_api_key": True, "user.query": [[closed]], "user.update": LOGIN})
        args = {**USER_ARGS, "users": [{**USER_ARGS["users"][0], "locked": False, "password_disabled": False}]}
        result = run(user, args)
        assert result["actions"] == {"tm-laptop": "update locked,password_disabled,smb, password"}
        sent = fake.called("user.update")
        assert sent == [[7, {"smb": True, "locked": False, "password_disabled": False, "password": SMB_PASSWORD}]]
        assert no_secret(result)

    def test_a_closed_login_needs_no_password(self, nas, run):
        fake = nas({"auth.login_with_api_key": True, "user.query": [[LOGIN]], "user.update": LOGIN})
        closed = {**USER_ARGS["users"][0], "smb": False, "locked": True, "password_disabled": True}
        result = run(user, {**USER_ARGS, "users": [closed], "passwords": {}})
        assert not result.get("failed"), result
        assert fake.called("user.update") == [[7, {"smb": False, "locked": True, "password_disabled": True}]]

    def test_the_api_keys_user_is_taken_out_of_smb(self, nas, run):
        admin = {**LOGIN, "id": 4, "uid": 3000, "username": "api-admin"}
        fake = nas(
            {
                "auth.login_with_api_key": True,
                "auth.me": {"pw_name": "api-admin", "two_factor_config": {"provisioning_uri": "otpauth://SECRET"}},
                "user.query": [[admin]],
                "user.update": admin,
            }
        )
        result = run(user, {**USER_ARGS, "users": [], "api_user_smb": False})
        assert result["actions"] == {"api-admin": "update smb"}
        assert fake.called("user.update") == [[4, {"smb": False}]]
        assert no_secret(result) and "otpauth" not in json.dumps(result)

    def test_a_login_named_to_close_is_taken_out_of_smb_and_left_otherwise_alone(self, nas, run):
        someone = {**LOGIN, "id": 9, "uid": 3050, "username": "someone"}
        fake = nas(
            {
                "auth.login_with_api_key": True,
                "user.query": [[LOGIN], [someone], [], [LOGIN]],
                "user.update": someone,
            }
        )
        result = run(user, {**USER_ARGS, "smb_closed_users": ["someone", "gone"]})
        assert not result.get("failed"), result
        assert result["actions"] == {"someone": "update smb"}
        assert fake.called("user.update") == [[9, {"smb": False}]]
        assert result["smb_users"] == ["tm-laptop"]

    @pytest.mark.parametrize("check_mode", [True, False], ids=["check", "apply"])
    def test_the_smb_logins_after_the_changes_are_returned(self, nas, run, check_mode):
        admin = {**LOGIN, "id": 4, "uid": 3000, "username": "api-admin"}
        # The last query reads the SMB logins again: in check mode the API user is still one, as nothing was sent.
        nas(
            {
                "auth.login_with_api_key": True,
                "auth.me": {"pw_name": "api-admin"},
                "user.query": [[], [admin], [admin] if check_mode else [{**LOGIN, "username": "tm-laptop"}]],
                "user.create": LOGIN,
                "user.update": admin,
            }
        )
        result = run(user, {**USER_ARGS, "api_user_smb": False}, check_mode=check_mode)
        assert not result.get("failed"), result
        assert result["smb_users"] == ["tm-laptop"]

    def test_a_password_is_needed_only_when_one_is_sent(self, nas, run):
        nas({"auth.login_with_api_key": True, "user.query": [[LOGIN]]})
        result = run(user, {**USER_ARGS, "passwords": {}})
        assert not result.get("failed") and not result["changed"], result

        nas({"auth.login_with_api_key": True, "user.query": [[]]})
        result = run(user, {**USER_ARGS, "passwords": {}})
        assert result["failed"] and "no password in passwords, and the user needs one to be created" in result["msg"]

        closed = {**LOGIN, "smb": False}
        fake = nas({"auth.login_with_api_key": True, "user.query": [[closed]]})
        result = run(user, {**USER_ARGS, "passwords": {}})
        assert result["failed"] and "turning smb on or the password back on needs one" in result["msg"], result
        assert fake.called("user.update") == []

    def test_a_failure_part_way_says_what_was_already_done(self, nas, run):
        second = {"username": "tm-studio", "uid": 3102, "smb": True}
        fake = nas(
            {
                "auth.login_with_api_key": True,
                "user.query": [[{**LOGIN, "shell": "/usr/bin/zsh"}], [{**LOGIN, "username": "tm-studio", "uid": 3005}]],
                "user.update": LOGIN,
            }
        )
        args = {**USER_ARGS, "users": [*USER_ARGS["users"], second], "passwords": {"tm-laptop": SMB_PASSWORD}}
        result = run(user, args)
        assert result["failed"] and "tm-studio exists with uid 3005, not 3102" in result["msg"], result
        assert result["changed"] and result["actions"] == {"tm-laptop": "update shell"}
        assert len(fake.called("user.update")) == 1

    @pytest.mark.parametrize(
        ("found", "message"),
        [
            ({**LOGIN, "uid": 3005}, "exists with uid 3005, not 3101"),
            ({**LOGIN, "immutable": True}, "is one of TrueNAS's own users"),
            ({**LOGIN, "builtin": True}, "is one of TrueNAS's own users"),
        ],
    )
    def test_a_user_it_cannot_manage_is_refused(self, nas, run, found, message):
        fake = nas({"auth.login_with_api_key": True, "user.query": [[found]]})
        result = run(user, USER_ARGS)
        assert result["failed"] and message in result["msg"], result
        assert fake.called("user.update") == [] and no_secret(result)

    @pytest.mark.parametrize(
        ("me", "found", "args", "message"),
        [
            ({}, [], {"users": []}, "auth.me names no local user"),
            ({"pw_name": "nobody"}, [], {"users": []}, "auth.me names no local user"),
            ({"pw_name": "tm-laptop"}, [LOGIN], {}, "tm-laptop, the API key's user, is also in users"),
            (
                {"pw_name": "truenas_admin"},
                [{**LOGIN, "username": "truenas_admin", "uid": 950, "immutable": True}],
                {"users": []},
                "truenas_admin, the API key's user, is one of TrueNAS's own users",
            ),
        ],
    )
    def test_an_api_user_it_cannot_take_out_of_smb_is_refused(self, nas, run, me, found, args, message):
        fake = nas({"auth.login_with_api_key": True, "auth.me": me, "user.query": [found]})
        result = run(user, {**USER_ARGS, **args, "api_user_smb": False})
        assert result["failed"] and message in result["msg"], result
        assert fake.called("user.update") == []

    @pytest.mark.parametrize(
        ("changed", "message"),
        [
            ({"users": [{"username": "tm-laptop"}]}, "smb must be true or false"),
            (
                {"users": [{"username": "tm-laptop", "smb": True, "password_disabled": True}]},
                "cannot have password_disabled",
            ),
            ({"users_reset": ["someone"]}, "someone: in users_reset, but not one of the users"),
            (
                {
                    "users": [{"username": "tm-laptop", "smb": False, "password_disabled": True}],
                    "users_reset": ["tm-laptop"],
                },
                "tm-laptop: in users_reset, but its password is disabled",
            ),
            ({"users_reset": ["tm-laptop"], "passwords": {}}, "tm-laptop: in users_reset, but passwords has none"),
            ({"smb_closed_users": ["tm-laptop"]}, "tm-laptop: in smb_closed_users and in users"),
            ({"api_user_smb": True}, "only false is supported"),
            ({"users": [{"username": "tm-laptop", "smb": True, "groups": [545]}]}, "unknown field(s) ['groups']"),
            ({"users": [{"smb": True}]}, "a user has no username"),
            ({"users": [USER_ARGS["users"][0], USER_ARGS["users"][0]]}, "tm-laptop: given twice"),
        ],
    )
    def test_options_are_checked_before_connecting(self, nas, run, changed, message):
        fake = nas({"auth.login_with_api_key": True})
        result = run(user, {**USER_ARGS, **changed})
        assert result["failed"] and message in result["msg"], result
        assert fake.calls == [], "the NAS was asked something"


STAT = {"user": "tm-laptop", "group": "tm-laptop", "mode": 0o40700, "acl": False, "uid": 3101, "gid": 3101}
PERMISSION_ARGS = {
    "api_host": NAS,
    "api_key": "a-key",
    "validate_certs": True,
    "entries": [{"path": "/mnt/tank/timemachine/laptop", "user": "tm-laptop", "group": "tm-laptop", "mode": "700"}],
}


class TestPermission:
    def test_an_entry_it_refuses_is_refused_before_connecting(self, nas, run):
        fake = nas({"auth.login_with_api_key": True})
        bad = {**PERMISSION_ARGS["entries"][0], "mode": 504}
        result = run(permission, {**PERMISSION_ARGS, "entries": [bad]})
        assert result["failed"] and "is not an octal string" in result["msg"], result
        assert fake.calls == [], "the NAS was asked something"

    def test_a_missing_directory_in_a_dry_run_is_one_a_dataset_of_the_run_makes_or_a_mistake(self, nas, run):
        nas({"auth.login_with_api_key": True, "filesystem.stat": Refuse("[ENOENT] Path does not exist")})
        made = {**PERMISSION_ARGS, "created_paths": ["/mnt/tank/timemachine/laptop"]}
        result = run(permission, made, check_mode=True)
        assert not result.get("failed") and result["actions"] == {"/mnt/tank/timemachine/laptop": "set"}, result

        nas({"auth.login_with_api_key": True, "filesystem.stat": Refuse("[ENOENT] Path does not exist")})
        result = run(permission, PERMISSION_ARGS, check_mode=True)
        assert result["failed"] and "no such directory, and no dataset this run creates" in result["msg"], result

    def test_a_setperm_that_does_not_take_is_refused_with_what_was_done(self, nas, run):
        other = {**PERMISSION_ARGS["entries"][0], "path": "/mnt/tank/timemachine/studio"}
        fake = nas(
            {
                "auth.login_with_api_key": True,
                "filesystem.stat": [
                    {**STAT, "mode": 0o40770},
                    STAT,
                    {**STAT, "mode": 0o40770},
                    {**STAT, "user": "root"},
                ],
                "filesystem.setperm": 5,
                "core.get_jobs": [[{"id": 5, "state": "SUCCESS"}]],
            }
        )
        result = run(permission, {**PERMISSION_ARGS, "entries": [*PERMISSION_ARGS["entries"], other]})
        assert result["failed"] and "studio: filesystem.setperm was accepted but it now has" in result["msg"], result
        # The second directory's setperm ran, so it is in actions too, though it did not take.
        assert result["changed"] and result["actions"] == {
            "/mnt/tank/timemachine/laptop": "set mode",
            "/mnt/tank/timemachine/studio": "set",
        }
        assert len(fake.called("filesystem.setperm")) == 2


def test_update_whole_and_update_together_are_refused_together(nas, run):
    fake = nas({"auth.login_with_api_key": True})
    args = {"api_host": NAS, "api_key": "a-key", "validate_certs": True, "namespace": "sharing.smb", "key": ["name"]}
    result = run(resource, {**args, "entries": [], "update_whole": True, "update_together": ["purpose", "options"]})
    assert result["failed"] and "give it or update_together, not both" in result["msg"], result
    assert fake.calls == [], "the NAS was asked something"
