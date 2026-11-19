"""Unit tests for the comparison logic in roles/truenas. No NAS is involved."""

import importlib.util
import sys
import time
from datetime import datetime
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
HASH = "HASH-MUST-NOT-LEAK"
# What TrueNAS answers beside what just status keeps, none of which it may return: a disk's model, a vdev's own name
# (its path's, which on a pool made by hand on a by-id path carries the disk's serial), a port's MAC, and an NFSv4
# client's name for itself and the files it holds open.
MODEL = "MODEL-MUST-NOT-LEAK"
VDEV_NAME = "VDEV-NAME-MUST-NOT-LEAK"
LINK_ADDRESS = "LINK-ADDRESS-MUST-NOT-LEAK"
CLIENT_NAME = "Linux NFSv4.2 CLIENT-NAME-MUST-NOT-LEAK"
OPEN_FILE = "/OPEN-FILE-MUST-NOT-LEAK"
# The NAS's own serial, which system.info gives and an alert could quote.
NAS_SERIAL = "NAS-SERIAL-MUST-NOT-LEAK"


def counts(read=0, write=0, checksum=0):
    """ZFS's error counts of a disk as truenas_info's status reports them."""
    return {"read_errors": read, "write_errors": write, "checksum_errors": checksum}


def disk_vdev(disk, status="ONLINE", guid="1", unavail_disk=None, **errors):
    """A disk in pool.query's topology as libzfs gives it, with the device and disk TrueNAS works out from its path
    (none for a disk that is gone, which unavail_disk then names, a disk.query entry)."""
    stats = {"read_errors": 0, "write_errors": 0, "checksum_errors": 0, "ops": [0] * 7, "bytes": [0] * 7} | errors
    return {
        "name": VDEV_NAME,
        "type": "DISK",
        "path": f"/dev/disk/by-partuuid/{guid}",
        "guid": guid,
        "status": status,
        "stats": stats,
        "children": [],
        "device": f"{disk}p1" if disk else None,
        "disk": disk,
        "unavail_disk": unavail_disk,
    }


def size(value):
    return {"parsed": value, "rawvalue": str(value), "value": "x", "source": "NONE"}


def snapshot(name):
    """pool.snapshot.query's entry for <dataset>@<snapshot> on TrueNAS's fast path (a select of name alone, filtered on
    dataset): libzfs's entry whole, without properties (zfs.snapshot.query, TS-25.10.7)."""
    parent, snapshot_name = name.split("@")
    return {
        "pool": parent.split("/")[0],
        "name": name,
        "type": "SNAPSHOT",
        "snapshot_name": snapshot_name,
        "dataset": parent,
        "id": name,
        "createtxg": "1",
    }


