#!/usr/bin/python

DOCUMENTATION = r"""
---
module: truenas_info
short_description: Connect to TrueNAS and report its state, read-only
description:
  - Tries each address in O(api_hosts) in order and reports the first one that accepts the API key,
    so a play can reach a NAS that is either on its configured static address or still on the
    address it was installed with.
  - Makes no changes. C(pool.import_find) is a job, but it only scans disks.
  - With O(status), also reports what just status shows, in the same login - each pool's health, capacity, layout
    and last scrub, TrueNAS's alerts that are not dismissed, each periodic snapshot task's last run, the uptime, and
    each Time Machine share's backup - its dataset's use against its quota, what its snapshots hold, and, for an
    enabled share, when the newest backup completed, the time in the newest C(aapltm-<epoch>) snapshot Samba takes
    after each one.
  - The backups' times come from one C(pool.snapshot.query) for every enabled Time Machine share whose dataset
    exists, and none without one - filtered on C(dataset) with C(in) and selecting C(name) alone, which TrueNAS
    answers by listing those datasets' snapshots by name, reading no property; any other filter or select reads every
    snapshot on the NAS with every property first.
  - With O(status), also reports each pool's disks by device name, with their state and ZFS's error counts, every
    disk's temperature and critical temperature (C(disk.temperatures), which TrueNAS reads from the kernel's sensors
    and keeps for five minutes), the addresses that have the NFS shares mounted (C(nfs.get_nfs3_clients) and
    C(nfs.get_nfs4_clients)), and the NAS's clock beside this machine's. These reads come last, and one that fails
    leaves its part null, with why in C(unread), never the whole read failed. No disk's serial or model is reported.
  - Without O(status), also reports which local users are SMB logins and which user the API key logs in as, by name
    alone, for the role's check that no SMB login it does not list exists when it starts SMB.
  - Runs on the controller and needs the websocket-client Python package there.
options:
  api_hosts:
    description: Addresses to try, in order.
    type: list
    elements: str
    required: true
  api_connect_wait:
    description: Seconds to keep retrying the list of addresses before giving up.
    type: int
    default: 60
  api_job_timeout:
    description: Seconds to wait for the pool scan job.
    type: int
    default: 1800
  importable:
    description: Also scan for pools that can be imported. Takes a few seconds.
    type: bool
    default: true
  status:
    description: Also report the NAS's health, as RV(status) describes.
    type: bool
    default: false
attributes:
  check_mode:
    support: full
  diff_mode:
    support: none
  platform:
    platforms: posix
extends_documentation_fragment:
  - ansible.builtin.action_common_attributes
  - truenas_api
"""

