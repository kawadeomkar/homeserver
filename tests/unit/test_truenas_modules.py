"""Unit tests for the comparison logic in roles/truenas. No NAS is involved."""

import importlib.util
import sys
from pathlib import Path
from typing import ClassVar

import pytest

ROLE = Path(__file__).resolve().parents[2] / "roles" / "truenas"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The modules import their client as ansible.module_utils.truenas_api, as Ansible ships it.
api = load("ansible.module_utils.truenas_api", ROLE / "module_utils" / "truenas_api.py")
dataset = load("truenas_dataset", ROLE / "library" / "truenas_dataset.py")


def prop(rawvalue, source="LOCAL"):
    return {"rawvalue": rawvalue, "value": str(rawvalue).upper(), "source": source}


class TestDifferences:
    def test_equal_values_give_nothing(self):
        assert api.differences({"a": 1, "b": "x", "extra": 2}, {"a": 1, "b": "x"}) == {}

    def test_changed_scalar_is_returned(self):
        assert api.differences({"a": 1}, {"a": 2}) == {"a": 2}

    def test_missing_key_is_a_difference(self):
        assert api.differences({}, {"a": None}) == {} and api.differences({}, {"a": 1}) == {"a": 1}

    def test_scalar_lists_ignore_order(self):
        assert api.differences({"hosts": ["b", "a"]}, {"hosts": ["a", "b"]}) == {}
        assert api.differences({"hosts": ["a"]}, {"hosts": ["a", "b"]}) == {"hosts": ["a", "b"]}

    def test_nested_dict_compares_only_given_keys_and_returns_merged(self):
        current = {"schedule": {"minute": "00", "hour": "00", "dow": "7"}}
        assert api.differences(current, {"schedule": {"dow": "7"}}) == {}
        assert api.differences(current, {"schedule": {"hour": "03"}}) == {
            "schedule": {"minute": "00", "hour": "03", "dow": "7"}
        }


def test_pick_never_returns_unrequested_keys():
    config = {"ui_httpsredirect": False, "ui_certificate": {"privatekey": "secret"}}
    assert api.pick(config, ["ui_httpsredirect", "missing"]) == {"ui_httpsredirect": False}


def test_validation_errors_are_formatted():
    error = {"message": "Validation error", "data": {"reason": "[EINVAL] bad", "extra": [["x.path", "nope", 22]]}}
    assert api._format_error("m.create", error) == "m.create: [EINVAL] bad (x.path: nope)"


class TestSizes:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            (0, 0),
            (None, 0),
            (65536, 65536),
            ("64K", 65536),
            ("1T", 1099511627776),
            ("1TiB", 1099511627776),
            ("512", 512),
        ],
    )
    def test_to_bytes(self, given, expected):
        assert dataset.to_bytes(given) == expected

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            dataset.to_bytes("lots")

    def test_quota_goes_to_the_api_as_an_integer(self):
        assert dataset.for_api("quota", "1T") == 1099511627776
        assert dataset.for_api("quota", 0) is None
        assert dataset.for_api("recordsize", "64K") == "64K"


class TestMatches:
    def test_recordsize_compares_bytes(self):
        assert dataset.matches("recordsize", prop("65536"), "64K")
        assert not dataset.matches("recordsize", prop("131072"), "64K")

    def test_quota(self):
        assert dataset.matches("quota", prop("0"), 0)
        assert dataset.matches("quota", prop("0"), None)
        assert dataset.matches("quota", prop("1099511627776"), 1099511627776)
        assert not dataset.matches("quota", prop("0"), "1T")

    def test_enums_ignore_case(self):
        assert dataset.matches("sync", prop("disabled"), "DISABLED")
        assert dataset.matches("compression", prop("lz4"), "LZ4")
        assert not dataset.matches("sync", prop("standard"), "DISABLED")

    def test_old_spellings(self):
        assert dataset.matches("acltype", prop("posixacl"), "POSIX")

    def test_inherit_means_not_set_locally(self):
        assert dataset.matches("sync", prop("standard", "INHERITED"), "INHERIT")
        assert not dataset.matches("sync", prop("standard", "LOCAL"), "INHERIT")

    def test_missing_property_never_matches(self):
        assert not dataset.matches("sync", None, "STANDARD")


