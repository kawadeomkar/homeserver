"""A stateful stand-in for the TrueNAS 25.10 JSON-RPC WebSocket API, for the idempotence tests.

It implements only the methods roles/truenas calls, returns the shapes the real API returns
(checked against TrueNAS 25.10.7 and its middleware source), and refuses what the real API refuses
where a non-idempotent role would trip over it: creating a dataset, share, NTP server or scrub
task that exists, importing a pool that is imported, POSIX ACLs without aclmode DISCARD,
non-consecutive name servers, a snapshot task for a dataset that does not exist. The SMB service, users, SMB shares
and directory permissions follow TrueNAS 25.10.7's middleware source (plugins/smb.py, plugins/account.py,
plugins/filesystem_/acl.py, api/v25_10_5), with its refusals: an SMB share update's options without its purpose, a
Time Machine share without the Apple extensions or mDNS, an SMB user without a password, SMB turned on without the
password, a built-in user's SMB changed, a directory with an ACL changed without stripacl, a mode outside 001 to 777.
It is stricter than TrueNAS in two places: an update's purpose without its options is refused, where 25.10.7 fills the
options in from their defaults (normalize_legacy_fields), which would quietly reset them; and an update whose Time
Machine options name no vuid gets a new one, as the API's description says, where 25.10.7 keeps the old. Users'
password hashes are stand-ins
(password_hash), not TrueNAS's, and the SMB shares know the TIMEMACHINE_SHARE purpose alone. The periodic snapshot
tasks, mail and alert services, alert.list, and the pools' health, capacity and scan follow the TrueNAS API v25.10
reference (api.truenas.com) rather than a capture, times as the API sends them ({"$date": milliseconds}), and
mail.config returns the password as set, as the reference documents. Any other method is an
error, so an unexpected call fails the test.

State lives in a JSON file that is re-read before and written after every call, so a test can
inspect it, or change it to simulate drift, between runs.

    python fake_truenas.py --port 8443 --cert cert.pem --key key.pem --state state.json --address 127.0.0.1
"""

import argparse
import asyncio
import copy
import hashlib
import json
import ssl
import stat
import types
import uuid
from pathlib import Path

from websockets.asyncio.server import serve

API_KEY = "1-fake-api-key-for-tests"
# Stands in for the UI certificate's private key; the tests check it never reaches Ansible output.
PRIVATE_KEY = "FAKE-PRIVATE-KEY-MUST-NOT-LEAK"
# Stands in for the machine's serial number, which system.info returns; just status's report never carries it.
SYSTEM_SERIAL = "FAKE-SYSTEM-SERIAL-MUST-NOT-LEAK"
BOOT_POOL = "boot-pool"
# The user the API key logs in as, an example for the tests and not the project's own setup: a hand-made full
# administrator that is an SMB login, as TrueNAS makes every new user unless told otherwise.
API_USER = "api-admin"
# The shells user.shell_choices offers a user with no extra groups.
SHELLS = (
    "/usr/bin/bash",
    "/usr/bin/rbash",
    "/usr/bin/dash",
    "/usr/bin/sh",
    "/usr/bin/zsh",
    "/usr/bin/tmux",
    "/usr/sbin/nologin",
)
TIB = 2**40
# When the fake's pools were last scrubbed: 2026-10-12 00:00 UTC, three hours long.
SCRUBBED = 1791763200


def date(seconds):
    """A time as the API sends it."""
    return {"$date": int(seconds * 1000)}


def finished_scrub(end=SCRUBBED + 3 * 3600, errors=0):
    """pool.query's scan for a scrub that finished at `end` (seconds since the epoch)."""
    return {
        "function": "SCRUB",
        "state": "FINISHED",
        "start_time": date(end - 3 * 3600),
        "end_time": date(end),
        "percentage": 100.0,
        "bytes_to_process": 2 * TIB,
        "bytes_processed": 2 * TIB,
        "bytes_issued": 2 * TIB,
        "pause": None,
        "errors": errors,
        "total_secs_left": None,
    }


def topology(vdev, guid):
    """pool.query's topology: one data vdev, a RAIDZ of three disks or a disk of its own."""
    stats = {"read_errors": 0, "write_errors": 0, "checksum_errors": 0}
    disk = {"type": "DISK", "status": "ONLINE", "stats": stats, "children": []}
    data = {"name": f"{vdev.lower()}-0", "type": vdev, "guid": guid, "status": "ONLINE", "stats": stats}
    data["children"] = [disk | {"name": f"disk{i}", "guid": f"{guid}{i}"} for i in range(3)] if vdev != "DISK" else []
    return {"data": [data], "log": [], "cache": [], "spare": [], "special": [], "dedup": []}


class CallError(Exception):
    def __init__(self, reason, extra=None, errname="EINVAL"):
        super().__init__(reason)
        self.reason = reason
        self.extra = extra or []
        self.errname = errname


def prop(raw, source="DEFAULT"):
    """A ZFS property as pool.dataset.query and pool.query return it."""
    raw = str(raw)
    return {"parsed": raw, "rawvalue": raw, "value": raw.upper(), "source": source}


