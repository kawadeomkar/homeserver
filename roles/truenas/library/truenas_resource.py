#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
---
module: truenas_resource
short_description: Create, update or delete items of a TrueNAS collection
description:
  - For each entry of O(entries), finds the one item whose O(key) fields are equal with
    C(<namespace>.query), then creates it, updates the other fields that differ, or deletes it.
    Fails if more than one item matches.
  - Used for C(sharing.nfs) (key C(path)) and C(system.ntpserver) (key C(address)).
  - Lists of plain values (an NFS share's C(hosts), say) are compared ignoring order.
  - Takes a list so a play logs in once for all of them; TrueNAS rate-limits logins.
options:
  namespace:
    description: API namespace, for example C(sharing.nfs).
    type: str
    required: true
  key:
    description: Fields that identify an item.
    type: list
    elements: str
    required: true
  entries:
    description: The items, each with the O(key) fields and the other fields to manage.
    type: list
    elements: dict
    required: true
  defaults:
    description: Fields laid under every entry; an entry's own value wins.
    type: dict
    default: {}
  state:
    type: str
    choices: [present, absent]
    default: present
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


def ensure(api, module, namespace, match, data, state):
    """Bring one item to the wanted state. Returns (action or None, before, after)."""
    found = api.call(f"{namespace}.query", [[k, "=", v] for k, v in match.items()])
    if len(found) > 1:
        module.fail_json(msg=f"{len(found)} items in {namespace} match {match}; expected at most one")
    item = found[0] if found else None
    fields = [*match, *data]

    if state == "absent":
        if item is None:
            return None, {}, {}
        if not module.check_mode:
            api.call(f"{namespace}.delete", item["id"])
        return "delete", pick(item, fields), {}

    if item is None:
        if not module.check_mode:
            api.call(f"{namespace}.create", {**match, **data})
        return "create", {}, {**match, **data}

    before = pick(item, fields)
    changes = differences(item, data)
    if not changes:
        return None, before, before
    if not module.check_mode:
        api.call(f"{namespace}.update", item["id"], changes)
    return "update", before, {**before, **data}


def main():
    spec = api_argument_spec()
    spec.update(
        namespace=dict(type="str", required=True),
        key=dict(type="list", elements="str", required=True, no_log=False),
        entries=dict(type="list", elements="dict", required=True),
        defaults=dict(type="dict", default={}),
        state=dict(type="str", choices=["present", "absent"], default="present"),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    p = module.params
    if not p["key"]:
        module.fail_json(msg="key must name at least one field")
    p["entries"] = [{**p["defaults"], **entry} for entry in p["entries"]]
    for entry in p["entries"]:
        missing = [k for k in p["key"] if k not in entry]
        if missing:
            module.fail_json(msg=f"an item lacks the key field(s) {missing}: {entry}")

    actions, before, after = {}, {}, {}
    api = connect(module)
    with api:
        try:
            for entry in p["entries"]:
                match = {k: entry[k] for k in p["key"]}
                data = {k: v for k, v in entry.items() if k not in match}
                label = " ".join(str(v) for v in match.values())
                action, before[label], after[label] = ensure(api, module, p["namespace"], match, data, p["state"])
                if action:
                    actions[label] = action
        except TrueNASError as exc:
            module.fail_json(msg=str(exc), actions=actions)
    module.exit_json(changed=bool(actions), actions=actions, diff={"before": before, "after": after})


if __name__ == "__main__":
    main()
