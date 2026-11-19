#!/usr/bin/python

DOCUMENTATION = r"""
---
module: truenas_permission
short_description: Give a TrueNAS directory its owner, group and mode, with no ACL beyond them
description:
  - For each entry of O(entries), reads the directory with C(filesystem.stat) and, when its owner, group or mode
    differs, or it carries an ACL beyond its mode, sets all three with C(filesystem.setperm), the ACL stripped.
  - The directory alone, never what is below it, so that files a client wrote keep their own.
  - In check mode a directory that does not exist yet is reported as one that would be set when O(created_paths)
    names it, the mount point of a dataset the same run creates; any other is refused, as a mistyped path.
  - A failure part-way returns what was already done in C(actions).
  - Takes a list so a play logs in once for all of them; TrueNAS rate-limits logins.
options:
  entries:
    description:
      - The directories, each with C(path) (under C(/mnt), and not a pool's own root), C(user), C(group) and
        C(mode), an octal string from C("001") to C("777") such as C("700"). A number is refused, since YAML reads an
        unquoted C(0770) as the decimal 504.
    type: list
    elements: dict
    required: true
  created_paths:
    description:
      - The mount points of the datasets the same run creates, which a dry run cannot stat yet.
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


class Refused(Exception):
    """A directory the module will not set, found after connecting; `action` when it changed the NAS all the same."""

    def __init__(self, message, action=None):
        super().__init__(message)
        self.action = action


def entry_problems(entries):
    """What is wrong with the entries, found before connecting, so a dry run refuses them too."""
    problems = []
    for entry in entries:
        missing = [field for field in FIELDS if not entry.get(field)]
        if missing:
            problems.append(f"{entry.get('path') or entry}: needs {', '.join(missing)}")
            continue
        path = str(entry["path"])
        if set(entry) - set(FIELDS):
            problems.append(f"{path}: unknown field(s) {sorted(set(entry) - set(FIELDS))}")
        # TrueNAS's setperm refuses a path outside a pool, a pool's own root and anything under .zfs; ZFS names no
        # directory "." or "..".
        parts = path.split("/")
        if len(parts) < 4 or parts[:2] != ["", "mnt"] or any(part in ("", ".", "..", ".zfs") for part in parts[2:]):
            problems.append(f"{path}: not a directory inside a pool, such as /mnt/<pool>/<dataset>")
        # A number is refused: YAML reads an unquoted 0770 as the decimal 504. TrueNAS's UnixPerm takes 000 to 777,
        # and setperm refuses 000.
        mode = entry["mode"]
        if not isinstance(mode, str) or not re.fullmatch(r"0?[0-7]{3}", mode) or int(mode, 8) == 0:
            problems.append(f'{path}: mode {mode!r} is not an octal string from "001" to "777", such as "700"')
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


def ensure(api, module, entry, created_paths=()):
    """Set one directory's owner, group and mode. Returns (action or None, before, after)."""
    want = wanted(entry)
    try:
        found = api.call("filesystem.stat", entry["path"])
    except TrueNASCallError as exc:
        # A dataset this run creates does not exist yet in a dry run; any other missing path is a mistake.
        if module.check_mode and "ENOENT" in str(exc):
            if entry["path"] in created_paths:
                return "set", {}, want
            raise Refused(
                f"{entry['path']}: no such directory, and no dataset this run creates is mounted there"
            ) from exc
        raise
    have = current(found)
    if have == want:
        return None, have, have
    if not module.check_mode:
        payload = {k: want[k] for k in ("user", "group", "mode")}
        api.job("filesystem.setperm", {"path": entry["path"], **payload, "options": {"stripacl": True}})
        have_now = current(api.call("filesystem.stat", entry["path"]))
        if have_now != want:
            raise Refused(f"{entry['path']}: filesystem.setperm was accepted but it now has {have_now}", "set")
    changed = sorted(key for key in want if have[key] != want[key])
    return "set " + ",".join(changed), have, want


def main():
    spec = api_argument_spec()
    spec.update(
        entries=dict(type="list", elements="dict", required=True),
        created_paths=dict(type="list", elements="str", default=[]),
    )
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
                action, before[path], after[path] = ensure(api, module, entry, module.params["created_paths"])
                if action:
                    actions[path] = action
        except (TrueNASError, Refused) as exc:
            if getattr(exc, "action", None):
                # setperm ran: the NAS changed, though not as wanted.
                actions[entry["path"]] = exc.action
            module.fail_json(msg=str(exc), changed=bool(actions), actions=actions)
    module.exit_json(changed=bool(actions), actions=actions, diff={"before": before, "after": after})


if __name__ == "__main__":
    main()