def time_machine_share(share_id, mac, enabled=True):
    """A Mac's Time Machine share as sharing.smb.query returns it, at its dataset under persistent/timemachine."""
    return {
        "id": share_id,
        "name": f"tm-{mac}",
        "path": f"/mnt/persistent/timemachine/{mac}",
        "purpose": "TIMEMACHINE_SHARE",
        "enabled": enabled,
        "options": {"auto_snapshot": True},
    }


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
                # A disk with checksum errors, one gone that TrueNAS knows, one gone it does not, and a cache disk.
                "topology": {
                    "data": [
                        {
                            "name": "raidz1-0",
                            "type": "RAIDZ1",
                            "guid": "10",
                            "status": "DEGRADED",
                            "stats": {"read_errors": 0, "write_errors": 0, "checksum_errors": 0},
                            "children": [
                                disk_vdev("nvme1n1", guid="11", checksum_errors=3),
                                disk_vdev(
                                    None,
                                    "UNAVAIL",
                                    "12",
                                    {"name": "nvme2n1", "serial": SERIAL, "model": MODEL},
                                    read_errors=2,
                                ),
                                disk_vdev(None, "REMOVED", "4242"),
                            ],
                        }
                    ],
                    "cache": [disk_vdev("nvme4n1", guid="14")],
                    "log": [],
                    "spare": [],
                    "special": [],
                    "dedup": [],
                },
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
                "topology": {"data": [disk_vdev("sda", guid="2")]},
            },
        ],
        "pool.dataset.query": [
            {"id": "persistent", "used": size(2 * TIB), "available": size(5 * TIB)},
            {"id": "persistent/nextcloud", "used": size(TIB), "available": size(5 * TIB)},
            {"id": "ephemeral", "used": size(TIB), "available": size(TIB)},
            {
                "id": "persistent/timemachine/laptop",
                "used": size(400 * 2**30),
                "quota": size(2 * TIB),
                "usedbysnapshots": size(20 * 2**30),
            },
        ],
        # Samba's snapshots after each completed backup; a weekly one is no backup's.
        "pool.snapshot.query": [
            snapshot("persistent/timemachine/laptop@aapltm-1791763200"),
            snapshot("persistent/timemachine/laptop@aapltm-1792497600"),
            snapshot("persistent/timemachine/laptop@weekly-2026-10-18_00-00"),
        ],
        "service.query": [{"service": "nfs", "enable": True, "state": "RUNNING"}],
        "systemdataset.config": {"pool": "boot-pool", "pool_set": True},
        "sharing.nfs.query": [],
        "sharing.smb.query": [
            {
                "id": 1,
                "name": "tm-laptop",
                "path": "/mnt/persistent/timemachine/laptop",
                "purpose": "TIMEMACHINE_SHARE",
                "enabled": True,
                "options": {"vuid": "d12aafdc-a7ac-4e3c-8bbd-6001f7f19819"},
            },
            {"id": 2, "name": "files", "path": "/mnt/persistent/files", "purpose": "DEFAULT_SHARE", "enabled": True},
        ],
        # As a NAS that ignored select would answer: the module keeps the names alone.
        "user.query": [{"username": "api-admin", "smbhash": HASH, "unixhash": f"$6${HASH}"}],
        "auth.me": {"pw_name": "api-admin", "pw_uid": 3000, "two_factor_config": {"secret_configured": False}},
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
        # With thresholds, each [celsius, critical celsius] (plugins/disk_/temperature.py at TS-25.10.7); a disk with no
        # sensor TrueNAS can read has nulls, and an older answer a bare null.
        "disk.temperatures": {"nvme0n1": [45.0, 80.85], "nvme1n1": [36.0, 80.85], "sda": [None, None], "vda": None},
        # rmtab's entries, one per export (plugins/nfs_/status.py at TS-25.10.7)...
        "nfs.get_nfs3_clients": [
            {"ip": "192.0.2.20", "export": "/mnt/persistent/proxmox/vm"},
            {"ip": "192.0.2.20", "export": "/mnt/persistent/proxmox/backup"},
        ],
        # ...and the kernel's NFSv4 clients, an address with its port, IPv6 in brackets: two live ones behind one
        # address, one whose info went away, and three that are not live, one that stopped renewing its lease
        # (courtesy), one about to be cleaned up (expirable) and one that never confirmed itself. The answer is the
        # same whatever the select, as a NAS that ignored it would give it, so nothing else may be kept.
        "nfs.get_nfs4_clients": [
            {
                "id": "4",
                "info": {
                    "clientid": 6273260596088110000,
                    "address": "192.0.2.31:739",
                    "status": "confirmed",
                    "name": CLIENT_NAME,
                    "Implementation name": CLIENT_NAME,
                },
                "states": [{"1": {"type": "open", "access": "rw", "filename": OPEN_FILE}}],
            },
            {"id": "5", "info": {"address": "[2001:db8::5]:801", "status": "confirmed"}, "states": []},
            {"id": "6", "info": {}, "states": []},
            {"id": "7", "info": {"address": "192.0.2.20:812", "status": "confirmed"}, "states": []},
            {"id": "8", "info": {"address": "192.0.2.20:901", "status": "confirmed"}, "states": []},
            {"id": "9", "info": {"address": "198.51.100.9:700", "status": "courtesy"}, "states": []},
            {"id": "10", "info": {"address": "198.51.100.10:700", "status": "expirable"}, "states": []},
            {"id": "11", "info": {"address": "198.51.100.11:700", "status": "unconfirmed"}, "states": []},
        ],
    }

    def __init__(self):
        self.calls = []
        # Each call with its parameters; the answers do not depend on them.
        self.sent = []
        # The timeout each call was given, where it was given one.
        self.timeouts = {}

    def call(self, method, *params, timeout=None):
        self.calls.append(method)
        self.sent.append((method, params))
        if timeout is not None:
            self.timeouts[method] = timeout
        return self.ANSWERS[method]


