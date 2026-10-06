#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
---
module: truenas_pool
short_description: Import or create TrueNAS pools, then set auto-trim and their scrub tasks
description:
  - An imported pool is left as it is, apart from its auto-trim setting and scrub task.
  - A pool that C(pool.import_find) offers is imported.
  - Otherwise the pool is created, but only when O(allow_create) is true and every disk named in
    its C(disk_serials) is blank - unused, in no pool, imported or exported, and without partitions.
    Anything else fails with an explanation.
  - The module never destroys, exports, wipes, expands or upgrades a pool.
  - Takes a list so a play logs in once for all of them; TrueNAS rate-limits logins.
  - Importing a pool can move the system dataset onto it. Pin the system dataset (with
    truenas_config, namespace C(systemdataset)) before calling this.
options:
  pools:
    description:
      - The pools. Each has C(name) and optionally C(guid) (the pool found or imported must have it),
        C(layout) (data vdev type for creation, C(STRIPE) for one disk), C(disk_serials) (resolved
        to device names at run time), C(autotrim) (C(ON) or C(OFF)) and C(scrub).
      - >-
        C(scrub) is the pool's scrub task as C(pool.scrub.create) takes it minus C(pool), for example
        C({threshold: 35, schedule: {minute: "00", hour: "00", dom: "*", month: "*", dow: "7"}}).
        TrueNAS creates a default task when it imports or creates a pool; this corrects it.
    type: list
    elements: dict
    required: true
  allow_create:
    description: Allow creating a pool that can be neither found nor imported.
    type: bool
    default: false
  disk_serials:
    description: Pool name to disk serials, for pools that do not list their own.
    type: dict
    default: {}
  guids:
    description: Pool name to GUID, for pools that do not give their own.
    type: dict
    default: {}
  autotrim:
    description: Auto-trim for pools that do not set their own.
    type: str
    choices: ["ON", "OFF"]
  scrub:
    description: Scrub task for pools that do not set their own.
    type: dict
notes:
  - Connection and timing options (api_host, api_port, api_key, validate_certs, api_cert_sha256,
    api_timeout, api_connect_wait, api_job_timeout, api_login_wait) are shared by every module in this role; see
    module_utils/truenas_api.py.