RETURN = r"""
api_host:
  description: The address that answered. Later tasks should use it.
  type: str
  returned: success
version:
  description: TrueNAS version without the product prefix, for example C(25.10.7).
  type: str
  returned: success
smb_shares:
  description: The SMB shares, by name, with their path, purpose and whether they are enabled.
  type: list
  elements: dict
  returned: success
  sample: [{name: tm-laptop, path: /mnt/persistent/timemachine/laptop, purpose: TIMEMACHINE_SHARE, enabled: true}]
smb_users:
  description: The names of the local users that are SMB logins, TrueNAS's own built-in users left out.
  type: list
  elements: str
  returned: success and not O(status)
api_user:
  description: The name of the user the API key logs in as.
  type: str
  returned: success and not O(status)
status:
  description:
    - The NAS's health. Times are RFC 3339 in UTC, sizes bytes. A pool's C(used) and C(available) are its root
      dataset's, what its datasets can still take, parity left out; C(scan) is its last scrub or resilver, and
      C(scrub) the task that scrubs it, if any. Only alerts that are not dismissed are listed.
    - A Time Machine share's C(dataset) is the one its path names, and C(dataset_found) false, its sizes null, when
      the NAS has none there. Its C(last_backup) is null for a disabled share, which is not read, and when no
      C(aapltm-) snapshot holds a time; a time more than an hour ahead of the controller's clock is ignored.
    - A pool's C(disks) are its topology's, each named as C(disk.temperatures) names it (a disk that is gone by the
      one TrueNAS last knew there, else by its ZFS guid), never by the vdev's own name, which can carry a serial.
      C(temperatures) is each disk's reading and critical temperature in degrees Celsius, either null when TrueNAS
      has none, and C(nfs_clients) the addresses that have a share mounted, without their ports; each is null when
      its read failed, and C(unread) then says why. C(datetime) is the NAS's clock, and C(read_at) this machine's,
      halfway through the call that read it.
  type: dict
  returned: success and O(status)
  sample:
    uptime_seconds: 2937600.5
    pools:
      - {name: persistent, status: ONLINE, healthy: true, detail: "", layout: raidz1, used: 2418925581107,
         available: 5387625168896, scan: {function: SCRUB, state: FINISHED, start_time: "2026-10-12T00:00:02+00:00",
         end_time: "2026-10-12T03:14:51+00:00", percentage: 100.0, errors: 0}, scrub: {threshold: 35, enabled: true},
         disks: [{name: nvme1n1, status: ONLINE, read_errors: 0, write_errors: 0, checksum_errors: 0}]}
    temperatures: {nvme0n1: {temp: 45.0, critical: 80.85}, sda: {temp: 30.0, critical: 70.85}}
    nfs_clients: [192.0.2.20]
    datetime: "2026-11-18T10:00:00.250000+00:00"
    read_at: "2026-11-18T10:00:00.210000+00:00"
    unread: {}
    alerts:
      - {klass: SMARTTestFailed, level: CRITICAL, text: "...", datetime: "2026-10-20T08:00:00+00:00"}
    snapshot_tasks:
      - {id: 1, dataset: persistent/proxmox/vm, enabled: true, schedule: {minute: "0", hour: "*", dom: "*",
         month: "*", dow: "*"}, lifetime_value: 1, lifetime_unit: DAY, state: FINISHED,
         last_run: "2026-10-21T21:00:01+00:00", error: ""}
    time_machine:
      - {name: tm-laptop, dataset: persistent/timemachine/laptop, dataset_found: true, enabled: true,
         used: 412316860416, quota: 2199023255552, snapshots_used: 21474836480,
         last_backup: "2026-11-17T20:14:05+00:00"}
"""

import re
import time
from datetime import UTC, datetime

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.truenas_api import (
    TrueNASError,
    api_argument_spec,
    open_client,
    pick,
)

# Samba's snapshot after each completed Time Machine backup, named after the time it completed (vfs_tmprotect).
BACKUP_SNAPSHOT = re.compile(r"aapltm-(\d+)")
# Seconds a time in one's name may be ahead of this machine's clock: the NAS's clock and this one's never agree exactly.
FUTURE_SLACK = 3600
# A pool's kinds of vdev, as pool.query's topology names them, and the error counts ZFS keeps for each vdev (libzfs's
# vdev stats), which `zpool clear` and a pool's import reset.
VDEV_KINDS = ("data", "special", "dedup", "log", "cache", "spare")
ERROR_COUNTS = ("read_errors", "write_errors", "checksum_errors")


def _address(alias):
    return f"{alias['address']}/{alias['netmask']}"


def _date(value):
    """An API time as RFC 3339 in UTC: TrueNAS sends {"$date": milliseconds}."""
    if isinstance(value, dict) and "$date" in value:
        value = value["$date"] / 1000
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC).isoformat()
    return value or None


def _bytes(prop):
    """A ZFS size property's bytes, None when it has none."""
    prop = prop or {}
    for value in (prop.get("parsed"), prop.get("rawvalue")):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _layout(topology):
    """The kinds of a pool's data vdevs, such as raidz1; a disk of its own is a stripe."""
    kinds = sorted({str(v.get("type") or "").lower() for v in (topology or {}).get("data") or []} - {""})
    return ", ".join("stripe" if kind == "disk" else kind for kind in kinds)