interface = load("truenas_interface", ROLE / "library" / "truenas_interface.py")


def test_a_refused_connection_counts_as_an_address_in_use():
    import socket

    with socket.socket() as s:  # find a loopback port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert "refused" in interface.address_in_use("127.0.0.1", port)


def test_a_listening_port_counts_as_an_address_in_use():
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        assert "accepts connections" in interface.address_in_use("127.0.0.1", server.getsockname()[1])


# ----------------------------------------------------------------------------------- truenas_info's status ---

info = load("truenas_info", ROLE / "library" / "truenas_info.py")
TIB = 2**40
SERIAL = "SERIAL-MUST-NOT-LEAK"


def size(value):
    return {"parsed": value, "rawvalue": str(value), "value": "x", "source": "NONE"}


class ScriptedAPI:
    """Answers each method as TrueNAS 25.10's reference documents it, times as {"$date": milliseconds}."""

    ANSWERS: ClassVar[dict] = {
        "network.configuration.config": {"hostname": "truenas", "domain": "local", "state": {}},
        "disk.details": {"used": [{"name": "sda", "serial": SERIAL, "model": "SSD", "size": TIB}], "unused": []},
        "system.version": "TrueNAS-25.10.7",
        "system.ready": True,
        "system.info": {"hostname": "truenas", "uptime_seconds": 86400.5, "system_serial": SERIAL},
        "interface.websocket_interface": {"name": "enp7s0"},
        "interface.websocket_local_ip": "192.0.2.10",
        "interface.query": [],
        "interface.has_pending_changes": False,
        "pool.query": [
            {
                "id": 1,
                "name": "persistent",
                "guid": "1",
                "status": "DEGRADED",
                "healthy": False,
                "status_detail": "One or more devices are faulted.",
                "path": "/mnt/persistent",
                "autotrim": {"rawvalue": "on"},
                "scan": {
                    "function": "SCRUB",
                    "state": "FINISHED",
                    "start_time": {"$date": 1791752400000},
                    "end_time": {"$date": 1791763200000},
                    "percentage": 100.0,
                    "errors": 2,
                    "bytes_processed": TIB,
                },
                "topology": {"data": [{"type": "RAIDZ1", "children": [{"type": "DISK", "serial": SERIAL}]}]},
            },
            {
                "id": 2,
                "name": "ephemeral",
                "guid": "2",
                "status": "ONLINE",
                "healthy": True,
                "path": "/mnt/ephemeral",
                "autotrim": {"rawvalue": "on"},
                "scan": None,
                "topology": {"data": [{"type": "DISK"}]},
            },
        ],
        "pool.dataset.query": [
            {"id": "persistent", "used": size(2 * TIB), "available": size(5 * TIB)},
            {"id": "persistent/nextcloud", "used": size(TIB), "available": size(5 * TIB)},
            {"id": "ephemeral", "used": size(TIB), "available": size(TIB)},
        ],
        "service.query": [{"service": "nfs", "enable": True, "state": "RUNNING"}],
        "systemdataset.config": {"pool": "boot-pool", "pool_set": True},
        "sharing.nfs.query": [],
        "pool.scrub.query": [{"id": 1, "pool": 1, "pool_name": "persistent", "threshold": 35, "enabled": True}],
        "alert.list": [
            {
                "klass": "SMART",
                "level": "CRITICAL",
                "formatted": "Device: sda, SMART test failed",
                "text": "%(device)s",
                "datetime": {"$date": 1792497600000},
                "dismissed": False,
                "uuid": "a",
            },
            {"klass": "Old", "level": "WARNING", "formatted": "dismissed", "datetime": {"$date": 1}, "dismissed": True},
        ],
        "pool.snapshottask.query": [
            {
                "id": 7,
                "dataset": "persistent/nextcloud",
                "enabled": True,
                "lifetime_value": 2,
                "lifetime_unit": "DAY",
                "naming_schema": "auto-%Y",
                "schedule": {
                    "minute": "0",
                    "hour": "*",
                    "dom": "*",
                    "month": "*",
                    "dow": "*",
                    "begin": "00:00",
                    "end": "23:59",
                },
                "state": {"state": "ERROR", "datetime": {"$date": 1792537200000}, "error": "out of space"},
            },
            {
                "id": 8,
                "dataset": "persistent/proxmox/vm",
                "enabled": True,
                "lifetime_value": 1,
                "lifetime_unit": "DAY",
                "schedule": {"minute": "0", "hour": "*"},
                "state": {"state": "PENDING"},
            },
        ],
    }

    def __init__(self):
        self.calls = []

    def call(self, method, *params):
        self.calls.append(method)
        return self.ANSWERS[method]


