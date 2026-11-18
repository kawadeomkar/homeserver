#!/usr/bin/python

DOCUMENTATION = r"""
---
module: truenas_permission
short_description: Give a TrueNAS directory its owner, group and mode, with no ACL beyond them
description:
  - For each entry of O(entries), reads the directory with C(filesystem.stat) and, when its owner, group or mode
    differs, or it carries an ACL beyond its mode, sets all three with C(filesystem.setperm), the ACL stripped.
  - The directory alone, never what is below it, so that files a client wrote keep their own.
  - In check mode a directory that does not exist yet, such as a dataset the same run would create, is reported as
    one that would be set.
  - Takes a list so a play logs in once for all of them; TrueNAS rate-limits logins.
options:
  entries:
    description:
      - The directories, each with C(path) (under C(/mnt), and not a pool's own root), C(user), C(group) and
        C(mode), an octal string such as C("700").
    type: list
    elements: dict
    required: true
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

import re
import stat

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.truenas_api import (
    TrueNASCallError,
    TrueNASError,
    api_argument_spec,
    connect,
)

FIELDS = ("path", "user", "group", "mode")


def entry_problems(entries):
    problems = []
    for entry in entries:
        missing = [field for field in FIELDS if not entry.get(field)]
        if missing:
            problems.append(f"{entry.get('path') or entry}: needs {', '.join(missing)}")
            continue
        if set(entry) - set(FIELDS):
            problems.append(f"{entry['path']}: unknown field(s) {sorted(set(entry) - set(FIELDS))}")
        if not re.fullmatch(r"[0-7]{3,4}", str(entry["mode"])):
            problems.append(f'{entry["path"]}: mode {entry["mode"]!r} is not an octal mode such as "700"')
    return problems


def current(found):
    """What a directory has, as the entry says it: names and an octal mode, and whether it carries an ACL."""
    return {
        "user": found.get("user"),
        "group": found.get("group"),
        "mode": f"{stat.S_IMODE(found.get('mode') or 0):03o}",
        "acl": bool(found.get("acl")),
    }


def wanted(entry):
    return {"user": entry["user"], "group": entry["group"], "mode": f"{int(str(entry['mode']), 8):03o}", "acl": False}


def ensure(api, module, entry):
    """Set one directory's owner, group and mode. Returns (action or None, before, after)."""
    want = wanted(entry)
    try:
        found = api.call("filesystem.stat", entry["path"])
    except TrueNASCallError as exc:
        # A dataset this run creates does not exist yet in a dry run.
        if module.check_mode and "ENOENT" in str(exc):
            return "set", {}, want
        raise
    have = current(found)
    if have == want:
        return None, have, have
    if not module.check_mode:
        payload = {k: want[k] for k in ("user", "group", "mode")}
        api.job("filesystem.setperm", {"path": entry["path"], **payload, "options": {"stripacl": True}})
        have_now = current(api.call("filesystem.stat", entry["path"]))
        if have_now != want:
            module.fail_json(msg=f"{entry['path']}: filesystem.setperm was accepted but it now has {have_now}")
    changed = sorted(key for key in want if have[key] != want[key])
    return "set " + ",".join(changed), have, want


def main():
    spec = api_argument_spec()
    spec.update(entries=dict(type="list", elements="dict", required=True))
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    problems = entry_problems(module.params["entries"])
    if problems:
        module.fail_json(msg="; ".join(problems))

    actions, before, after = {}, {}, {}
    api = connect(module)
    with api:
        try:
            for entry in module.params["entries"]:
                path = entry["path"]
                action, before[path], after[path] = ensure(api, module, entry)
                if action:
                    actions[path] = action
        except TrueNASError as exc:
            module.fail_json(msg=str(exc), actions=actions)
    module.exit_json(changed=bool(actions), actions=actions, diff={"before": before, "after": after})


if __name__ == "__main__":
    main()