def _share_dataset(share):
    return str(share.get("path") or "").removeprefix("/mnt/")


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _disks(topology):
    """A pool's disks, in its vdevs' order, each with its state and ZFS's error counts. A disk is named as
    disk.temperatures names it (pool.query's `disk`, which TrueNAS works out from the vdev's path); one that is gone
    by the disk TrueNAS last knew at its place (`unavail_disk`, a disk.query entry, of which its name alone is kept),
    else by its ZFS guid. Never by the vdev's own name, which is its path's: a pool made by hand on
    /dev/disk/by-id/... would give the disk's model and serial."""
    out = []

    def walk(vdev):
        if not isinstance(vdev, dict):
            return
        if str(vdev.get("type") or "").upper() == "DISK":
            stats = vdev.get("stats") or {}
            gone = vdev.get("unavail_disk") if isinstance(vdev.get("unavail_disk"), dict) else {}
            out.append(
                {
                    "name": str(vdev.get("disk") or gone.get("name") or vdev.get("guid") or ""),
                    "status": str(vdev.get("status") or ""),
                    **{key: _count(stats.get(key)) for key in ERROR_COUNTS},
                }
            )
        for child in vdev.get("children") or []:
            walk(child)

    for kind in VDEV_KINDS:
        for vdev in (topology or {}).get(kind) or []:
            walk(vdev)
    return out


