"""A stateful stand-in for the TrueNAS 25.10 JSON-RPC WebSocket API, for the idempotence tests.

It implements only the methods roles/truenas calls, returns the shapes the real API returns
(checked against TrueNAS 25.10.7 and its middleware source), and refuses what the real API refuses
where a non-idempotent role would trip over it: creating a dataset, share, NTP server or scrub
task that exists, importing a pool that is imported, POSIX ACLs without aclmode DISCARD,
non-consecutive name servers. Any other method is an error, so an unexpected call fails the test.

State lives in a JSON file that is re-read before and written after every call, so a test can
inspect it, or change it to simulate drift, between runs.

    python fake_truenas.py --port 8443 --cert cert.pem --key key.pem --state state.json --address 127.0.0.1
"""

import argparse
import asyncio
import copy
import json
import ssl
from pathlib import Path

from websockets.asyncio.server import serve

API_KEY = "1-fake-api-key-for-tests"
# Stands in for the UI certificate's private key; the tests check it never reaches Ansible output.
PRIVATE_KEY = "FAKE-PRIVATE-KEY-MUST-NOT-LEAK"
BOOT_POOL = "boot-pool"


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
            "enp7s0": {"ipv4_dhcp": True, "ipv6_auto": True, "aliases": [], "live": [address]},
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
            {"name": "nvme_gen3", "guid": "1001", "status": "ONLINE", "hostname": "", "children": []},
            {
                "name": "ephemeral",
                "guid": "1002",
                "status": "ONLINE",
                "hostname": "",
                "children": ["ephemeral/jellyfin"],
            },
        ],
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
        "mutations": 0,
    }


def matches(item, filters):
    for field, op, value in filters or []:
        if op != "=":
            raise CallError(f"filter operator {op!r} not supported by the fake")
        if item.get(field) != value:
            return False
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
        return "TrueNAS-25.10.7"

    def m_system_ready(self):
        return True

    def m_system_info(self):
        return {"version": "25.10.7", "hostname": self.s["network"]["hostname"]}

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
        return self.iface("enp7s0")

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
        self.s["pools"].append(
            {
                "id": pool_id,
                "name": name,
                "guid": guid,
                "status": "ONLINE",
                "healthy": True,
                "path": f"/mnt/{name}",
                "autotrim": prop("off"),
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

    SHARE_FIELDS = {
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
    DEFAULTS = {
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

    PROPERTY_FIELDS = set(DEFAULTS) | {"comments"}

    def m_pool_dataset_query(self, filters=None, options=None):
        return [copy.deepcopy(d) for d in self.s["datasets"].values() if matches(d, filters)]

    def m_pool_dataset_create(self, data):
        data = dict(data)
        name = data.pop("name")
        if data.pop("type", "FILESYSTEM") != "FILESYSTEM":
            raise CallError("the fake only creates filesystems")
        data.pop("share_type", None)
        ancestors = data.pop("create_ancestors", False)
        unknown = set(data) - self.PROPERTY_FIELDS
        if unknown:
            raise CallError(f"pool_dataset_create: unexpected fields {sorted(unknown)}")
        if name in self.s["datasets"]:
            raise CallError(f"pool_dataset_create.name: Path /mnt/{name} already exists", errname="EEXIST")
        parent = name.rsplit("/", 1)[0]
        if "/" not in name or (parent not in self.s["datasets"] and not ancestors):
            raise CallError(f"pool_dataset_create.name: parent {parent} does not exist", errname="ENOENT")
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