class StatusAPI(ScriptedAPI):
    """ScriptedAPI, with what just status also reads of two answers the role reads as well: the NAS's clock in
    system.info, and in interface.query a port's type, MTU and link, beside its MAC, which nothing may return."""

    ANSWERS: ClassVar[dict] = ScriptedAPI.ANSWERS | {
        "system.info": ScriptedAPI.ANSWERS["system.info"] | {"datetime": {"$date": 1792540800000}},
        "interface.query": [
            {
                "id": "eno1",
                "name": "eno1",
                "type": "PHYSICAL",
                "aliases": [],
                "ipv4_dhcp": False,
                "ipv6_auto": False,
                "state": {
                    "name": "eno1",
                    "aliases": [],
                    "link_state": "LINK_STATE_UP",
                    "mtu": 1500,
                    "active_media_subtype": "2500Mb/s Twisted Pair",
                    "link_address": LINK_ADDRESS,
                },
            }
        ],
    }


def test_status_reports_the_nas_health_in_the_same_login():
    api = StatusAPI()
    before = time.time()
    result = info.gather(api, importable=False, status=True)
    after = time.time()
    # This machine's clock when the NAS read its own, which the report compares: during the read.
    read_at = datetime.fromisoformat(result["status"].pop("read_at")).timestamp()
    assert before - 0.001 <= read_at <= after + 0.001
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
                # The data disks in order, then the cache's; a gone disk by the one TrueNAS knew there, else its guid.
                "disks": [
                    {"name": "nvme1n1", "status": "ONLINE", "gone": False, **counts(checksum=3)},
                    {"name": "nvme2n1", "status": "UNAVAIL", "gone": True, **counts(read=2)},
                    {"name": "4242", "status": "REMOVED", "gone": True, **counts()},
                    {"name": "nvme4n1", "status": "ONLINE", "gone": False, **counts()},
                ],
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
                "disks": [{"name": "sda", "status": "ONLINE", "gone": False, **counts()}],
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
        "time_machine": [
            {
                "name": "tm-laptop",
                "enabled": True,
                "dataset": "persistent/timemachine/laptop",
                "dataset_found": True,
                "used": 400 * 2**30,
                "quota": 2 * TIB,
                "snapshots_used": 20 * 2**30,
                "last_backup": "2026-10-20T12:00:00+00:00",
            }
        ],
        "datetime": "2026-10-21T00:00:00+00:00",
        "temperatures": {
            "nvme0n1": {"temp": 45.0, "critical": 80.85},
            "nvme1n1": {"temp": 36.0, "critical": 80.85},
            "sda": {"temp": None, "critical": None},
            "vda": {"temp": None, "critical": None},
        },
        # Each address once, without its port, with how many live clients it is: nothing else of an NFSv4 client's
        # entry, and no client that is not live.
        "nfs_clients": [
            {"address": "192.0.2.20", "clients": 2},
            {"address": "192.0.2.31", "clients": 1},
            {"address": "2001:db8::5", "clients": 1},
        ],
        "unread": {},
    }
    for secret in (SERIAL, MODEL, VDEV_NAME, LINK_ADDRESS, CLIENT_NAME, OPEN_FILE):
        assert secret not in repr(result["status"]), secret
    assert api.calls.count("system.info") == 1 and api.calls.count("pool.query") == 1
    # The reads that may fail on their own come last, the temperatures of every disk with their thresholds.
    assert api.calls[-3:] == ["disk.temperatures", "nfs.get_nfs3_clients", "nfs.get_nfs4_clients"]
    assert [params for method, params in api.sent if method == "disk.temperatures"] == [([], True)]
    select = ([], {"select": ["info.address", "info.status"]})
    assert [params for method, params in api.sent if method.startswith("nfs.")] == [(), select]
    # Each of them within 10 s, where every other call has the client's own timeout.
    assert api.timeouts == {"disk.temperatures": 10, "nfs.get_nfs3_clients": 10, "nfs.get_nfs4_clients": 10}
    # The shares are read once, for the shares and the Time Machine backups alike.
    assert api.calls.count("sharing.smb.query") == 1
    assert not {"user.query", "auth.me"} & set(api.calls) and "smb_users" not in result
    # One snapshot read, for the Time Machine share's dataset alone and by name alone: TrueNAS's fast path.
    assert [params for method, params in api.sent if method == "pool.snapshot.query"] == [
        ([["dataset", "in", ["persistent/timemachine/laptop"]]], {"select": ["name"]})
    ]


