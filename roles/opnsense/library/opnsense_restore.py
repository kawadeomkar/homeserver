#!/usr/bin/python

DOCUMENTATION = r"""
---
module: opnsense_restore
short_description: Restore a whole configuration onto an OPNsense router through its web UI
description:
  - OPNsense's API cannot upload a configuration file, so this logs in to the web UI as O(gui_user) and restores one
    on its Restore page (C(diag_backup.php)), as a person would - the whole file, the history flushed, the console
    settings kept, then a reboot.
  - The file goes up still encrypted, with O(backup_password) for the router to decrypt it with; it is decrypted here
    first only to prove the password, never written anywhere.
  - With O(fingerprint_only) it only reads the SHA-256 of the certificate the router presents, sending nothing else,
    so that a fresh router's certificate can be checked before a password goes to it.
  - In check mode it logs in and opens the Restore page, which proves the password and the privilege, and restores
    nothing.
options:
  gui_user:
    description: The web UI user to log in as; on a fresh install, root.
    type: str
    default: root
  gui_password:
    description:
      - That user's password; on a fresh install, the one set in the installer.
      - Required unless O(fingerprint_only).
    type: str
    default: ""
  backup_file:
    description: The backup to restore, in OPNsense's encrypted format. Required unless O(fingerprint_only).
    type: path
  backup_password:
    description: The backup's password. Required unless O(fingerprint_only).
    type: str
    default: ""
  reboot:
    description: Reboot the router once the configuration is restored, as OPNsense recommends.
    type: bool
    default: true
  fingerprint_only:
    description: Only read and return the certificate's fingerprint.
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
"""

RETURN = r"""
fingerprint:
  description: The SHA-256 of the certificate the router presents, colon-separated.
  type: str
  returned: always
restored:
  description: Whether the router said the configuration was restored.
  type: bool
  returned: success
rebooting:
  description: Whether it is rebooting into it. False when its interfaces do not match the configuration's.
  type: bool
  returned: success
would_restore:
  description: In check mode, whether a real run would restore - the login and the backup's password proven.
  type: bool
  returned: success
interface_mismatch:
  description: The router said the configuration's interfaces are not all on this machine, and postponed the reboot.
  type: bool
  returned: success
backup_version:
  description: The version the backup's header names, the release it was made on.
  type: str
  returned: success
"""

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.opnsense_api import (
    OPNsenseError,
    OPNsenseWeb,
    alerts,
    connection_argument_spec,
    peer_fingerprint,
)
from ansible.module_utils.opnsense_crypt import CryptError, decrypt, headers, is_encrypted

PAGE = "/diag_backup.php"
RESTORED = "The configuration has been restored."
MISMATCH = "Interfaces do not seem to match"


def main():
    spec = connection_argument_spec()
    spec.update(
        gui_user=dict(type="str", default="root"),
        gui_password=dict(type="str", default="", no_log=True),
        backup_file=dict(type="path"),
        backup_password=dict(type="str", default="", no_log=True),
        reboot=dict(type="bool", default=True),
        fingerprint_only=dict(type="bool", default=False),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    params = module.params
    host, port = params["api_host"], params["api_port"]
    if not host:
        module.fail_json(msg="no router address given")
    try:
        fingerprint = peer_fingerprint(host, port, params["api_timeout"])
    except OPNsenseError as exc:
        module.fail_json(msg=str(exc), fingerprint="")
    result = dict(
        fingerprint=fingerprint,
        restored=False,
        would_restore=False,
        rebooting=False,
        interface_mismatch=False,
        backup_version="",
    )
    if params["fingerprint_only"]:
        module.exit_json(changed=False, **result)

    missing = [name for name in ("gui_password", "backup_file", "backup_password") if not params[name]]
    if missing:
        module.fail_json(msg=f"missing {', '.join(missing)}", **result)
    try:
        with open(params["backup_file"]) as handle:
            text = handle.read()
    except OSError as exc:
        module.fail_json(msg=f"cannot read {params['backup_file']}: {exc}", **result)
    if not is_encrypted(text):
        module.fail_json(msg=f"{params['backup_file']} is not in OPNsense's encrypted format", **result)
    result["backup_version"] = headers(text).get("version", "")
    try:
        decrypt(text, params["backup_password"])
    except CryptError as exc:
        module.fail_json(msg=f"{params['backup_file']}: {exc}", **result)

    try:
        web = OPNsenseWeb(
            host,
            port,
            validate_certs=params["validate_certs"],
            cert_sha256=params["api_cert_sha256"],
            timeout=params["api_timeout"],
        )
        web.login(params["gui_user"], params["gui_password"], PAGE)
        _, token = web.page(PAGE)
        if module.check_mode:
            result["would_restore"] = True
            module.exit_json(changed=True, **result)
        fields = {
            token[0]: token[1],
            "restore": "Restore configuration",
            "decrypt": "on",
            "decrypt_password": params["backup_password"],
            "keepconsole": "on",
            "flush_history": "on",
        }
        if params["reboot"]:
            fields["rebootafterrestore"] = "on"
        name = params["backup_file"].rsplit("/", 1)[-1]
        page = web.post_form(PAGE, fields, {"conffile": (name, text.encode())})
    except OPNsenseError as exc:
        module.fail_json(msg=str(exc), **result)

    errors = alerts(page, "danger")
    said = alerts(page, "info")
    if errors:
        module.fail_json(msg="the router refused the restore: " + " ".join(errors), **result)
    if not any(RESTORED in line for line in said):
        module.fail_json(msg="the router did not say the configuration was restored: " + " ".join(said), **result)
    result["restored"] = True
    result["interface_mismatch"] = any(MISMATCH in line for line in said)
    result["rebooting"] = params["reboot"] and not result["interface_mismatch"]
    module.exit_json(changed=True, **result)


if __name__ == "__main__":
    main()
