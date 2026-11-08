#!/usr/bin/python

DOCUMENTATION = r"""
---
module: opnsense_backup
short_description: Keep encrypted copies of the router's configuration on the controller
description:
  - Reads the router's configuration history (C(GET /api/core/backup/backups/this)), whose newest entry is the running
    configuration, and compares it with the newest backup in O(backup_dir), by the history entry's id.
  - With no backup yet, or when the newest backup's revision is still in the router's history and the router has a
    newer one, it downloads that revision (C(GET /api/core/backup/download/this/<id>)) and writes it to O(backup_dir)
    as the id names it, in OPNsense's own encrypted format (AES-256-CBC, PBKDF2-SHA512, 100000 iterations), which the
    router's Restore page and its installer read with O(password).
  - When the router runs the newest backup's revision, it does nothing.
  - When the newest backup's revision is not in the router's history, the router does not descend from it (restored or
    reinstalled by hand, or more revisions since than the router keeps). It then writes nothing and says so, unless
    O(accept) makes the router's configuration the newest backup.
  - Never deletes a backup, and never returns the configuration.
options:
  backup_dir:
    description: Where the backups go, made with mode 0700. One readable by others is refused.
    type: path
    required: true
  password:
    description: The password the backups are encrypted with.
    type: str
    required: true
  version:
    description: The router's version, written in the backup's header, as the router writes it.
    type: str
    default: ""
  accept:
    description: Back the router's configuration up even when it does not descend from the newest backup.
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
  - opnsense_api
  - opnsense_api.key
"""

RETURN = r"""
state:
  description:
    - C(first) (no backup until now), C(changed) (the router has a newer revision), C(in_sync) (the router runs the
      newest backup's revision), C(diverged) (it does not descend from the newest backup), or C(accepted) (it did not,
      and O(accept) backed it up all the same).
  type: str
  returned: success
router:
  description: The router's newest revision, the one it runs.
  type: dict
  returned: success
  sample: {id: config-1762600000.1234.xml, time: "1762600000.12", time_iso: "2025-11-08T11:06:40+00:00",
           description: "/firewall_rules.php made changes", username: "root@192.0.2.5"}
newest:
  description: The newest backup's id before this run, empty when there was none.
  type: str
  returned: success
backup_file:
  description:
    - The backup written, or the one a dry run would write; empty when none.
    - Not named path, for which AnsibleModule would add the file's attributes, its state among them.
  type: str
  returned: success
backups:
  description: How many backups O(backup_dir) holds after this run.
  type: int
  returned: success
"""

import os
import re
import stat
import tempfile

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.opnsense_api import OPNsenseError, api_argument_spec, open_client, pick
from ansible.module_utils.opnsense_crypt import CryptError, decrypt, encrypt

# A history entry's id: config-<microtime>.xml, with _<nanoseconds> when two saves shared a microsecond
# (OPNsense/Core/Config.php's backup()).
BACKUP_ID = re.compile(r"^config-(\d+)(?:\.(\d+))?(?:_(\d+))?\.xml$")


def order(backup_id):
    """A backup's place in time, from its id, or None for a name that is not one."""
    match = BACKUP_ID.match(backup_id)
    if not match:
        return None
    seconds, fraction, nanoseconds = match.groups()
    return (float(f"{seconds}.{fraction or 0}"), int(nanoseconds or -1))


def stored(backup_dir):
    """The ids of the backups in `backup_dir`, oldest first."""
    if not os.path.isdir(backup_dir):
        return []
    found = [name for name in os.listdir(backup_dir) if order(name) is not None]
    return sorted(found, key=order)


def decide(history_ids, router_id, newest, accept):
    """What to do, from the router's history ids, its newest revision and the newest backup's id."""
    if not newest:
        return "first"
    if router_id == newest:
        return "in_sync"
    if newest in history_ids:
        return "changed"
    return "accepted" if accept else "diverged"


def write_private(directory, name, text):
    """Write `text` to directory/name with mode 0600, whole or not at all."""
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(temporary, os.path.join(directory, name))
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def main():
    spec = api_argument_spec()
    spec.update(
        backup_dir=dict(type="path", required=True),
        password=dict(type="str", required=True, no_log=True),
        version=dict(type="str", default=""),
        accept=dict(type="bool", default=False),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    backup_dir = module.params["backup_dir"]
    if os.path.isdir(backup_dir) and stat.S_IMODE(os.stat(backup_dir).st_mode) & 0o077:
        module.fail_json(msg=f"{backup_dir} holds the router's configuration and is readable by others: chmod 700 it")

    try:
        api = open_client(module)
        history = api.get("/api/core/backup/backups/this").get("items") or []
    except OPNsenseError as exc:
        module.fail_json(msg=str(exc))
    history = [h for h in history if isinstance(h, dict) and order(str(h.get("id", ""))) is not None]
    if not history:
        module.fail_json(msg="the router lists no configuration revision (api/core/backup/backups/this)")
    router = max(history, key=lambda h: order(h["id"]))
    backups = stored(backup_dir)
    newest = backups[-1] if backups else ""
    state = decide({h["id"] for h in history}, router["id"], newest, module.params["accept"])
    result = dict(
        state=state,
        router=pick(router, ["id", "time", "time_iso", "description", "username"]),
        newest=newest,
        backup_file="",
        backups=len(backups),
    )
    if state in ("in_sync", "diverged"):
        module.exit_json(changed=False, **result)

    result["backup_file"] = os.path.join(backup_dir, router["id"])
    if module.check_mode:
        module.exit_json(changed=True, **result)

    try:
        data = api.download(f"/api/core/backup/download/this/{router['id']}")
    except OPNsenseError as exc:
        module.fail_json(msg=str(exc), **result)
    if b"<opnsense" not in data[:4096]:
        module.fail_json(msg=f"the router's revision {router['id']} downloaded as no OPNsense configuration", **result)
    try:
        text = encrypt(data, module.params["password"], version=module.params["version"])
        # Proven readable before it is kept: a backup that cannot be restored is no backup.
        if decrypt(text, module.params["password"]) != data:
            module.fail_json(msg="the encrypted backup did not read back as the configuration", **result)
    except CryptError as exc:
        module.fail_json(msg=str(exc), **result)
    os.makedirs(backup_dir, mode=0o700, exist_ok=True)
    write_private(backup_dir, router["id"], text)
    result["backups"] = len(stored(backup_dir))
    module.exit_json(changed=True, **result)


if __name__ == "__main__":
    main()