class MacsAPI(ScriptedAPI):
    """Four Macs' Time Machine shares beside another share: laptop's and desk's enabled, old's disabled, and gone's
    enabled at a path where the NAS has no dataset. ScriptedAPI answers every query alike, whatever it filters on, so
    every dataset's snapshots come back: each counts for its own dataset alone, and for none that was not asked."""

    ANSWERS: ClassVar[dict] = ScriptedAPI.ANSWERS | {
        "pool.dataset.query": [
            *ScriptedAPI.ANSWERS["pool.dataset.query"],
            {
                "id": "persistent/timemachine/desk",
                "used": size(100 * 2**30),
                "quota": size(TIB),
                "usedbysnapshots": size(5 * 2**30),
            },
            {
                "id": "persistent/timemachine/old",
                "used": size(300 * 2**30),
                "quota": size(2 * TIB),
                "usedbysnapshots": size(0),
            },
        ],
        "sharing.smb.query": [
            *ScriptedAPI.ANSWERS["sharing.smb.query"],
            time_machine_share(3, "desk"),
            time_machine_share(4, "old", enabled=False),
            time_machine_share(5, "gone"),
        ],
        "pool.snapshot.query": [
            *ScriptedAPI.ANSWERS["pool.snapshot.query"],
            # Laptop's, in 2100 and in the year 10000: no backup's, though each is its newest.
            snapshot("persistent/timemachine/laptop@aapltm-4102444800"),
            snapshot("persistent/timemachine/laptop@aapltm-253402300800"),
            # Desk's, newer than laptop's newest.
            snapshot("persistent/timemachine/desk@aapltm-1792540800"),
            # The disabled share's, which was not asked for.
            snapshot("persistent/timemachine/old@aapltm-1792540800"),
        ],
    }


def test_each_time_machine_backup_is_its_own_datasets_newest():
    """One snapshot read for every enabled share whose dataset exists; each backup's time from its own dataset's
    snapshots, a time ahead of now or past what datetime holds ignored; none for a disabled share; and a share whose
    dataset is missing says so, without sizes."""
    api = MacsAPI()
    result = info.gather(api, importable=False, status=True)
    laptop, desk, old, gone = (f"persistent/timemachine/{mac}" for mac in ("laptop", "desk", "old", "gone"))
    assert [params for method, params in api.sent if method == "pool.snapshot.query"] == [
        ([["dataset", "in", [laptop, desk]]], {"select": ["name"]})
    ]
    assert result["status"]["time_machine"] == [
        {
            "name": "tm-laptop",
            "enabled": True,
            "dataset": laptop,
            "dataset_found": True,
            "used": 400 * 2**30,
            "quota": 2 * TIB,
            "snapshots_used": 20 * 2**30,
            "last_backup": "2026-10-20T12:00:00+00:00",
        },
        {
            "name": "tm-desk",
            "enabled": True,
            "dataset": desk,
            "dataset_found": True,
            "used": 100 * 2**30,
            "quota": TIB,
            "snapshots_used": 5 * 2**30,
            "last_backup": "2026-10-21T00:00:00+00:00",
        },
        {
            "name": "tm-old",
            "enabled": False,
            "dataset": old,
            "dataset_found": True,
            "used": 300 * 2**30,
            "quota": 2 * TIB,
            "snapshots_used": 0,
            "last_backup": None,
        },
        {
            "name": "tm-gone",
            "enabled": True,
            "dataset": gone,
            "dataset_found": False,
            "used": None,
            "quota": None,
            "snapshots_used": None,
            "last_backup": None,
        },
    ]