def initial_state(address):
    """A freshly installed NAS, as the real one was found: DHCP, pools exported, nothing set up."""
    services = ["cifs", "ftp", "iscsitarget", "nfs", "snmp", "ssh", "ups", "nvmet"]
    return {
        "next_id": 100,
        "jobs": {},
        "address": address,
        "interfaces": {
            "eno1": {"ipv4_dhcp": True, "ipv6_auto": True, "aliases": [], "live": [address]},
        },
        "interface_snapshot": None,
        "checkin_waiting": None,
        "network": {
            "id": 1,
            "hostname": "truenas",
            "domain": "local",
            "ipv4gateway": "",
            "ipv6gateway": "",
            "nameserver1": "",
            "nameserver2": "",
            "nameserver3": "",
            "httpproxy": "",
            "hosts": [],
            "domains": [],
            "service_announcement": {"netbios": True, "mdns": True, "wsd": True},
            "activity": {"type": "DENY", "activities": []},
        },
        "exported_pools": [
            {
                "name": "persistent",
                "guid": "1001",
                "status": "ONLINE",
                "hostname": "",
                "children": [],
                "vdev": "RAIDZ1",
            },
            {
                "name": "ephemeral",
                "guid": "1002",
                "status": "ONLINE",
                "hostname": "",
                "children": ["ephemeral/jellyfin"],
                "vdev": "DISK",
            },
        ],
        # Each pool's root dataset's used and available bytes, which pool.dataset.query reports.
        "space": {"persistent": [int(2.2 * TIB), int(4.9 * TIB)], "ephemeral": [int(2.3 * TIB), int(1.3 * TIB)]},
        "uptime_seconds": 2937600.5,
        "alerts": [],
        "pools": [],
        "datasets": {},
        "scrubs": [],
        # "" is a fresh install: reads as the boot pool with pool_set false.
        "sysds_pool": "",
        "general": {
            "id": 1,
            "ui_certificate": {
                "id": 1,
                "name": "truenas_default",
                "certificate": "FAKE-CERT",
                "privatekey": PRIVATE_KEY,
            },
            "ui_httpsredirect": False,
            "ui_port": 80,
            "ui_httpsport": 443,
            "ui_address": ["0.0.0.0"],
            "timezone": "America/Los_Angeles",
            "kbdmap": "us",
            "usage_collection": None,
        },
        "ntp": [
            {
                "id": i + 1,
                "address": f"{i}.debian.pool.ntp.org",
                "burst": False,
                "iburst": True,
                "prefer": False,
                "minpoll": 6,
                "maxpoll": 10,
            }
            for i in range(3)
        ],
        "services": [
            {"id": i + 1, "service": s, "enable": False, "state": "STOPPED", "pids": []} for i, s in enumerate(services)
        ],
        "nfs": {
            "id": 1,
            "servers": 12,
            "allow_nonroot": False,
            "protocols": ["NFSV3", "NFSV4"],
            "v4_krb": False,
            "v4_domain": "",
            "bindip": [],
            "mountd_port": None,
            "rpcstatd_port": None,
            "rpclockd_port": None,
            "mountd_log": False,
            "statd_lockd_log": False,
            "userd_manage_gids": False,
            "rdma": False,
        },
        "shares": [],
        "snapshot_tasks": [],
        # mail.config as a fresh install has it: no server, no authentication.
        "mail": {
            "id": 1,
            "fromemail": "",
            "fromname": "",
            "outgoingserver": "",
            "port": 25,
            "security": "PLAIN",
            "smtp": False,
            "user": None,
            "pass": None,
            "oauth": None,
        },
        "alert_services": [],
        # smb.config as the home NAS had it before SMB was set up.
        "smb": {
            "id": 1,
            "netbiosname": "truenas",
            "netbiosalias": [],
            "workgroup": "WORKGROUP",
            "description": "TrueNAS Server",
            "enable_smb1": False,
            "unixcharset": "UTF-8",
            "localmaster": True,
            "syslog": False,
            "aapl_extensions": False,
            "admin_group": None,
            "guest": "nobody",
            "filemask": "DEFAULT",
            "dirmask": "DEFAULT",
            "ntlmv1_auth": False,
            "multichannel": False,
            "encryption": "DEFAULT",
            "bindip": [],
            "server_sid": "S-1-5-21-1-2-3",
            "smb_options": "",
            "debug": False,
        },
        "users": initial_users(),
        "groups": initial_groups(),
        "smb_shares": [],
        # A directory's owner, group, mode and whether it carries an ACL beyond its mode, by path; a dataset's own
        # directory that is not listed is root's, 0755, with no ACL.
        "paths": {},
        "mutations": 0,
    }


def password_hash(password):
    """The fake's stand-in for the hashes user.query returns for a password: never TrueNAS's own, but as much a secret
    in the tests, which check that no run's output carries one. A key-derivation function, as a password's hash
    should be, though nothing here protects a real one."""
    return hashlib.pbkdf2_hmac("sha256", password.encode(), b"fake-truenas", 1000).hex()[:32].upper()


def initial_users():
    """A fresh install's root, its first administrator, and the user the API key logs in as."""

    def user(uid, username, builtin, immutable, smb, roles):
        return {
            "id": uid + 1,
            "uid": uid,
            "username": username,
            "full_name": username,
            "builtin": builtin,
            "immutable": immutable,
            "smb": smb,
            "shell": "/usr/bin/zsh",
            "home": "/var/empty",
            "locked": False,
            "password_disabled": False,
            "group": uid,
            "groups": [544] if roles else [],
            "roles": roles,
            "password": password_hash(f"{username}-password"),
        }

    return [
        user(0, "root", True, True, False, ["FULL_ADMIN"]),
        user(950, "truenas_admin", False, True, False, ["FULL_ADMIN"]),
        user(3000, API_USER, False, False, True, ["FULL_ADMIN"]),
    ]


def initial_groups():
    names = {0: "root", 544: "builtin_administrators", 545: "builtin_users", 950: "truenas_admin", 3000: API_USER}
    return [{"id": gid + 1, "gid": gid, "group": name, "builtin": gid in (0, 544, 545)} for gid, name in names.items()]


def matches(item, filters):
    for field, op, value in filters or []:
        if op == "=":
            if item.get(field) != value:
                return False
        elif op == "^":
            if not str(item.get(field) or "").startswith(value):
                return False
        elif op == "in":
            if item.get(field) not in value:
                return False
        else:
            raise CallError(f"filter operator {op!r} not supported by the fake")
    return True


