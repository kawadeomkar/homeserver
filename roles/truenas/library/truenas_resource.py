#!/usr/bin/python

DOCUMENTATION = r"""
---
module: truenas_resource
short_description: Create, update or delete items of a TrueNAS collection
description:
  - For each entry of O(entries), finds the one item whose O(key) fields are equal with
    C(<namespace>.query), then creates it, updates the other fields that differ, or deletes it.
    Fails if more than one item matches.
  - Used for C(sharing.nfs) (key C(path)), C(system.ntpserver) (key C(address)), C(alertservice) (key
    C(name), with O(update_whole)) and C(sharing.smb) (key C(name), with O(update_together)).
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
  update_whole:
    description:
      - Send the whole entry, its key fields and every managed field, to C(<namespace>.update), rather than the
        fields that differ alone. For namespaces whose update takes the full model, such as C(alertservice) in
        TrueNAS 25.10, which refuses an update without its name and attributes.
    type: bool
    default: false
  update_together:
    description:
      - Fields that go to C(<namespace>.update) as a group. When any of them differs, every one of them the entry
        gives is sent, a nested object as the current one with the wanted keys laid over it; when none differs, none
        is sent. When a plain field of the group changes too, a nested object goes as the entry gives it, since the
        current one belongs to the old value and TrueNAS refuses keys the new one does not have.
      - For C(sharing.smb), whose update refuses C(options) without C(purpose), and fills C(options) in from their
        defaults when C(purpose) comes alone (TrueNAS 25.10.7's C(normalize_legacy_fields)), so the two go together.
        Laying the wanted options over the current ones keeps what the entry does not give, such as the Time Machine
        volume's C(vuid), as it is; a new C(purpose) takes the entry's options alone.
      - Cannot be combined with O(update_whole).
    type: list
    elements: str
    default: []
attributes:
  check_mode:
    support: full
  diff_mode:
    support: full
  platform:
    platforms: posix
extends_documentation_fragment:
  - ansible.builtin.action_common_attributes
  - truenas_api
  - truenas_api.host
"""

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.truenas_api import (
    TrueNASError,
    api_argument_spec,
    connect,
    differences,
    pick,
)


def grouped(item, data, changes, together):
    """The update's fields: those that differ, and with any field of the group every one the entry gives, a nested
    object as the current one with the wanted keys laid over it, unless a plain field of the group changes too: the
    current object then belongs to the old value (a share's options to its old purpose), so it goes as wanted."""
    if not set(changes) & set(together):
        return changes
    given = [key for key in together if key in data]
    relaid = not any(key in changes and not isinstance(data[key], dict) for key in given)
    group = {}
    for key in given:
        have, want = item.get(key), data[key]
        group[key] = {**have, **want} if relaid and isinstance(have, dict) and isinstance(want, dict) else want
    return {**changes, **group}


def ensure(api, module, namespace, match, data, state, update_whole=False, update_together=()):
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
    payload = {**match, **data} if update_whole else grouped(item, data, changes, update_together)
    if not module.check_mode:
        api.call(f"{namespace}.update", item["id"], payload)
    # What the item holds after the update: the fields sent, a nested object laid over as it was sent.
    return "update", before, {**before, **pick(payload, fields)}


def main():
    spec = api_argument_spec()
    spec.update(
        namespace=dict(type="str", required=True),
        key=dict(type="list", elements="str", required=True, no_log=False),
        entries=dict(type="list", elements="dict", required=True),
        defaults=dict(type="dict", default={}),
        state=dict(type="str", choices=["present", "absent"], default="present"),
        update_whole=dict(type="bool", default=False),
        update_together=dict(type="list", elements="str", default=[]),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    p = module.params
    if not p["key"]:
        module.fail_json(msg="key must name at least one field")
    if p["update_whole"] and p["update_together"]:
        module.fail_json(msg="update_whole sends every field already: give it or update_together, not both")
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
                action, before[label], after[label] = ensure(
                    api, module, p["namespace"], match, data, p["state"], p["update_whole"], p["update_together"]
                )
                if action:
                    actions[label] = action
        except TrueNASError as exc:
            module.fail_json(msg=str(exc), actions=actions)
    module.exit_json(changed=bool(actions), actions=actions, diff={"before": before, "after": after})


if __name__ == "__main__":
    main()