@pytest.mark.parametrize(
    "shares",
    [
        pytest.param([], id="no-share"),
        pytest.param([time_machine_share(1, "laptop", enabled=False)], id="disabled"),
        pytest.param([time_machine_share(5, "gone")], id="no-dataset"),
    ],
)
def test_no_snapshot_is_read_without_an_enabled_time_machine_share_on_a_dataset(shares):
    """TrueNAS takes an empty list of datasets for every pool, so no read is made with none to ask for."""
    api = ScriptedAPI()
    api.ANSWERS = ScriptedAPI.ANSWERS | {"sharing.smb.query": shares}
    result = info.gather(api, importable=False, status=True)
    assert "pool.snapshot.query" not in api.calls
    assert [b["last_backup"] for b in result["status"]["time_machine"]] == [None] * len(shares)


def test_without_status_nothing_more_is_asked():
    api = ScriptedAPI()
    result = info.gather(api, importable=False)
    assert "status" not in result
    status_reads = {"alert.list", "pool.snapshottask.query", "pool.scrub.query", "pool.snapshot.query"}
    status_reads |= {"disk.temperatures", "nfs.get_nfs3_clients", "nfs.get_nfs4_clients"}
    assert not status_reads & set(api.calls)
    assert [share["name"] for share in result["smb_shares"]] == ["tm-laptop", "files"]
    assert result["smb_shares"][0] == {
        "name": "tm-laptop",
        "path": "/mnt/persistent/timemachine/laptop",
        "purpose": "TIMEMACHINE_SHARE",
        "enabled": True,
    }
    assert (result["smb_users"], result["api_user"]) == (["api-admin"], "api-admin")
    assert HASH not in repr(result)
    assert result["hostname"] == "truenas" and result["datasets"] == [
        d["id"] for d in api.ANSWERS["pool.dataset.query"]
    ]


def alert(klass, level, formatted, **args):
    """An alert as alert.list returns it, its text formatted as its source words it."""
    when = {"$date": 1792497600000}
    return {"klass": klass, "level": level, "formatted": formatted, "args": args, "datetime": when, "dismissed": False}


# How TrueNAS 25.10.7 words the pool's state when a disk is gone (py-libzfs's MISSING_DEV_R).
MISSING = (
    "One or more devices could not be opened. Sufficient replicas exist for the pool to continue functioning in a "
    "degraded state."
)


class AlertsAPI(ScriptedAPI):
    """ScriptedAPI, with alerts that quote disks' serials and models as TrueNAS 25.10.7's alert sources word them
    (alert/source/disk_temp.py, smart.py and volume_status.py): sda and nvme1n1 in disk.details, each with a serial
    of its own, the gone nvme2n1 that pool.query's unavail_disk names by the model and serial TrueNAS last knew there,
    and a text no source has, which quotes the NAS's own serial and a model alone."""

    ANSWERS: ClassVar[dict] = ScriptedAPI.ANSWERS | {
        "disk.details": {
            "used": [
                {"name": "sda", "serial": f"{SERIAL}-SDA", "model": MODEL, "size": TIB},
                {"name": "nvme1n1", "serial": f"{SERIAL}-NVME1", "model": MODEL, "size": TIB},
            ],
            "unused": [],
        },
        "system.info": ScriptedAPI.ANSWERS["system.info"] | {"system_serial": NAS_SERIAL},
        "alert.list": [
            alert(
                "DiskTemperatureTooHot",
                "CRITICAL",
                f"Disk /dev/sda (with serial: {SERIAL}-SDA) critical temperature threshold is 70 degrees celsius and"
                " the current temp is 72 degrees celsius",
                serial=f"{SERIAL}-SDA",
            ),
            alert("SMARTFailedSelfTest", "WARNING", f"nvme1n1 ({SERIAL}-NVME1) failed a SMART selftest."),
            alert(
                "VolumeStatus",
                "CRITICAL",
                f"Pool persistent state is DEGRADED: {MISSING}<br>The following devices are not healthy:"
                f"<ul><li>Disk {MODEL} {SERIAL} is UNAVAIL</li><li>Disk 4242 is REMOVED</li></ul>",
            ),
            alert("Made up", "WARNING", f"The NAS {NAS_SERIAL} holds {MODEL} disks."),
        ],
    }


