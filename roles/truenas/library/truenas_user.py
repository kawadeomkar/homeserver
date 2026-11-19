#!/usr/bin/python

DOCUMENTATION = r"""
---
module: truenas_user
short_description: Create TrueNAS local users or correct their settings, and take other logins out of SMB
description:
  - Creates each user of O(users) that is missing, each with a group of its own, and corrects the fields its entry
    gives (C(full_name), C(smb), C(shell), C(home), C(locked), C(password_disabled)) on one that exists. Never deletes
    a user, and refuses one that is TrueNAS's own (built in, or its first administrator).
  - A password is set when the user is created, and after that only when O(users_reset) names the user, or when
    TrueNAS needs it again because the update turns C(smb) on or C(password_disabled) off. It is never compared, so
    the module never reads a password hash; it asks C(user.query) for named fields alone. A password is needed only
    when one would be sent, so a user the run only closes needs none.
  - Takes out of SMB each login O(smb_closed_users) names, without managing it otherwise, and with O(api_user_smb)
    false the user the API key logs in as (C(auth.me)), so that starting the SMB service does not make their
    passwords network logins.
  - Returns the local SMB logins as they stand after its changes, or would in check mode, for a check just before
    the SMB service starts. A failure part-way returns what was already done in C(actions).
  - Takes a list so a play logs in once for all of them; TrueNAS rate-limits logins.
options:
  users:
    description:
      - The users, each with C(username) and C(smb), and optionally C(uid), C(full_name) (the username when not
        given), C(shell), C(home), C(locked) and C(password_disabled).
      - C(smb) is required because TrueNAS makes every new user an SMB login unless told otherwise.
      - C(uid) is used when the user is created, and an existing user with another uid is refused; TrueNAS cannot
        change one. Pin it so that a reinstalled NAS that imports the pools gives the files back to the same number.
      - An SMB user needs a password and cannot have C(password_disabled).
    type: list
    elements: dict
    required: true
  passwords:
    description:
      - Username to password, for the users that are created, named in O(users_reset), or need one again. Never
        compared and never returned.
    type: dict
    default: {}
  users_reset:
    description:
      - Users whose password is set again from O(passwords) on this run, whether it changed or not. A one-run switch
        for rotating a password, since the module cannot see the one TrueNAS holds.
    type: list
    elements: str
    default: []
  smb_closed_users:
    description:
      - Local logins taken out of SMB by name, such as one made by hand. Never created, and otherwise left alone; one
        that does not exist is nothing to do. A name in O(users) too is refused.
    type: list
    elements: str
    default: []
  api_user_smb:
    description:
      - Set to false to take the user the API key logs in as out of SMB. Unset leaves it alone. True is refused,
        since turning SMB on needs the user's password, which the module does not have.
    type: bool
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

RETURN = r"""
actions:
  description: What was done, or would be in check mode, to each user, by username.
  type: dict
  returned: always
  sample: {tm-laptop: create, api-admin: update smb}
smb_users:
  description: The local logins with SMB on after the changes, or as they would be in check mode, by name.
  type: list
  elements: str
  returned: success
  sample: [tm-laptop]
