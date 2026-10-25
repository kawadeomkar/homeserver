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
    and last scrub, TrueNAS's alerts that are not dismissed, each periodic snapshot task's last run, and the uptime.
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
status:
  description:
    - The NAS's health. Times are RFC 3339 in UTC, sizes bytes. A pool's C(used) and C(available) are its root
      dataset's, what its datasets can still take, parity left out; C(scan) is its last scrub or resilver, and
      C(scrub) the task that scrubs it, if any. Only alerts that are not dismissed are listed.
  type: dict
  returned: success and O(status)
  sample:
    uptime_seconds: 2937600.5
    pools:
      - {name: persistent, status: ONLINE, healthy: true, detail: "", layout: raidz1, used: 2418925581107,
         available: 5387625168896, scan: {function: SCRUB, state: FINISHED, start_time: "2026-10-12T00:00:02+00:00",
         end_time: "2026-10-12T03:14:51+00:00", percentage: 100.0, errors: 0}, scrub: {threshold: 35, enabled: true}}
    alerts:
      - {klass: SMARTTestFailed, level: CRITICAL, text: "...", datetime: "2026-10-20T08:00:00+00:00"}
    snapshot_tasks:
      - {id: 1, dataset: persistent/proxmox/vm, enabled: true, schedule: {minute: "0", hour: "*", dom: "*",
         month: "*", dow: "*"}, lifetime_value: 1, lifetime_unit: DAY, state: FINISHED,
         last_run: "2026-10-21T21:00:01+00:00", error: ""}
"""

import time
from datetime import UTC, datetime

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.truenas_api import (
    TrueNASError,
    api_argument_spec,
    open_client,
    pick,
)


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


def health(api, info, pools, datasets):
    """What just status shows of the NAS, from what gather read and three more calls."""
    roots = {d.get("id"): d for d in datasets if "/" not in str(d.get("id"))}
    scrubs = {s.get("pool_name"): pick(s, ["threshold", "enabled"]) for s in api.call("pool.scrub.query")}
    scan_fields = ["function", "state", "percentage", "errors"]
    return {
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
    }


def gather(api, importable, status=False):
    network = api.call("network.configuration.config")
    disks = api.call("disk.details", {"type": "BOTH"})
    info = api.call("system.info")
    pools = api.call("pool.query")
    datasets = api.call("pool.dataset.query", [], {"extra": {"flat": True}})
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
    }
    if importable:
        result["importable_pools"] = api.job("pool.import_find")
    if status:
        result["status"] = health(api, info, pools, datasets)
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