def test_no_alert_keeps_a_disks_serial_or_model():
    """TrueNAS's alerts quote disks' serials and models: each comes out by the disk's device name, its alert's own
    wording whole, a gone disk's by the name TrueNAS last knew there; the NAS's serial and a model alone are left out.
    An alert's args are never returned."""
    status = info.gather(AlertsAPI(), importable=False, status=True)["status"]
    assert [a["text"] for a in status["alerts"]] == [
        "Disk /dev/sda critical temperature threshold is 70 degrees celsius and the current temp is 72 degrees celsius",
        "nvme1n1 failed a SMART selftest.",
        f"Pool persistent state is DEGRADED: {MISSING}<br>The following devices are not healthy:"
        "<ul><li>Disk nvme2n1 is UNAVAIL</li><li>Disk 4242 is REMOVED</li></ul>",
        "The NAS (serial left out) holds (model left out) disks.",
    ]
    for secret in (SERIAL, MODEL, NAS_SERIAL):
        assert secret not in repr(status), secret


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("sda-SERIAL-1 is ok", "sda-SERIAL-1 is ok", id="inside-a-longer-word"),
        pytest.param("SERIAL-12 and SERIAL-1", "sdb and sda", id="one-the-start-of-another"),
        pytest.param("SERIAL-1.", "sda.", id="before-a-full-stop"),
    ],
)
def test_a_serial_is_matched_whole(text, expected):
    disks = [{"name": "sda", "serial": "SERIAL-1"}, {"name": "sdb", "serial": "SERIAL-12"}]
    assert info.without_serials(text, disks) == expected


def test_each_interface_with_its_type_mtu_and_speed_and_never_its_mac():
    result = info.gather(StatusAPI(), importable=False, status=True)
    assert result["interfaces"] == [
        {
            "name": "eno1",
            "ipv4_dhcp": False,
            "ipv6_auto": False,
            "aliases": [],
            "addresses": [],
            "link_state": "LINK_STATE_UP",
            "type": "PHYSICAL",
            "mtu": 1500,
            "media": "2500Mb/s Twisted Pair",
        }
    ]
    assert LINK_ADDRESS not in repr(result)


class RefusingAPI(ScriptedAPI):
    """ScriptedAPI, refusing the methods it is given as TrueNAS refuses a call the key may not make."""

    def __init__(self, *refused):
        super().__init__()
        self.refused = refused

    def call(self, method, *params, timeout=None):
        if method in self.refused:
            self.calls.append(method)
            raise api.TrueNASCallError(f"{method}: [EPERM] Not authorized")
        return super().call(method, *params, timeout=timeout)


class TimingOutAPI(ScriptedAPI):
    """ScriptedAPI, whose methods given here answer nothing in time, as the client then says."""

    def __init__(self, *slow):
        super().__init__()
        self.slow = slow

    def call(self, method, *params, timeout=None):
        if method in self.slow:
            self.calls.append(method)
            raise api.TrueNASConnectionError(f"{method}: connection to 192.0.2.10 failed: timed out")
        return super().call(method, *params, timeout=timeout)


class NullAPI(ScriptedAPI):
    """ScriptedAPI, answering null to the methods given here, which TrueNAS never does for these."""

    def __init__(self, *null):
        super().__init__()
        self.null = null

    def call(self, method, *params, timeout=None):
        answer = super().call(method, *params, timeout=timeout)
        return None if method in self.null else answer