class FakeTrueNAS:
    def __init__(self, state_path, call_log=None):
        self.state_path = Path(state_path)
        # Optional: every call and its answer, one JSON object per line, for checking the role's
        # requests and the fake's answers against the real API's published schemas.
        self.call_log = Path(call_log) if call_log else None

    # ----------------------------------------------------------------- plumbing
    def load(self):
        self.s = json.loads(self.state_path.read_text())

    def save(self):
        self.state_path.write_text(json.dumps(self.s, indent=1, sort_keys=True))

    def new_id(self):
        self.s["next_id"] += 1
        return self.s["next_id"]

    def mutated(self):
        self.s["mutations"] += 1

    def start_job(self, method, func, *args):
        job_id = self.new_id()
        try:
            job = {"id": job_id, "method": method, "state": "SUCCESS", "result": func(*args), "error": None}
        except CallError as exc:
            job = {
                "id": job_id,
                "method": method,
                "state": "FAILED",
                "result": None,
                "error": exc.reason,
                "exc_info": {"type": "VALIDATION" if exc.extra else "CallError", "extra": exc.extra},
            }
        self.s["jobs"][str(job_id)] = job
        return job_id

    # ------------------------------------------------------------------ system
    def m_system_version(self):
        return "TrueNAS-" + self.s.get("version", "25.10.7")

    def m_system_ready(self):
        return True

    def m_system_info(self):
        return {
            "version": self.s.get("version", "25.10.7"),
            "hostname": self.s["network"]["hostname"],
            "uptime_seconds": self.s.get("uptime_seconds", 0.0),
            "system_serial": SYSTEM_SERIAL,
            "system_product": "FAKE",
            "physmem": 64 * 2**30,
        }

    # ------------------------------------------------------------------ alerts
    def m_alert_list(self):
        return copy.deepcopy(self.s.get("alerts", []))

    def m_core_get_jobs(self, filters=None, options=None):
        return [j for j in self.s["jobs"].values() if matches(j, filters)]

    # --------------------------------------------------------------- interfaces
    def iface(self, name):
        cfg = self.s["interfaces"][name]
        live = [
            {"type": "INET", "address": a.split("/")[0], "netmask": int(a.split("/")[1]) if "/" in a else 24}
            for a in cfg["live"]
        ]
        return {
            "id": name,
            "name": name,
            "type": "PHYSICAL",
            "fake": False,
            "description": "",
            "mtu": None,
            "ipv4_dhcp": cfg["ipv4_dhcp"],
            "ipv6_auto": cfg["ipv6_auto"],
            "aliases": [] if cfg["ipv4_dhcp"] else copy.deepcopy(cfg["aliases"]),
            "state": {"name": name, "aliases": live, "link_state": "LINK_STATE_UP"},
        }

    def m_interface_query(self, filters=None, options=None):
        return [i for i in (self.iface(n) for n in self.s["interfaces"]) if matches(i, filters)]

    def m_interface_websocket_interface(self):
        return self.iface("eno1")

    def m_interface_websocket_local_ip(self):
        return self.s["address"]

    def m_interface_has_pending_changes(self):
        return self.s["interface_snapshot"] is not None

    def m_interface_checkin_waiting(self):
        return self.s["checkin_waiting"]

    def m_interface_update(self, name, data):
        if name not in self.s["interfaces"]:
            raise CallError(f"{name}: no such interface", errname="ENOENT")
        unknown = set(data) - {"ipv4_dhcp", "ipv6_auto", "aliases", "description", "mtu"}
        if unknown:
            raise CallError(f"interface.update: unexpected fields {sorted(unknown)}")
        for alias in data.get("aliases", []):
            if not isinstance(alias.get("netmask"), int) or "address" not in alias:
                raise CallError("interface_update.aliases: each alias needs address and an integer netmask")
        if self.s["interface_snapshot"] is None:
            self.s["interface_snapshot"] = {
                "interfaces": copy.deepcopy(self.s["interfaces"]),
                "ipv4gateway": self.s["network"]["ipv4gateway"],
            }
        self.s["interfaces"][name].update({k: v for k, v in data.items() if k in ("ipv4_dhcp", "ipv6_auto", "aliases")})
        self.mutated()
        return self.iface(name)

    def m_interface_commit(self, options):
        if not any(c["ipv4_dhcp"] or c["aliases"] for c in self.s["interfaces"].values()):
            raise CallError("At least one interface must be configured with IPv4 DHCP or a static IP.")
        for cfg in self.s["interfaces"].values():
            if not cfg["ipv4_dhcp"]:
                cfg["live"] = [
                    f"{a['address']}/{a['netmask']}" for a in cfg["aliases"] if a.get("type", "INET") == "INET"
                ]
        if options.get("rollback", True) and options.get("checkin_timeout", 60):
            self.s["checkin_waiting"] = options.get("checkin_timeout", 60)
        else:
            self.s["interface_snapshot"] = None
        self.mutated()

    def m_interface_checkin(self):
        self.s["interface_snapshot"] = None
        self.s["checkin_waiting"] = None
        self.mutated()

    def m_interface_rollback(self):
        snap = self.s["interface_snapshot"]
        if snap:
            self.s["interfaces"] = snap["interfaces"]
            self.s["network"]["ipv4gateway"] = snap["ipv4gateway"]
        self.s["interface_snapshot"] = None
        self.s["checkin_waiting"] = None
        self.mutated()

    def m_network_configuration_config(self):
        return {**copy.deepcopy(self.s["network"]), "hostname_local": self.s["network"]["hostname"], "state": {}}

    def m_network_configuration_update(self, data):
        new = {**self.s["network"], **data}
        servers = [new["nameserver1"], new["nameserver2"], new["nameserver3"]]
        if any(servers[i] and not servers[i - 1] for i in (1, 2)):
            raise CallError("network_configuration_update: name servers must be set in order")
        unknown = set(data) - set(self.s["network"])
        if unknown:
            raise CallError(f"network.configuration.update: unexpected fields {sorted(unknown)}")
        self.s["network"] = new
        self.mutated()
        return self.m_network_configuration_config()

    # -------------------------------------------------------------------- pools
    def pool_obj(self, pool):
        return copy.deepcopy(pool)

    def m_pool_query(self, filters=None, options=None):
        return [self.pool_obj(p) for p in self.s["pools"] if matches(p, filters)]

    def m_pool_import_find(self):
        return self.start_job(
            "pool.import_find",
            lambda: [{k: p[k] for k in ("name", "guid", "status", "hostname")} for p in self.s["exported_pools"]],
        )

    def m_pool_import_pool(self, data):
        return self.start_job("pool.import_pool", self._import_pool, data)

    def _import_pool(self, data):
        guid = data["guid"]
        if any(p["guid"] == guid for p in self.s["pools"]):
            raise CallError(f'Pool with guid: "{guid}" already imported', errname="EEXIST")
        found = [p for p in self.s["exported_pools"] if p["guid"] == guid]
        if not found:
            raise CallError("Pool not found", errname="ENOENT")
        exported = found[0]
        self.s["exported_pools"].remove(exported)
        pool_id = self.new_id()
        name = exported["name"]
        used, available = self.s.get("space", {}).get(name, [0, TIB])
        self.s["pools"].append(
            {
                "id": pool_id,
                "name": name,
                "guid": guid,
                "status": "ONLINE",
                "healthy": True,
                "warning": False,
                "status_code": "OK",
                "status_detail": None,
                "path": f"/mnt/{name}",
                "scan": finished_scrub(),
                "size": int((used + available) * 1.05),
                "allocated": used,
                "free": int((used + available) * 1.05) - used,
                "fragmentation": "2",
                "autotrim": prop("off"),
                "topology": topology(exported.get("vdev", "DISK"), guid),
            }
        )
        for ds in [name, *exported["children"]]:
            self.s["datasets"][ds] = self.new_dataset(
                ds, {"compression": "lz4", "atime": "off", "acltype": "nfsv4", "aclmode": "passthrough"}, source="LOCAL"
            )
        # The real import creates a default scrub task...
        self.s["scrubs"].append(
            {
                "id": self.new_id(),
                "pool": pool_id,
                "pool_name": name,
                "threshold": 35,
                "description": "",
                "enabled": True,
                "schedule": {"minute": "00", "hour": "00", "dom": "*", "month": "*", "dow": "7"},
            }
        )
        # ...and its post-import hook moves an unchosen system dataset onto the pool.
        if not self.s["sysds_pool"]:
            self.s["sysds_pool"] = name
        self.mutated()
        return True

    def m_pool_update(self, pool_id, data):
        return self.start_job("pool.update", self._pool_update, pool_id, data)

    def _pool_update(self, pool_id, data):
        pool = next((p for p in self.s["pools"] if p["id"] == pool_id), None)
        if pool is None:
            raise CallError(f"pool {pool_id} does not exist", errname="ENOENT")
        if set(data) - {"autotrim"}:
            raise CallError(f"pool.update: field(s) {sorted(set(data) - {'autotrim'})} not supported by the fake")
        if data.get("autotrim") not in ("ON", "OFF"):
            raise CallError("pool_update.autotrim: must be ON or OFF")
        pool["autotrim"] = prop(data["autotrim"].lower(), "LOCAL")
        self.mutated()
        return self.pool_obj(pool)

    def m_pool_scrub_query(self, filters=None, options=None):
        return [copy.deepcopy(t) for t in self.s["scrubs"] if matches(t, filters)]

    def m_pool_scrub_create(self, data):
        if any(t["pool"] == data["pool"] for t in self.s["scrubs"]):
            raise CallError("pool_scrub_create.pool: A scrub with this pool already exists", errname="EEXIST")
        pool = next((p for p in self.s["pools"] if p["id"] == data["pool"]), None)
        if pool is None:
            raise CallError("pool_scrub_create.pool: no such pool")
        task = {
            "id": self.new_id(),
            "pool_name": pool["name"],
            "threshold": 35,
            "description": "",
            "enabled": True,
            "schedule": {"minute": "00", "hour": "00", "dom": "*", "month": "*", "dow": "7"},
            **data,
        }
        self.s["scrubs"].append(task)
        self.mutated()
        return copy.deepcopy(task)

    def m_pool_scrub_update(self, task_id, data):
        task = next((t for t in self.s["scrubs"] if t["id"] == task_id), None)
        if task is None:
            raise CallError(f"scrub task {task_id} does not exist", errname="ENOENT")
        task.update(data)
        self.mutated()
        return copy.deepcopy(task)

    def m_disk_details(self, options=None):
        return {"used": [], "unused": []}

    # ----------------------------------------------------------- system dataset
    def m_systemdataset_config(self):
        pool = self.s["sysds_pool"] or BOOT_POOL
        return {
            "id": 1,
            "pool": pool,
            "pool_set": bool(self.s["sysds_pool"]),
            "uuid": "fake",
            "basename": f"{pool}/.system",
            "path": "/var/db/system",
        }

    def m_systemdataset_update(self, data):
        return self.start_job("systemdataset.update", self._sysds_update, data)

    def _sysds_update(self, data):
        pool = data.get("pool")
        if pool not in [BOOT_POOL, *[p["name"] for p in self.s["pools"]]]:
            raise CallError("sysdataset_update.pool: The system dataset cannot be placed on this pool.")
        self.s["sysds_pool"] = pool
        self.mutated()
        return self.m_systemdataset_config()

    # ------------------------------------------------------------------ general
    def m_system_general_config(self):
        return copy.deepcopy(self.s["general"])

    def m_system_general_update(self, data):
        data = dict(data)
        data.pop("ui_restart_delay", None)
        data.pop("rollback_timeout", None)
        unknown = set(data) - set(self.s["general"])
        if unknown:
            raise CallError(f"system.general.update: unexpected fields {sorted(unknown)}")
        self.s["general"].update(data)
        self.mutated()
        return self.m_system_general_config()

    # --------------------------------------------------------------------- NTP
    def m_system_ntpserver_query(self, filters=None, options=None):
        return [copy.deepcopy(n) for n in self.s["ntp"] if matches(n, filters)]

    def m_system_ntpserver_create(self, data):
        if any(n["address"] == data["address"] for n in self.s["ntp"]):
            raise CallError("ntp_server_create.address: this server already exists", errname="EEXIST")
        server = {
            "id": self.new_id(),
            "burst": False,
            "iburst": True,
            "prefer": False,
            "minpoll": 6,
            "maxpoll": 10,
            **data,
        }
        self.s["ntp"].append(server)
        self.mutated()
        return copy.deepcopy(server)

    def m_system_ntpserver_update(self, server_id, data):
        server = next((n for n in self.s["ntp"] if n["id"] == server_id), None)
        if server is None:
            raise CallError("no such NTP server", errname="ENOENT")
        server.update(data)
        self.mutated()
        return copy.deepcopy(server)

    # --------------------------------------------------------------- snapshots
    # As pool.snapshottask.create takes and returns them (TrueNAS API v25.10). The dataset must exist.
    SNAPSHOT_TASK_FIELDS = frozenset(
        {
            "dataset",
            "recursive",
            "lifetime_value",
            "lifetime_unit",
            "enabled",
            "exclude",
            "naming_schema",
            "allow_empty",
            "schedule",
        }
    )
    SCHEDULE_FIELDS = frozenset({"minute", "hour", "dom", "month", "dow", "begin", "end"})
    SCHEDULE_DEFAULTS = types.MappingProxyType(
        {"minute": "00", "hour": "*", "dom": "*", "month": "*", "dow": "*", "begin": "00:00", "end": "23:59"}
    )

    def check_snapshot_task(self, data, where):
        unknown = set(data) - self.SNAPSHOT_TASK_FIELDS
        if unknown:
            raise CallError(f"{where}: unexpected fields {sorted(unknown)}")
        if set(data.get("schedule") or {}) - self.SCHEDULE_FIELDS:
            raise CallError(
                f"{where}.schedule: unexpected fields {sorted(set(data['schedule']) - self.SCHEDULE_FIELDS)}"
            )
        if "lifetime_unit" in data and data["lifetime_unit"] not in ("HOUR", "DAY", "WEEK", "MONTH", "YEAR"):
            raise CallError(f"{where}.lifetime_unit: Input should be 'HOUR', 'DAY', 'WEEK', 'MONTH' or 'YEAR'")
        if "dataset" in data and data["dataset"] not in self.s["datasets"]:
            raise CallError(f"{where}.dataset: Dataset not found", errname="ENOENT")

    def m_pool_snapshottask_query(self, filters=None, options=None):
        return [copy.deepcopy(t) for t in self.s.setdefault("snapshot_tasks", []) if matches(t, filters)]

    def m_pool_snapshottask_create(self, data):
        self.check_snapshot_task(data, "pool_snapshottask_create")
        task = {
            "id": self.new_id(),
            "recursive": False,
            "lifetime_value": 2,
            "lifetime_unit": "WEEK",
            "enabled": True,
            "exclude": [],
            "naming_schema": "auto-%Y-%m-%d_%H-%M",
            "allow_empty": True,
            "vmware_sync": False,
            "state": {"state": "PENDING"},
            **data,
            "schedule": {**self.SCHEDULE_DEFAULTS, **(data.get("schedule") or {})},
        }
        self.s.setdefault("snapshot_tasks", []).append(task)
        self.mutated()
        return copy.deepcopy(task)

    def m_pool_snapshottask_update(self, task_id, data):
        task = next((t for t in self.s.setdefault("snapshot_tasks", []) if t["id"] == task_id), None)
        if task is None:
            raise CallError(f"periodic snapshot task {task_id} does not exist", errname="ENOENT")
        self.check_snapshot_task(data, "pool_snapshottask_update")
        task.update({**data, "schedule": {**task["schedule"], **(data.get("schedule") or {})}})
        self.mutated()
        return copy.deepcopy(task)

    # -------------------------------------------------------------------- mail
    # mail.config returns the password as it was set (TrueNAS API v25.10: pass is a string or null).
    def m_mail_config(self):
        return copy.deepcopy(self.s["mail"])

    def m_mail_update(self, data):
        unknown = set(data) - (set(self.s["mail"]) - {"id"})
        if unknown:
            raise CallError(f"mail_update: unexpected fields {sorted(unknown)}")
        if data.get("security", self.s["mail"]["security"]) not in ("PLAIN", "SSL", "TLS"):
            raise CallError("mail_update.security: Input should be 'PLAIN', 'SSL' or 'TLS'")
        self.s["mail"].update(data)
        self.mutated()
        return self.m_mail_config()

    # ------------------------------------------------------------ alertservice
    ALERT_LEVELS = ("INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL", "ALERT", "EMERGENCY")

    def check_alert_service(self, data, where):
        unknown = set(data) - {"name", "attributes", "level", "enabled"}
        if unknown:
            raise CallError(f"{where}: unexpected fields {sorted(unknown)}")
        attributes = data.get("attributes")
        if attributes is not None and (attributes.get("type") != "Mail" or set(attributes) - {"type", "email"}):
            raise CallError(f"{where}.attributes: the fake knows only the Mail service ({{type: Mail, email}})")
        if "level" in data and data["level"] not in self.ALERT_LEVELS:
            raise CallError(f"{where}.level: Input should be one of {self.ALERT_LEVELS}")

    def m_alertservice_query(self, filters=None, options=None):
        return [copy.deepcopy(a) for a in self.s.setdefault("alert_services", []) if matches(a, filters)]

    def m_alertservice_create(self, data):
        self.check_alert_service(data, "alert_service_create")
        if not {"name", "attributes", "level"} <= set(data):
            raise CallError("alert_service_create: name, attributes and level are required")
        service = {"id": self.new_id(), "enabled": True, "type__title": "Email", **data}
        self.s.setdefault("alert_services", []).append(service)
        self.mutated()
        return copy.deepcopy(service)

    def m_alertservice_update(self, service_id, data):
        service = next((a for a in self.s.setdefault("alert_services", []) if a["id"] == service_id), None)
        if service is None:
            raise CallError(f"alert service {service_id} does not exist", errname="ENOENT")
        self.check_alert_service(data, "alert_service_update")
        # TrueNAS 25.10's alertservice.update takes the full AlertServiceCreate model, unlike most updates.
        missing = sorted({"name", "attributes", "level"} - set(data))
        if missing:
            raise CallError(f"alert_service_update.{missing[0]}: Field required")
        service.update(data)
        self.mutated()
        return copy.deepcopy(service)

    # ---------------------------------------------------------------- services
    def m_service_query(self, filters=None, options=None):
        return [copy.deepcopy(s) for s in self.s["services"] if matches(s, filters)]

    def m_service_update(self, id_or_name, data):
        service = next((s for s in self.s["services"] if id_or_name in (s["id"], s["service"])), None)
        if service is None:
            raise CallError(f"no service {id_or_name}", errname="ENOENT")
        if set(data) != {"enable"}:
            raise CallError("service_update: only enable can be set")
        service["enable"] = data["enable"]
        self.mutated()
        return service["id"]

    def m_service_control(self, verb, name, options=None):
        return self.start_job("service.control", self._service_control, verb, name)

    def _service_control(self, verb, name):
        service = next((s for s in self.s["services"] if s["service"] == name), None)
        if service is None:
            raise CallError(f"no service {name}", errname="ENOENT")
        service["state"] = {"START": "RUNNING", "RESTART": "RUNNING", "STOP": "STOPPED"}.get(verb, service["state"])
        self.mutated()
        return True

    # --------------------------------------------------------------------- NFS
    def m_nfs_config(self):
        return copy.deepcopy(self.s["nfs"])

    def m_nfs_update(self, data):
        unknown = set(data) - set(self.s["nfs"])
        if unknown:
            raise CallError(f"nfs.update: unexpected fields {sorted(unknown)}")
        self.s["nfs"].update(data)
        self.mutated()
        return self.m_nfs_config()

    def m_sharing_nfs_query(self, filters=None, options=None):
        return [copy.deepcopy(s) for s in self.s["shares"] if matches(s, filters)]

    SHARE_FIELDS = frozenset(
        {
            "path",
            "aliases",
            "comment",
            "networks",
            "hosts",
            "ro",
            "maproot_user",
            "maproot_group",
            "mapall_user",
            "mapall_group",
            "security",
            "enabled",
            "expose_snapshots",
        }
    )

    def m_sharing_nfs_create(self, data):
        unknown = set(data) - self.SHARE_FIELDS
        if unknown:
            raise CallError(f"sharing_nfs_create: unexpected fields {sorted(unknown)}")
        path = data["path"]
        if not path.startswith("/mnt/") or path[5:] not in self.s["datasets"]:
            raise CallError(f"sharing_nfs_create.path: {path}: export path is not the root directory of a dataset.")
        if any(s["path"] == path for s in self.s["shares"]):
            raise CallError(
                f"sharing_nfs_create.path: ERROR - Export conflict. Another share exports {path}", errname="EEXIST"
            )
        share = {
            "id": self.new_id(),
            "aliases": [],
            "comment": "",
            "networks": [],
            "hosts": [],
            "ro": False,
            "maproot_user": None,
            "maproot_group": None,
            "mapall_user": None,
            "mapall_group": None,
            "security": [],
            "enabled": True,
            "expose_snapshots": False,
            "locked": False,
            **data,
        }
        self.s["shares"].append(share)
        self.mutated()
        return copy.deepcopy(share)

    def m_sharing_nfs_update(self, share_id, data):
        share = next((s for s in self.s["shares"] if s["id"] == share_id), None)
        if share is None:
            raise CallError("no such share", errname="ENOENT")
        unknown = set(data) - self.SHARE_FIELDS
        if unknown:
            raise CallError(f"sharing_nfs_update: unexpected fields {sorted(unknown)}")
        share.update(data)
        self.mutated()
        return copy.deepcopy(share)

    # ---------------------------------------------------------------- datasets
    INHERITED = (
        "sync",
        "compression",
        "atime",
        "recordsize",
        "acltype",
        "aclmode",
        "exec",
        "readonly",
        "snapdir",
        "deduplication",
        "copies",
    )
    DEFAULTS = types.MappingProxyType(
        {
            "sync": "standard",
            "compression": "lz4",
            "atime": "on",
            "recordsize": "131072",
            "acltype": "posix",
            "aclmode": "discard",
            "exec": "on",
            "readonly": "off",
            "snapdir": "hidden",
            "deduplication": "off",
            "copies": "1",
            "quota": "0",
            "refquota": "0",
            "reservation": "0",
            "refreservation": "0",
        }
    )

    def new_dataset(self, name, local, source="LOCAL"):
        parent = self.s["datasets"].get(name.rsplit("/", 1)[0]) if "/" in name else None
        ds = {
            "id": name,
            "name": name,
            "pool": name.split("/")[0],
            "type": "FILESYSTEM",
            "mountpoint": f"/mnt/{name}",
            "encrypted": False,
            "locked": False,
        }
        for key, default in self.DEFAULTS.items():
            if key in local:
                ds[key] = prop(local[key], source)
            elif parent and key in self.INHERITED:
                ds[key] = prop(parent[key]["rawvalue"], "INHERITED")
            else:
                ds[key] = prop(default, "DEFAULT")
        return ds

    @staticmethod
    def raw_props(data):
        """API values (STANDARD, 64K, 1099511627776) to ZFS raw values (standard, 65536, ...)."""
        out = {}
        for key, value in data.items():
            if key == "recordsize":
                units = {"K": 1024, "M": 1024**2}
                out[key] = str(int(value[:-1]) * units[value[-1].upper()]) if value[-1].upper() in units else str(value)
            elif key in ("quota", "refquota", "reservation", "refreservation"):
                out[key] = str(value or 0)
            else:
                out[key] = str(value).lower()
        return out

    def check_acl(self, name, props):
        acltype = props.get("acltype") or self.s["datasets"].get(name, {}).get("acltype", {}).get("rawvalue")
        aclmode = props.get("aclmode") or self.s["datasets"].get(name, {}).get("aclmode", {}).get("rawvalue")
        if acltype in ("posix", "off") and aclmode and aclmode != "discard":
            raise CallError(
                "aclmode: Must be set to DISCARD when acltype is POSIX or OFF", [["aclmode", "Must be DISCARD", 22]]
            )

    PROPERTY_FIELDS = frozenset(DEFAULTS) | {"comments"}

    def m_pool_dataset_query(self, filters=None, options=None):
        out = [copy.deepcopy(d) for d in self.s["datasets"].values() if matches(d, filters)]
        # A dataset in "space" reports its used and available bytes, as every dataset does on the real NAS, and one in
        # "snapshot_space" what its snapshots hold.
        for ds in out:
            sizes = dict(zip(("used", "available"), self.s.get("space", {}).get(ds["id"], ()), strict=False))
            if ds["id"] in self.s.get("snapshot_space", {}):
                sizes["usedbysnapshots"] = self.s["snapshot_space"][ds["id"]]
            for key, value in sizes.items():
                ds[key] = {"parsed": value, "rawvalue": str(value), "value": f"{value / TIB:.2f}T", "source": "NONE"}
        return out

    # The snapshots the fake holds, by dataset: names alone, as "<dataset>@<name>", in state["snapshots"]. As
    # zfs.snapshot.query does at TS-25.10.7 (plugins/zfs_/snapshot.py): a select of name alone, filtered on name, pool
    # or dataset alone, is its fast path, which lists the snapshots without reading a property, so its entries have no
    # properties; asked nothing more than that select and = or in on pool or dataset, it returns them whole, the select
    # never applied. Any other query reads every snapshot with its properties, then filters and selects.
    def m_pool_snapshot_query(self, filters=None, options=None):
        filters, options = filters or [], options or {}
        fast = options.get("select") == ["name"] and {f[0] for f in filters} <= {"name", "pool", "dataset"}
        whole = fast and len(options) == 1 and all(f[0] in ("pool", "dataset") and f[1] in ("=", "in") for f in filters)
        out = []
        for full in self.s.get("snapshots", []):
            dataset, name = full.split("@", 1)
            snap = {
                "pool": dataset.split("/")[0],
                "name": full,
                "type": "SNAPSHOT",
                "snapshot_name": name,
                "dataset": dataset,
                "id": full,
                "createtxg": "1",
            }
            if not fast:
                snap["properties"] = {}
            if matches(snap, filters):
                out.append(snap)
        select = options.get("select")
        return [{key: snap[key] for key in select if key in snap} for snap in out] if select and not whole else out

    def m_pool_dataset_create(self, data):
        data = dict(data)
        name = data.pop("name")
        if data.pop("type", "FILESYSTEM") != "FILESYSTEM":
            raise CallError("the fake only creates filesystems")
        share_type = data.pop("share_type", "GENERIC")
        ancestors = data.pop("create_ancestors", False)
        unknown = set(data) - self.PROPERTY_FIELDS
        if unknown:
            raise CallError(f"pool_dataset_create: unexpected fields {sorted(unknown)}")
        if name in self.s["datasets"]:
            raise CallError(f"pool_dataset_create.name: Path /mnt/{name} already exists", errname="EEXIST")
        parent = name.rsplit("/", 1)[0]
        if "/" not in name or (parent not in self.s["datasets"] and not ancestors):
            raise CallError(f"pool_dataset_create.name: parent {parent} does not exist", errname="ENOENT")
        if share_type == "SMB":
            # The SMB preset: NFSv4 ACLs, RESTRICTED, and the template ACL on the new dataset's directory, in which
            # every SMB login may modify and the administrators have full control.
            data = {"acltype": "NFSV4", "aclmode": "RESTRICTED", **data}
            self.s.setdefault("paths", {})[f"/mnt/{name}"] = {"uid": 0, "gid": 544, "mode": 0o770, "acl": True}
        props = self.raw_props(data)
        self.check_acl(name, props)
        self.s["datasets"][name] = self.new_dataset(name, props)
        self.mutated()
        return copy.deepcopy(self.s["datasets"][name])

    def m_pool_dataset_update(self, name, data):
        if name not in self.s["datasets"]:
            raise CallError(f"{name} does not exist", errname="ENOENT")
        unknown = set(data) - self.PROPERTY_FIELDS
        if unknown:
            raise CallError(f"pool_dataset_update: unexpected fields {sorted(unknown)}")
        props = self.raw_props(data)
        self.check_acl(name, props)
        for key, value in props.items():
            self.s["datasets"][name][key] = prop(value, "LOCAL")
        self.mutated()
        return copy.deepcopy(self.s["datasets"][name])

    # --------------------------------------------------------------------- SMB
    def m_smb_config(self):
        return copy.deepcopy(self.s["smb"])

    def tm_shares(self):
        return [sh for sh in self.s.setdefault("smb_shares", []) if sh["purpose"] == "TIMEMACHINE_SHARE"]

    def m_smb_update(self, data):
        unknown = set(data) - (set(self.s["smb"]) - {"id", "server_sid"})
        if unknown:
            raise CallError(f"smb_update: unexpected fields {sorted(unknown)}")
        new = {**self.s["smb"], **data}
        if new["encryption"] not in ("DEFAULT", "NEGOTIATE", "DESIRED", "REQUIRED"):
            raise CallError("smb_update.encryption: Input should be 'NEGOTIATE', 'DESIRED', 'REQUIRED' or 'DEFAULT'")
        if new["enable_smb1"] and new["encryption"] == "REQUIRED":
            raise CallError(
                "smb_update.encryption: Encryption may not be set to REQUIRED while SMB1 support is enabled."
            )
        if not new["aapl_extensions"] and self.tm_shares():
            raise CallError(
                "smb_update.aapl_extensions: This option must be enabled when AFP, time machine, or Final Cut Pro "
                "shares are present"
            )
        self.s["smb"] = new
        self.mutated()
        return self.m_smb_config()

    # ------------------------------------------------------------------- users
    # user.create's fields (TrueNAS API v25.10.5's UserCreate); user.update takes them less uid and group_create.
    USER_CREATE_FIELDS = frozenset(
        {
            "username",
            "uid",
            "full_name",
            "email",
            "password",
            "password_disabled",
            "group",
            "group_create",
            "groups",
            "home",
            "home_create",
            "home_mode",
            "shell",
            "smb",
            "sudo_commands",
            "sudo_commands_nopasswd",
            "locked",
            "ssh_password_enabled",
            "sshpubkey",
            "userns_idmap",
            "random_password",
        }
    )
    USER_UPDATE_FIELDS = USER_CREATE_FIELDS - {"uid", "group_create"}

    def group_of(self, gid):
        return next((g for g in self.s["groups"] if g["gid"] == gid), None)

    def user_obj(self, user):
        """A user as user.query returns it, hashes included."""
        hashed = user["password"] or "*"
        group = self.group_of(user["group"]) or {}
        return {
            **{k: v for k, v in user.items() if k not in ("password", "group")},
            "unixhash": f"$6$fake${hashed}" if user["password"] else "*",
            "smbhash": hashed,
            "group": {"id": group.get("id"), "bsdgrp_gid": group.get("gid"), "bsdgrp_group": group.get("group")},
            "local": True,
            "sid": f"S-1-5-21-1-2-3-{20000 + user['uid']}" if user["smb"] else None,
            "email": None,
            "sshpubkey": None,
            "ssh_password_enabled": False,
            "sudo_commands": [],
            "sudo_commands_nopasswd": [],
            "userns_idmap": None,
            "twofactor_auth_configured": False,
            "last_password_change": None,
            "password_age": None,
            "password_history": [hashed],
            "password_change_required": False,
            "api_keys": [1] if user["username"] == API_USER else [],
        }

    def m_user_query(self, filters=None, options=None):
        found = [obj for obj in (self.user_obj(u) for u in self.s["users"]) if matches(obj, filters)]
        select = (options or {}).get("select")
        return [{key: obj[key] for key in select if key in obj} for obj in found] if select else found

    def m_auth_me(self):
        user = next(u for u in self.s["users"] if u["username"] == API_USER)
        return {
            "pw_name": user["username"],
            "pw_gecos": user["full_name"],
            "pw_dir": user["home"],
            "pw_shell": user["shell"],
            "pw_uid": user["uid"],
            "pw_gid": user["group"],
            "grouplist": None,
            "sid": None,
            "source": "LOCAL",
            "local": True,
            "attributes": {},
            "two_factor_config": {"secret_configured": False},
            "privilege": {"roles": {"$set": ["FULL_ADMIN"]}},
            "account_attributes": ["LOCAL", "SYS_ADMIN"],
        }

    def check_user(self, combined, data, where):
        if combined["smb"] and combined["password_disabled"]:
            raise CallError(f"{where}.password_disabled: Password authentication may not be disabled for SMB users.")
        if data.get("password_disabled") and data.get("password"):
            raise CallError(
                f'{where}.password_disabled: Leave "Password" blank when "Disable password login" is checked.'
            )
        if combined["shell"] not in SHELLS:
            raise CallError(f"{where}.shell: Please select a valid shell.")

    def m_user_create(self, data):
        unknown = set(data) - self.USER_CREATE_FIELDS
        if unknown:
            raise CallError(f"user_create: Extra inputs are not permitted: {sorted(unknown)}")
        name = data.get("username")
        if any(u["username"] == name for u in self.s["users"]):
            raise CallError(f'user_create.username: The username "{name}" already exists.', errname="EEXIST")
        if not data.get("full_name"):
            raise CallError("user_create.full_name: Field required")
        if bool(data.get("group")) == bool(data.get("group_create")):
            raise CallError("user_create.group: Enter either a group name or create a new group to continue.")
        if not data.get("password") and not data.get("password_disabled"):
            raise CallError("user_create.password: Password is required")
        uid = data.get("uid")
        if uid is None:
            uid = max([3000, *(u["uid"] + 1 for u in self.s["users"])])
        elif any(u["uid"] == uid for u in self.s["users"]):
            raise CallError(f"user_create.uid: Uid {uid} is already used", errname="EEXIST")
        user = {
            "id": self.new_id(),
            "uid": uid,
            "username": name,
            "full_name": data["full_name"],
            "builtin": False,
            "immutable": False,
            "smb": data.get("smb", True),
            "shell": data.get("shell", "/usr/bin/zsh"),
            "home": data.get("home", "/var/empty"),
            "locked": data.get("locked", False),
            "password_disabled": data.get("password_disabled", False),
            "groups": list(data.get("groups", [])),
            "roles": [],
            "password": password_hash(data["password"]) if data.get("password") else None,
        }
        self.check_user(user, data, "user_create")
        if data.get("group_create"):
            group = next((g for g in self.s["groups"] if g["group"] == name), None)
            if group is None:
                gid = max(3000, *(g["gid"] + 1 for g in self.s["groups"]))
                group = {"id": self.new_id(), "gid": gid, "group": name, "builtin": False}
                self.s["groups"].append(group)
            user["group"] = group["gid"]
        else:
            user["group"] = data["group"]
        if user["smb"]:
            user["groups"].append(545)
        self.s["users"].append(user)
        self.mutated()
        # As TrueNAS does: the new user, and the password it was given.
        return {**self.user_obj(user), "password": data.get("password")}

    def m_user_update(self, user_id, data):
        unknown = set(data) - self.USER_UPDATE_FIELDS
        if unknown:
            raise CallError(f"user_update: Extra inputs are not permitted: {sorted(unknown)}")
        user = next((u for u in self.s["users"] if u["id"] == user_id), None)
        if user is None:
            raise CallError(f"user {user_id} does not exist", errname="ENOENT")
        if user["immutable"]:
            for key in ("group", "home", "username", "smb"):
                if key in data and data[key] != user[key]:
                    raise CallError(f"user_update.{key}: This attribute cannot be changed")
        if not user["smb"] and data.get("smb") and not data.get("password"):
            raise CallError("user_update.smb: Password must be reset in order to enable SMB authentication")
        combined = {**user, **{k: v for k, v in data.items() if k != "password"}}
        self.check_user(combined, data, "user_update")
        # A test's stand-in for a change that did not take: the users named in smb_kept stay SMB logins.
        if user["username"] in self.s.get("smb_kept", []):
            data = {k: v for k, v in data.items() if k != "smb"}
        user.update({k: v for k, v in data.items() if k in user and k != "password"})
        if data.get("password"):
            user["password"] = password_hash(data["password"])
        if user["smb"] and 545 not in user["groups"]:
            user["groups"].append(545)
        self.mutated()
        return {**self.user_obj(user), "password": data.get("password")}

    # -------------------------------------------------------------- SMB shares
    SMB_SHARE_FIELDS = frozenset(
        {
            "purpose",
            "name",
            "path",
            "enabled",
            "comment",
            "readonly",
            "browsable",
            "access_based_share_enumeration",
            "audit",
            "options",
        }
    )
    TIMEMACHINE_OPTIONS = types.MappingProxyType(
        {
            "timemachine_quota": 0,
            "auto_snapshot": False,
            "auto_dataset_creation": False,
            "dataset_naming_schema": None,
            "vuid": None,
            "hostsallow": [],
            "hostsdeny": [],
        }
    )

    def check_smb_share(self, data, where):
        """sharing.smb's model and validation, for what the fake knows: a Time Machine share."""
        unknown = set(data) - self.SMB_SHARE_FIELDS
        if unknown:
            raise CallError(f"{where}: Extra inputs are not permitted: {sorted(unknown)}")
        # api/v25_10_5/smb.py refuses options without purpose. Purpose without options it fills in from their defaults
        # (normalize_legacy_fields); the fake refuses that too, so no test passes on an update that would reset them.
        if "purpose" in data and "options" not in data:
            raise CallError(f"{where}: Value error, You must set `options` if you set `purpose`.")
        if "options" in data and "purpose" not in data:
            raise CallError(f"{where}: Value error, You must set `purpose` if you set `options`.")
        if "options" in data and set(data["options"] or {}) - set(self.TIMEMACHINE_OPTIONS):
            extra = sorted(set(data["options"]) - set(self.TIMEMACHINE_OPTIONS))
            raise CallError(f"{where}.options: Extra inputs are not permitted: {extra}")

    def validate_smb_share(self, share, where):
        if share["purpose"] != "TIMEMACHINE_SHARE":
            raise CallError(f"{where}.purpose: the fake knows only TIMEMACHINE_SHARE")
        others = [sh for sh in self.s["smb_shares"] if sh["id"] != share.get("id")]
        if any(sh["name"].casefold() == share["name"].casefold() for sh in others):
            raise CallError(f"{where}.name: Share names are case-insensitive and must be unique", errname="EEXIST")
        path = share["path"]
        if not path.startswith("/mnt/") or path[5:] not in self.s["datasets"]:
            raise CallError(f"{where}.path: {path}: path does not exist.", errname="ENOENT")
        if share["enabled"] and not self.s["network"]["service_announcement"]["mdns"]:
            raise CallError(
                f"{where}.purpose: mDNS must be enabled in order to use an SMB share as a time machine target."
            )
        if not self.s["smb"]["aapl_extensions"]:
            raise CallError(
                f"{where}.purpose: Apple SMB2/3 protocol extension support is required by this parameter. This "
                "feature may be enabled in the general SMB server configuration."
            )

    def m_sharing_smb_query(self, filters=None, options=None):
        return [copy.deepcopy(sh) for sh in self.s.setdefault("smb_shares", []) if matches(sh, filters)]

    def m_sharing_smb_create(self, data):
        self.check_smb_share(data, "sharingsmb_create")
        share = {
            "id": self.new_id(),
            "purpose": "DEFAULT_SHARE",
            "enabled": True,
            "comment": "",
            "readonly": False,
            "browsable": True,
            "access_based_share_enumeration": False,
            "locked": False,
            "audit": {"enable": False, "watch_list": [], "ignore_list": []},
            **copy.deepcopy(data),
        }
        share["options"] = {**self.TIMEMACHINE_OPTIONS, **(data.get("options") or {})}
        share["options"]["vuid"] = share["options"]["vuid"] or str(uuid.uuid4())
        self.validate_smb_share(share, "sharingsmb_create")
        self.s.setdefault("smb_shares", []).append(share)
        self.mutated()
        return copy.deepcopy(share)

    def m_sharing_smb_update(self, share_id, data):
        share = next((sh for sh in self.s.setdefault("smb_shares", []) if sh["id"] == share_id), None)
        if share is None:
            raise CallError(f"SMB share {share_id} does not exist", errname="ENOENT")
        self.check_smb_share(data, "sharingsmb_update")
        new = {**copy.deepcopy(share), **copy.deepcopy(data)}
        if "options" in data:
            # The options are replaced whole, each one not given its default. Stricter than TrueNAS 25.10.7, which
            # keeps the old vuid: one not given is made anew, as the API's description says.
            new["options"] = {**self.TIMEMACHINE_OPTIONS, **(data["options"] or {})}
            new["options"]["vuid"] = new["options"]["vuid"] or str(uuid.uuid4())
        self.validate_smb_share(new, "sharingsmb_update")
        share.clear()
        share.update(new)
        self.mutated()
        return copy.deepcopy(share)

    # -------------------------------------------------------------- filesystem
    def directory(self, path, where):
        """A dataset's own directory, as the path names it: the fake has no other."""
        if not path.startswith("/mnt/") or path[5:] not in self.s["datasets"]:
            raise CallError(f"{where}: Path {path} not found", errname="ENOENT")
        return self.s.setdefault("paths", {}).get(path) or {"uid": 0, "gid": 0, "mode": 0o755, "acl": False}

    def m_filesystem_stat(self, path):
        entry = self.directory(path, "filesystem.stat")
        user = next((u for u in self.s["users"] if u["uid"] == entry["uid"]), None)
        group = self.group_of(entry["gid"])
        return {
            "realpath": path,
            "type": "DIRECTORY",
            "size": 3,
            "allocation_size": 512,
            "mode": stat.S_IFDIR | entry["mode"],
            "mount_id": 100,
            "uid": entry["uid"],
            "gid": entry["gid"],
            "atime": 0.0,
            "mtime": 0.0,
            "ctime": 0.0,
            "btime": 0.0,
            "dev": 1,
            "inode": 34,
            "nlink": 2,
            "acl": entry["acl"],
            "is_mountpoint": True,
            "is_ctldir": False,
            "attributes": [],
            "user": user["username"] if user else None,
            "group": group["group"] if group else None,
        }

    def m_filesystem_setperm(self, data):
        return self.start_job("filesystem.setperm", self._setperm, data)

    def _setperm(self, data):
        unknown = set(data) - {"path", "uid", "user", "gid", "group", "mode", "options"}
        if unknown:
            raise CallError(f"filesystem.setperm: Extra inputs are not permitted: {sorted(unknown)}")
        options = data.get("options") or {}
        if options.get("recursive") or options.get("traverse"):
            raise CallError("filesystem.setperm: the fake applies permissions to one directory, never recursively")
        if data.get("mode") is None and not options.get("stripacl"):
            raise CallError(
                "filesystem.setperm: Payload must either explicitly specify permissions or contain the stripacl option."
            )
        path = data["path"]
        entry = dict(self.directory(path, "filesystem.setperm.path"))
        if "/" not in path[5:]:
            raise CallError(f'filesystem.setperm.path: The specified path is a ZFS pool mountpoint "({path})"')
        if data.get("user"):
            user = next((u for u in self.s["users"] if u["username"] == data["user"]), None)
            if user is None:
                raise CallError(f"filesystem.setperm.user: {data['user']}: user does not exist")
            entry["uid"] = user["uid"]
        if data.get("group"):
            group = next((g for g in self.s["groups"] if g["group"] == data["group"]), None)
            if group is None:
                raise CallError(f"filesystem.setperm.group: {data['group']}: group does not exist")
            entry["gid"] = group["gid"]
        if entry["acl"] and not options.get("stripacl"):
            raise CallError(
                f'filesystem.setperm.mode: Non-trivial ACL present on [{path}]. Option "stripacl" required to change '
                "permission."
            )
        if data.get("mode") is not None:
            # UnixPerm (api/base/types/filesystem.py) takes 000 to 777, and setperm refuses 000.
            mode = int(data["mode"], 8)
            if mode & 0o777 != mode:
                raise CallError("filesystem.setperm.mode: Value error, Please supply a value between 000 and 777")
            if mode == 0:
                raise CallError("filesystem.setperm.mode: Empty permissions are not permitted.")
            entry["mode"] = mode
        entry["acl"] = False
        self.s["paths"][path] = entry
        self.mutated()

    # --------------------------------------------------------------- dispatch
    def handle(self, request, session):
        method = request.get("method", "")
        params = request.get("params", [])
        if method == "auth.login_with_api_key":
            session["authenticated"] = params == [API_KEY]
            self.log(method, ["<redacted>"], session["authenticated"])
            return session["authenticated"]
        if not session.get("authenticated"):
            raise CallError("Not authenticated", errname="EACCES")
        func = getattr(self, "m_" + method.replace(".", "_"), None)
        if func is None:
            raise CallError(f"Method {method!r} not found (or not implemented by the fake)", errname="ENOMETHOD")
        self.load()
        try:
            result = func(*params)
        finally:
            self.save()
        self.log(method, params, result)
        return result

    def log(self, method, params, result):
        if self.call_log:
            with self.call_log.open("a") as log:
                log.write(json.dumps({"method": method, "params": params, "result": result}) + "\n")