"""

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.truenas_api import (
    TrueNASError,
    api_argument_spec,
    connect,
    differences,
    pick,
)


LAYOUTS = ["STRIPE", "MIRROR", "RAIDZ1", "RAIDZ2", "RAIDZ3"]


def find_blank_disks(api, serials):
    """Map serials to device names, failing unless every disk is entirely unused."""
    details = api.call("disk.details", {"join_partitions": True, "type": "BOTH"})
    unused = {d["serial"]: d for d in details.get("unused", [])}
    used = {d["serial"]: d for d in details.get("used", [])}
    names, problems = [], []
    for serial in serials:
        disk = unused.get(serial)
        if disk is None:
            other = used.get(serial)
            if other is None:
                problems.append(f"{serial}: no such disk")
            else:
                pool = other.get("imported_zpool") or other.get("exported_zpool") or "something"
                problems.append(f"{serial} ({other['name']}): in use by {pool}")
            continue
        if disk.get("exported_zpool") or disk.get("imported_zpool"):
            problems.append(f"{serial} ({disk['name']}): carries pool {disk.get('exported_zpool') or disk.get('imported_zpool')}")
        elif disk.get("partitions"):
            problems.append(f"{serial} ({disk['name']}): has {len(disk['partitions'])} partition(s)")
        elif disk.get("duplicate_serial"):
            problems.append(f"{serial} ({disk['name']}): serial is not unique")
        else:
            names.append(disk["name"])
    return names, problems


def ensure(api, module, spec, allow_create):
    """Import or create one pool, then set auto-trim and its scrub task.

    Returns (actions, state before, summary after)."""
    name, guid = spec["name"], spec.get("guid")
    actions = []
    found = api.call("pool.query", [["name", "=", name]])
    before = {"state": "imported" if found else "not imported"}
    if found:
        pool = found[0]
        if guid and str(pool["guid"]) != str(guid):
            module.fail_json(msg=f"pool {name} is imported but has GUID {pool['guid']}, not {guid}")
    else:
        offered = api.job("pool.import_find")
        candidates = [c for c in offered if c["name"] == name and (not guid or str(c["guid"]) == str(guid))]
        if len(candidates) > 1:
            module.fail_json(msg=f"{len(candidates)} importable pools are named {name}; set its GUID to choose one")
        if candidates:
            candidate = candidates[0]
            if candidate["status"] != "ONLINE":
                module.fail_json(msg=f"pool {name} can be imported but is {candidate['status']}; not importing it")
            actions.append("import")
            if not module.check_mode:
                api.job("pool.import_pool", {"guid": str(candidate["guid"])})
        elif not allow_create:
            module.fail_json(
                msg=(
                    f"pool {name} is neither imported nor importable. Creating it needs "
                    "allow_create (truenas_pool_allow_create) and blank disks."
                ),
                importable=[pick(c, ["name", "guid", "status"]) for c in offered],
            )
        else:
            if not spec.get("layout") or not spec.get("disk_serials"):
                module.fail_json(msg=f"creating pool {name} needs a layout and disk serials")
            if spec["layout"] not in LAYOUTS:
                module.fail_json(msg=f"pool {name}: layout must be one of {LAYOUTS}")
            disks, problems = find_blank_disks(api, spec["disk_serials"])
            if problems:
                module.fail_json(msg=f"not creating pool {name}; these disks are not blank: " + "; ".join(problems))
            actions.append("create")
            if not module.check_mode:
                api.job(
                    "pool.create",
                    {
                        "name": name,
                        "encryption": False,
                        "topology": {"data": [{"type": spec["layout"], "disks": disks}]},
                    },
                )
        if module.check_mode:
            # Nothing else can be compared against a pool that does not exist yet.
            actions += [a for a in ("autotrim", "scrub") if spec.get(a)]
            return actions, before, {"name": name}
        pool = api.call("pool.query", [["name", "=", name]])[0]

    if spec.get("autotrim"):
        current = str((pool.get("autotrim") or {}).get("rawvalue", "")).upper()
        before["autotrim"] = current
        if current != spec["autotrim"].upper():
            actions.append("autotrim")
            if not module.check_mode:
                api.job("pool.update", pool["id"], {"autotrim": spec["autotrim"].upper()})

    scrub = None
    if spec.get("scrub") is not None:
        tasks = [t for t in api.call("pool.scrub.query") if t.get("pool") == pool["id"] or t.get("pool_name") == name]
        if len(tasks) > 1:
            module.fail_json(msg=f"pool {name} has {len(tasks)} scrub tasks; expected one")
        if not tasks:
            actions.append("scrub")
            if not module.check_mode:
                scrub = api.call("pool.scrub.create", {"pool": pool["id"], **spec["scrub"]})
        else:
            scrub = tasks[0]
            before["scrub"] = pick(scrub, list(spec["scrub"]))
            changes = differences(scrub, spec["scrub"])
            if changes:
                actions.append("scrub")
                if not module.check_mode:
                    scrub = api.call("pool.scrub.update", scrub["id"], changes)

    if actions and not module.check_mode:
        pool = api.call("pool.query", [["name", "=", name]])[0]
    if not pool.get("healthy", True):
        module.warn(f"pool {name} reports status {pool.get('status')}")
    return actions, before, {
        **pick(pool, ["id", "name", "guid", "status", "healthy", "path"]),
        "autotrim": (pool.get("autotrim") or {}).get("rawvalue"),
        "scrub": pick(scrub or {}, ["id", "threshold", "schedule", "enabled"]),
    }


def main():
    spec = api_argument_spec()
    spec.update(
        pools=dict(type="list", elements="dict", required=True),
        allow_create=dict(type="bool", default=False),
        disk_serials=dict(type="dict", default={}, no_log=False),
        guids=dict(type="dict", default={}),
        autotrim=dict(type="str", choices=["ON", "OFF"]),
        scrub=dict(type="dict"),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    p = module.params
    pools = []
    for entry in p["pools"]:
        if not entry.get("name"):
            module.fail_json(msg=f"a pool has no name: {entry}")
        name = entry["name"]
        shared = {
            "disk_serials": p["disk_serials"].get(name) or [],
            "guid": p["guids"].get(name),
            "autotrim": p["autotrim"],
            "scrub": p["scrub"],
        }
        pools.append({**shared, **{k: v for k, v in entry.items() if v is not None}})

    actions, before, after, summary = {}, {}, {}, {}
    api = connect(module)
    with api:
        try:
            for entry in pools:
                name = entry["name"]
                done, before[name], summary[name] = ensure(api, module, entry, p["allow_create"])
                after[name] = {"state": "imported"}
                after[name].update({k: entry[k] for k in ("autotrim", "scrub") if k in before[name] and entry.get(k)})
                if done:
                    actions[name] = done
                    if "autotrim" not in before[name] and entry.get("autotrim"):
                        after[name]["autotrim"] = entry["autotrim"]
                    if "scrub" not in before[name] and entry.get("scrub"):
                        after[name]["scrub"] = entry["scrub"]
        except TrueNASError as exc:
            module.fail_json(msg=str(exc), actions=actions)
    module.exit_json(
        changed=bool(actions), actions=actions, pools=summary, diff={"before": before, "after": after}
    )


if __name__ == "__main__":
    main()