def _celsius(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _temperatures(answer):
    """disk.temperatures' answer with thresholds, {disk: [celsius, critical celsius]}, as {disk: {temp, critical}}.
    Either is null for a disk whose sensor TrueNAS cannot read (a virtual disk has none)."""
    out = {}
    for name, value in answer.items() if isinstance(answer, dict) else ():
        reading, critical = [*value, None, None][:2] if isinstance(value, (list, tuple)) else (value, None)
        out[str(name)] = {"temp": _celsius(reading), "critical": _celsius(critical)}
    return out


def _client_address(text):
    """An NFSv4 client's address without its port: 192.0.2.20 from 192.0.2.20:739, and 2001:db8::5 from
    [2001:db8::5]:739."""
    text = str(text or "").strip()
    if text.startswith("["):
        return text[1:].partition("]")[0]
    host, colon, port = text.rpartition(":")
    return host if colon and port.isdigit() and ":" not in host else text


def nfs_clients(v3, v4):
    """The addresses that have an NFS share mounted, each once, in the order read: NFSv3's from rmtab (`ip`), which
    may keep a client that has gone, and NFSv4's from the kernel's list of clients (`info.address`). Nothing else of
    either is kept: an NFSv4 client's entry also names the client's host and the files it holds open."""
    addresses = [str(c.get("ip") or "").strip() for c in v3 or [] if isinstance(c, dict)]
    addresses += [_client_address((c.get("info") or {}).get("address")) for c in v4 or [] if isinstance(c, dict)]
    return list(dict.fromkeys(a for a in addresses if a))


def _optional(api, unread, part, method, *params):
    """A read that may fail on its own: its answer, or None with why in unread[part]. The NAS is still read."""
    try:
        return api.call(method, *params)
    except TrueNASError as exc:
        unread[part] = str(exc)
        return None


def time_machine(api, shares, datasets):
    """Each Time Machine share's backup: its dataset's use, quota and snapshots' share of it, and, for an enabled share,
    when the newest backup completed, which the newest of the aapltm-<epoch> snapshots Samba takes after each one says.

    One pool.snapshot.query for every enabled share whose dataset exists, and none without one. A select of name alone,
    filtered on dataset alone, is TrueNAS's fast path (zfs.snapshot.query, TS-25.10.7): it lists the snapshots of those
    datasets alone and reads no property, where any other select or filter reads every snapshot on the NAS with every
    property, then filters. Each name, <dataset>@<snapshot>, is split here, so a snapshot counts for its own dataset
    alone, and for none it was not asked about. A time more than FUTURE_SLACK ahead of this machine's clock is no
    backup's: the NAS's clock was wrong, or the name was made by hand. Taken as the newest it would keep the Mac from
    ever reading as late, and one past the year 9999 is a time datetime cannot hold."""
    shares = [s for s in shares if s.get("purpose") == "TIMEMACHINE_SHARE"]
    enabled = [_share_dataset(s) for s in shares if s.get("enabled", True)]
    read = list(dict.fromkeys(dataset for dataset in enabled if dataset in datasets))
    newest = {}
    if read:
        latest = time.time() + FUTURE_SLACK
        for snapshot in api.call("pool.snapshot.query", [["dataset", "in", read]], {"select": ["name"]}):
            dataset, _, name = str(snapshot.get("name") or "").partition("@")
            backup = BACKUP_SNAPSHOT.fullmatch(name)
            if dataset in read and backup and int(backup.group(1)) <= latest:
                newest[dataset] = max(newest.get(dataset, 0), int(backup.group(1)))
    out = []
    for share in shares:
        dataset = _share_dataset(share)
        found = datasets.get(dataset)
        out.append(
            {
                **pick(share, ["name", "enabled"]),
                "dataset": dataset,
                "dataset_found": found is not None,
                "used": _bytes((found or {}).get("used")),
                "quota": _bytes((found or {}).get("quota")),
                "snapshots_used": _bytes((found or {}).get("usedbysnapshots")),
                "last_backup": _date(newest[dataset]) if dataset in newest else None,
            }
        )
    return out


def health(api, info, pools, datasets, smb_shares=(), info_at=None):
    """What just status shows of the NAS, from what gather read and three more calls, one for the Time Machine shares'
    snapshots, and last the three reads that may fail on their own: the disks' temperatures and the NFS clients, whose
    failure leaves that part null and says why in `unread`. `info_at` is this machine's clock when system.info
    answered."""
    roots = {d.get("id"): d for d in datasets if "/" not in str(d.get("id"))}
    by_id = {d.get("id"): d for d in datasets}
    scrubs = {s.get("pool_name"): pick(s, ["threshold", "enabled"]) for s in api.call("pool.scrub.query")}
    scan_fields = ["function", "state", "percentage", "errors"]
    out = {
        "uptime_seconds": info.get("uptime_seconds"),
        "pools": [
            {
                **pick(p, ["name", "status", "healthy"]),
                "detail": p.get("status_detail") or "",
                "layout": _layout(p.get("topology")),
                "used": _bytes(roots.get(p.get("name"), {}).get("used")),
                "available": _bytes(roots.get(p.get("name"), {}).get("available")),
                "scan": {
                    **pick(p["scan"], scan_fields),
                    "start_time": _date(p["scan"].get("start_time")),
                    "end_time": _date(p["scan"].get("end_time")),
                }
                if p.get("scan")
                else None,
                "scrub": scrubs.get(p.get("name")),
                "disks": _disks(p.get("topology")),
            }
            for p in pools
        ],
        "alerts": [
            {
                **pick(a, ["klass", "level"]),
                "text": a.get("formatted") or a.get("text") or "",
                "datetime": _date(a.get("datetime")),
            }
            for a in api.call("alert.list")
            if not a.get("dismissed")
        ],
        "snapshot_tasks": [
            {
                **pick(t, ["id", "dataset", "enabled", "lifetime_value", "lifetime_unit"]),
                "schedule": pick(t.get("schedule") or {}, ["minute", "hour", "dom", "month", "dow"]),
                "state": (t.get("state") or {}).get("state") or "",
                "last_run": _date((t.get("state") or {}).get("datetime")),
                "error": (t.get("state") or {}).get("error") or "",
            }
            for t in api.call("pool.snapshottask.query")
        ],
        "time_machine": time_machine(api, smb_shares, by_id),
        "datetime": _date(info.get("datetime")),
        "read_at": _date(info_at),
    }
    unread = {}
    # Every disk, with its critical temperature: TrueNAS reads the kernel's sensors (hwmon) and keeps each reading for
    # five minutes, so this asks nothing of the disks themselves.
    temperatures = _optional(api, unread, "temperatures", "disk.temperatures", [], True)
    v3 = _optional(api, unread, "nfs_clients", "nfs.get_nfs3_clients")
    v4 = _optional(api, unread, "nfs_clients", "nfs.get_nfs4_clients") if v3 is not None else None
    return out | {
        "temperatures": None if temperatures is None else _temperatures(temperatures),
        "nfs_clients": None if v3 is None or v4 is None else nfs_clients(v3, v4),
        "unread": unread,
    }


def gather(api, importable, status=False):
    network = api.call("network.configuration.config")
    disks = api.call("disk.details", {"type": "BOTH"})
    before = time.time()
    info = api.call("system.info")
    # This machine's clock when the NAS read its own: halfway through the call, within half its round trip.
    info_at = (before + time.time()) / 2
    pools = api.call("pool.query")
    datasets = api.call("pool.dataset.query", [], {"extra": {"flat": True}})
    smb_shares = api.call("sharing.smb.query")
    result = {
        "version": api.call("system.version").removeprefix("TrueNAS-"),
        "ready": api.call("system.ready"),
        "hostname": info.get("hostname"),
        "websocket_interface": (api.call("interface.websocket_interface") or {}).get("name"),
        "websocket_local_ip": api.call("interface.websocket_local_ip"),
        "interfaces": [
            {
                **pick(iface, ["name", "ipv4_dhcp", "ipv6_auto"]),
                "aliases": [_address(a) for a in iface["aliases"]],
                "addresses": [_address(a) for a in iface["state"]["aliases"]],
                "link_state": iface["state"].get("link_state"),
                # PHYSICAL for a port; a bridge, a VLAN or a link aggregation is another. Its MTU, and the speed
                # its link runs at, as TrueNAS words it (10000Mb/s Twisted Pair): never its MAC.
                "type": iface.get("type"),
                "mtu": iface["state"].get("mtu"),
                "media": iface["state"].get("active_media_subtype") or "",
            }
            for iface in api.call("interface.query")
        ],
        "network": {
            **pick(network, ["hostname", "domain", "ipv4gateway", "nameserver1", "nameserver2", "nameserver3"]),
            "effective": network.get("state", {}),
        },
        "network_pending_changes": api.call("interface.has_pending_changes"),
        "pools": [
            {
                **pick(p, ["id", "name", "guid", "status", "healthy", "path"]),
                "autotrim": (p.get("autotrim") or {}).get("rawvalue"),
            }
            for p in pools
        ],
        "disks": [
            pick(d, ["name", "serial", "model", "size", "imported_zpool", "exported_zpool"])
            for kind in ("used", "unused")
            for d in disks.get(kind, [])
        ],
        "services": {s["service"]: pick(s, ["enable", "state"]) for s in api.call("service.query")},
        "system_dataset": pick(api.call("systemdataset.config"), ["pool", "pool_set"]),
        "datasets": [d["id"] for d in datasets],
        "nfs_shares": [
            pick(s, ["id", "path", "hosts", "networks", "enabled", "ro", "maproot_user", "maproot_group"])
            for s in api.call("sharing.nfs.query")
        ],
        "smb_shares": [pick(s, ["name", "path", "purpose", "enabled"]) for s in smb_shares],
    }
    if not status:
        # Names alone: user.query also holds each user's password hashes, which select leaves out.
        smb = [["local", "=", True], ["builtin", "=", False], ["smb", "=", True]]
        result["smb_users"] = [u["username"] for u in api.call("user.query", smb, {"select": ["username"]})]
        result["api_user"] = (api.call("auth.me") or {}).get("pw_name")
    if importable:
        result["importable_pools"] = api.job("pool.import_find")
    if status:
        result["status"] = health(api, info, pools, datasets, smb_shares, info_at)
    return result


def main():
    spec = api_argument_spec()
    spec.pop("api_host")
    spec.update(
        api_hosts=dict(type="list", elements="str", required=True),
        importable=dict(type="bool", default=True),
        status=dict(type="bool", default=False),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)

    hosts = [h for h in dict.fromkeys(module.params["api_hosts"]) if h]
    if not hosts:
        module.fail_json(msg="api_hosts is empty: there is no address to reach the NAS on")

    # Each pass tries every address once with a short timeout, so a static address that is not
    # live yet costs a few seconds rather than the whole connect window.
    deadline = time.monotonic() + module.params["api_connect_wait"]
    errors = {}
    while True:
        for host in hosts:
            try:
                api = open_client(module, host=host, connect_wait=0, connect_timeout=5)
            except TrueNASError as exc:
                if "rejected the API key" in str(exc) or "refused the login" in str(exc):
                    module.fail_json(msg=str(exc))
                errors[host] = str(exc)
                continue
            with api:
                try:
                    result = gather(api, module.params["importable"], module.params["status"])
                except TrueNASError as exc:
                    module.fail_json(msg=str(exc))
            module.exit_json(changed=False, api_host=host, **result)
        if time.monotonic() >= deadline:
            module.fail_json(msg="TrueNAS did not answer on any of the given addresses", errors=errors)
        time.sleep(2)


if __name__ == "__main__":
    main()