def test_status_reports_the_nas_health_in_the_same_login():
    api = ScriptedAPI()
    result = info.gather(api, importable=False, status=True)
    assert result["status"] == {
        "uptime_seconds": 86400.5,
        "pools": [
            {
                "name": "persistent",
                "status": "DEGRADED",
                "healthy": False,
                "detail": "One or more devices are faulted.",
                "layout": "raidz1",
                "used": 2 * TIB,
                "available": 5 * TIB,
                "scan": {
                    "function": "SCRUB",
                    "state": "FINISHED",
                    "percentage": 100.0,
                    "errors": 2,
                    "start_time": "2026-10-11T21:00:00+00:00",
                    "end_time": "2026-10-12T00:00:00+00:00",
                },
                "scrub": {"threshold": 35, "enabled": True},
            },
            {
                "name": "ephemeral",
                "status": "ONLINE",
                "healthy": True,
                "detail": "",
                "layout": "stripe",
                "used": TIB,
                "available": TIB,
                "scan": None,
                "scrub": None,
            },
        ],
        "alerts": [
            {
                "klass": "SMART",
                "level": "CRITICAL",
                "text": "Device: sda, SMART test failed",
                "datetime": "2026-10-20T12:00:00+00:00",
            }
        ],
        "snapshot_tasks": [
            {
                "id": 7,
                "dataset": "persistent/nextcloud",
                "enabled": True,
                "lifetime_value": 2,
                "lifetime_unit": "DAY",
                "schedule": {"minute": "0", "hour": "*", "dom": "*", "month": "*", "dow": "*"},
                "state": "ERROR",
                "last_run": "2026-10-20T23:00:00+00:00",
                "error": "out of space",
            },
            {
                "id": 8,
                "dataset": "persistent/proxmox/vm",
                "enabled": True,
                "lifetime_value": 1,
                "lifetime_unit": "DAY",
                "schedule": {"minute": "0", "hour": "*"},
                "state": "PENDING",
                "last_run": None,
                "error": "",
            },
        ],
    }
    assert SERIAL not in repr(result["status"])
    assert api.calls.count("system.info") == 1 and api.calls.count("pool.query") == 1


def test_without_status_nothing_more_is_asked():
    api = ScriptedAPI()
    result = info.gather(api, importable=False)
    assert "status" not in result
    assert not {"alert.list", "pool.snapshottask.query", "pool.scrub.query"} & set(api.calls)
    assert result["hostname"] == "truenas" and result["datasets"] == [
        d["id"] for d in api.ANSWERS["pool.dataset.query"]
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"$date": 1791763200000}, "2026-10-12T00:00:00+00:00"),
        (1791763200, "2026-10-12T00:00:00+00:00"),
        ("2026-10-12T00:00:00Z", "2026-10-12T00:00:00Z"),
        (None, None),
        ({}, None),
    ],
)
def test_api_times_become_rfc_3339(value, expected):
    assert info._date(value) == expected