@pytest.mark.parametrize(
    ("refused", "temperatures", "clients", "unread", "asked"),
    [
        pytest.param(
            ["disk.temperatures"],
            False,
            True,
            {"temperatures": "disk.temperatures: [EPERM] Not authorized"},
            ["disk.temperatures", "nfs.get_nfs3_clients", "nfs.get_nfs4_clients"],
            id="temperatures",
        ),
        pytest.param(
            ["nfs.get_nfs3_clients"],
            True,
            False,
            {"nfs_clients": "nfs.get_nfs3_clients: [EPERM] Not authorized"},
            ["disk.temperatures", "nfs.get_nfs3_clients"],
            id="nfsv3-clients",
        ),
        pytest.param(
            ["nfs.get_nfs4_clients"],
            True,
            False,
            {"nfs_clients": "nfs.get_nfs4_clients: [EPERM] Not authorized"},
            ["disk.temperatures", "nfs.get_nfs3_clients", "nfs.get_nfs4_clients"],
            id="nfsv4-clients",
        ),
    ],
)
def test_a_read_that_fails_on_its_own_leaves_its_part_out(refused, temperatures, clients, unread, asked):
    """The temperatures or the NFS clients refused: that part null, why in unread, and the NAS read all the same, the
    rest of what it says whole."""
    api = RefusingAPI(*refused)
    status = info.gather(api, importable=False, status=True)["status"]
    whole = info.gather(ScriptedAPI(), importable=False, status=True)["status"]
    assert (status["temperatures"] is not None, status["nfs_clients"] is not None) == (temperatures, clients)
    assert status["unread"] == unread
    assert [m for m in api.calls if m.startswith(("disk.temperatures", "nfs."))] == asked
    for key in ("pools", "alerts", "snapshot_tasks", "time_machine", "datetime"):
        assert status[key] == whole[key], key


def test_a_read_that_times_out_leaves_its_part_out():
    """disk.temperatures not answering within its 10 s: the temperatures left out, why said, the rest whole."""
    status = info.gather(TimingOutAPI("disk.temperatures"), importable=False, status=True)["status"]
    assert status["temperatures"] is None and status["nfs_clients"] is not None
    assert status["unread"] == {"temperatures": "disk.temperatures: connection to 192.0.2.10 failed: timed out"}


@pytest.mark.parametrize(
    ("method", "part"),
    [
        ("disk.temperatures", "temperatures"),
        ("nfs.get_nfs3_clients", "nfs_clients"),
        ("nfs.get_nfs4_clients", "nfs_clients"),
    ],
)
def test_a_null_answer_is_said_among_the_reads_that_failed(method, part):
    """A read that answers null leaves its part out as one that fails does, and says so, never a part left out with no
    word of why."""
    status = info.gather(NullAPI(method), importable=False, status=True)["status"]
    assert status[part] is None
    assert status["unread"] == {part: f"{method}: TrueNAS answered null"}


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("192.0.2.20:739", "192.0.2.20"),
        ("[2001:db8::5]:801", "2001:db8::5"),
        ("192.0.2.20", "192.0.2.20"),
        ("2001:db8::5", "2001:db8::5"),
        ("", ""),
        (None, ""),
    ],
)
def test_an_nfs4_clients_address_without_its_port(address, expected):
    assert info._client_address(address) == expected


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


resource = load("truenas_resource", ROLE / "library" / "truenas_resource.py")
permission = load("truenas_permission", ROLE / "library" / "truenas_permission.py")