async def serve_forever(args):
    fake = FakeTrueNAS(args.state, args.call_log)
    if not Path(args.state).exists():
        Path(args.state).write_text(json.dumps(initial_state(args.address), indent=1, sort_keys=True))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)

    async def handler(ws):
        if ws.request.path != "/api/current":
            await ws.close(code=1008, reason="wrong path")
            return
        session = {}
        async for raw in ws:
            request = json.loads(raw)
            reply = {"jsonrpc": "2.0", "id": request.get("id")}
            try:
                reply["result"] = fake.handle(request, session)
            except CallError as exc:
                reply["error"] = {
                    "code": -32001,
                    "message": "Method call error",
                    "data": {"errname": exc.errname, "reason": f"[{exc.errname}] {exc.reason}", "extra": exc.extra},
                }
            except Exception as exc:  # noqa: BLE001 - a fake bug must surface as a call error, not a hang
                reply["error"] = {"code": -32603, "message": "Internal error", "data": {"reason": repr(exc)}}
            await ws.send(json.dumps(reply))

    async with serve(handler, args.bind, args.port, ssl=context):
        await asyncio.Future()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--address", default="127.0.0.1", help="the address the fake NAS has before any change")
    parser.add_argument("--call-log", help="append every call and its answer to this file (JSON lines)")
    asyncio.run(serve_forever(parser.parse_args()))


if __name__ == "__main__":
    main()