"""

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.truenas_api import (
    TrueNASError,
    api_argument_spec,
    connect,
    pick,
)

# The fields an entry may give and the module corrects. Never the password hashes, which user.query returns too.
MANAGED = ("full_name", "smb", "shell", "home", "locked", "password_disabled")
ENTRY_FIELDS = frozenset({"username", "uid", *MANAGED})
# What the module asks user.query for: no hash, no password history.
SELECT = ["id", "uid", "username", "builtin", "immutable", *MANAGED]
# A password in the diff: never its value.
CHANGED_SECRET = "<secret, changed>"
# The local logins SMB would let in: the connection's check reads the same.
SMB_LOGINS = [["local", "=", True], ["builtin", "=", False], ["smb", "=", True]]


class Refused(Exception):
    """A user the module will not change, found after connecting."""


def entry_problems(users, passwords, users_reset, smb_closed_users, api_user_smb):
    """What is wrong with the options, found before connecting, so a dry run refuses them too."""
    problems = []
    names = [user.get("username") for user in users]
    for user in users:
        name = user.get("username")
        if not name:
            problems.append(f"a user has no username: {sorted(user)}")
            continue
        unknown = sorted(set(user) - ENTRY_FIELDS)
        if unknown:
            problems.append(f"{name}: unknown field(s) {unknown}; the fields are {sorted(ENTRY_FIELDS)}")
        if not isinstance(user.get("smb"), bool):
            problems.append(
                f"{name}: smb must be true or false, since TrueNAS makes a new user an SMB login unless told"
            )
        if user.get("smb") and user.get("password_disabled"):
            problems.append(f"{name}: an SMB user needs a password, so it cannot have password_disabled")
    problems += [f"{name}: given twice" for name in sorted({n for n in names if n and names.count(n) > 1})]
    for name in users_reset:
        if name not in names:
            problems.append(f"{name}: in users_reset, but not one of the users")
        elif next(u for u in users if u.get("username") == name).get("password_disabled"):
            problems.append(f"{name}: in users_reset, but its password is disabled")
        elif not passwords.get(name):
            problems.append(f"{name}: in users_reset, but passwords has none for it")
    for name in smb_closed_users:
        if name in names:
            problems.append(f"{name}: in smb_closed_users and in users; give its smb in users alone")
    if api_user_smb is True:
        problems.append("api_user_smb: only false is supported; turning SMB on needs the user's password")
    return problems


def find(api, username):
    found = api.call("user.query", [["username", "=", username], ["local", "=", True]], {"select": SELECT})
    return found[0] if found else None


def ensure(api, module, user, password, reset):
    """Create one user or correct it. Returns (action or None, before, after)."""
    name = user["username"]
    wanted = {key: user[key] for key in MANAGED if key in user}
    found = find(api, name)
    if found is None:
        payload = {"username": name, "full_name": name, **wanted, "group_create": True}
        if "uid" in user:
            payload["uid"] = user["uid"]
        if not user.get("password_disabled"):
            if not password:
                raise Refused(f"{name}: no password in passwords, and the user needs one to be created")
            payload["password"] = password
        if not module.check_mode:
            # user.create answers with the user and the password it was given: neither is kept.
            api.call("user.create", payload)
        after = {**pick(payload, ["uid", *MANAGED]), **({"password": CHANGED_SECRET} if "password" in payload else {})}
        return "create", {}, after

    if found.get("builtin") or found.get("immutable"):
        raise Refused(f"{name} is one of TrueNAS's own users; the role does not manage it")
    if "uid" in user and found.get("uid") != user["uid"]:
        uid = found.get("uid")
        raise Refused(f"{name} exists with uid {uid}, not {user['uid']}; TrueNAS cannot change a user's uid")
    before = pick(found, list(wanted))
    changes = {key: value for key, value in wanted.items() if found.get(key) != value}
    # TrueNAS refuses smb turned on without the password, and a password re-enabled is useless without one.
    needs_password = reset or changes.get("smb") is True or changes.get("password_disabled") is False
    if not changes and not needs_password:
        return None, before, before
    if needs_password and not password:
        raise Refused(f"{name}: no password in passwords, and turning smb on or the password back on needs one")
    payload = {**changes, **({"password": password} if needs_password else {})}
    if not module.check_mode:
        api.call("user.update", found["id"], payload)
    action = "update " + ",".join(sorted(changes)) if changes else "reset password"
    if changes and needs_password:
        action += ", password"
    after = {**before, **changes, **({"password": CHANGED_SECRET} if needs_password else {})}
    return action, before, after


def close_smb(api, module, name, found, what):
    """One login out of SMB, and nothing else about it. Returns (action or None, before, after)."""
    before = {"smb": found.get("smb")}
    if not found.get("smb"):
        return None, before, before
    if found.get("builtin") or found.get("immutable"):
        raise Refused(f"{name}, {what}, is one of TrueNAS's own users; its smb cannot be changed")
    if not module.check_mode:
        api.call("user.update", found["id"], {"smb": False})
    return "update smb", before, {"smb": False}


def take_api_user_out_of_smb(api, module, names):
    """The user the API key logs in as, out of SMB. Returns (username, action or None, before, after)."""
    name = (api.call("auth.me") or {}).get("pw_name")
    found = find(api, name) if name else None
    if found is None:
        raise Refused("auth.me names no local user, so the API key's user cannot be taken out of SMB")
    if name in names:
        raise Refused(f"{name}, the API key's user, is also in users; give its smb there or with api_user_smb")
    return (name, *close_smb(api, module, name, found, "the API key's user"))


def smb_logins(api, module, after):
    """The local SMB logins once the changes are made: read again, and in check mode as the changes would leave them."""
    logins = {u["username"] for u in api.call("user.query", SMB_LOGINS, {"select": ["username"]})}
    if module.check_mode:
        for name, state in after.items():
            if "smb" in state:
                (logins.add if state["smb"] else logins.discard)(name)
    return sorted(logins)


def main():
    spec = api_argument_spec()
    spec.update(
        users=dict(type="list", elements="dict", required=True),
        passwords=dict(type="dict", default={}, no_log=True),
        users_reset=dict(type="list", elements="str", default=[]),
        smb_closed_users=dict(type="list", elements="str", default=[]),
        api_user_smb=dict(type="bool"),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    p = module.params
    problems = entry_problems(p["users"], p["passwords"], p["users_reset"], p["smb_closed_users"], p["api_user_smb"])
    if problems:
        module.fail_json(msg="; ".join(problems))

    actions, before, after = {}, {}, {}
    api = connect(module)
    with api:
        try:
            for user in p["users"]:
                name = user["username"]
                action, before[name], after[name] = ensure(
                    api, module, user, p["passwords"].get(name), name in p["users_reset"]
                )
                if action:
                    actions[name] = action
            for name in p["smb_closed_users"]:
                found = find(api, name)
                if found is not None:
                    action, before[name], after[name] = close_smb(api, module, name, found, "in smb_closed_users")
                    if action:
                        actions[name] = action
            if p["api_user_smb"] is False:
                # Named in smb_closed_users too, it is closed already: both mean out of SMB.
                names = {user["username"] for user in p["users"]}
                name, action, was, now = take_api_user_out_of_smb(api, module, names)
                # Closed above already when smb_closed_users names it too: its diff keeps what it was then.
                if name not in before:
                    before[name], after[name] = was, now
                if action:
                    actions[name] = action
            logins = smb_logins(api, module, after)
        except (TrueNASError, Refused) as exc:
            module.fail_json(msg=str(exc), changed=bool(actions), actions=actions)
    module.exit_json(changed=bool(actions), actions=actions, smb_users=logins, diff={"before": before, "after": after})


if __name__ == "__main__":
    main()