class TestUpdateTogether:
    """sharing.smb.update refuses options without purpose, and fills options in from their defaults when purpose
    comes alone (TrueNAS 25.10.7's normalize_legacy_fields): the two go to an update together, or not at all."""

    SHARE: ClassVar[dict] = {
        "id": 1,
        "purpose": "TIMEMACHINE_SHARE",
        "enabled": False,
        "options": {"vuid": "d12aafdc", "auto_snapshot": True, "timemachine_quota": 0},
    }
    TOGETHER = ("purpose", "options")

    def updates(self, data):
        return resource.grouped(self.SHARE, data, api.differences(self.SHARE, data), self.TOGETHER)

    def test_another_field_alone_sends_neither(self):
        data = {"purpose": "TIMEMACHINE_SHARE", "enabled": True, "options": {"auto_snapshot": True}}
        assert self.updates(data) == {"enabled": True}

    def test_an_option_sends_the_purpose_and_keeps_what_the_entry_does_not_give(self):
        data = {"purpose": "TIMEMACHINE_SHARE", "options": {"auto_snapshot": False}}
        assert self.updates(data) == {
            "purpose": "TIMEMACHINE_SHARE",
            "options": {"vuid": "d12aafdc", "auto_snapshot": False, "timemachine_quota": 0},
        }

    def test_a_new_purpose_takes_the_entrys_options_alone(self):
        # The current options are the old purpose's; TrueNAS refuses keys the new one does not have.
        data = {"purpose": "LEGACY_SHARE", "options": {"recyclebin": True}}
        assert self.updates(data) == {"purpose": "LEGACY_SHARE", "options": {"recyclebin": True}}

    def test_a_field_of_the_group_the_entry_does_not_give_is_not_sent(self):
        assert self.updates({"options": {"auto_snapshot": False}}) == {
            "options": {"vuid": "d12aafdc", "auto_snapshot": False, "timemachine_quota": 0}
        }

    def test_nothing_differs_nothing_is_sent(self):
        assert self.updates({"purpose": "TIMEMACHINE_SHARE", "options": {"auto_snapshot": True}}) == {}


class TestPermissions:
    def stat(self, mode, acl=False, user="tm-laptop", group="tm-laptop"):
        return {"mode": mode, "acl": acl, "user": user, "group": group, "uid": 3101, "gid": 3101}

    def test_a_directory_compares_by_its_permission_bits(self):
        entry = {"path": "/mnt/p/tm", "user": "tm-laptop", "group": "tm-laptop", "mode": "700"}
        assert permission.current(self.stat(0o40700)) == permission.wanted(entry)
        assert permission.wanted({**entry, "mode": "0700"}) == permission.wanted(entry)

    def test_an_acl_beyond_the_mode_is_a_difference(self):
        entry = {"path": "/mnt/p/tm", "user": "tm-laptop", "group": "tm-laptop", "mode": "700"}
        assert permission.current(self.stat(0o40700, acl=True)) != permission.wanted(entry)
        assert permission.current(self.stat(0o40770)) != permission.wanted(entry)
        assert permission.current(self.stat(0o40700, user="root")) != permission.wanted(entry)

    @pytest.mark.parametrize(
        ("entry", "problem"),
        [
            ({"path": "/mnt/p/tm", "user": "u", "group": "g"}, "needs mode"),
            ({"path": "/mnt/p/tm", "user": "u", "group": "g", "mode": "rwx"}, "is not an octal string"),
            ({"path": "/mnt/p/tm", "user": "u", "group": "g", "mode": 504}, "is not an octal string"),
            ({"path": "/mnt/p/tm", "user": "u", "group": "g", "mode": "2770"}, "is not an octal string"),
            ({"path": "/mnt/p/tm", "user": "u", "group": "g", "mode": "000"}, "is not an octal string"),
            ({"path": "/mnt/p/tm", "user": "u", "group": "g", "mode": "700", "recursive": True}, "unknown field"),
            ({"path": "/mnt/p", "user": "u", "group": "g", "mode": "700"}, "not a directory inside a pool"),
            ({"path": "/srv/p/tm", "user": "u", "group": "g", "mode": "700"}, "not a directory inside a pool"),
            ({"path": "/mnt/p/../q", "user": "u", "group": "g", "mode": "700"}, "not a directory inside a pool"),
        ],
    )
    def test_entries_are_checked_before_connecting(self, entry, problem):
        assert problem in "; ".join(permission.entry_problems([entry]))

    def test_a_good_entry_passes(self):
        entry = {"path": "/mnt/p/tm/laptop", "user": "u", "group": "g", "mode": "0700"}
        assert permission.entry_problems([entry]) == []
