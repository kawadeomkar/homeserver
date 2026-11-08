#!/usr/bin/python

DOCUMENTATION = r"""
---
module: opnsense_info
short_description: Connect to OPNsense and report its release and plugins, read-only
description:
  - Reads the router's firmware information through its API (C(GET /api/core/firmware/info)) and reports its
    release, its kernel and base sets, and the plugins installed.
  - Makes no changes. The firmware information lists the packages the router already knows of; it runs no update
    check.
  - Runs on the controller, with Python's standard library alone.
options: {}
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
api_host:
  description: The address that answered.
  type: str
  returned: success
version:
  description: The version opnsense-version prints, with any hotfix suffix, for example C(26.7.8_2).
  type: str
  returned: success
release:
  description:
    - The version without its hotfix suffix, for example C(26.7.8), which the version test compares; empty for a
      development version.
  type: str
  returned: success
series:
  description: The release series, for example C(26.7).
  type: str
  returned: success
kernel:
  description: The installed kernel set's version, which carries FreeBSD's fixes.
  type: str
  returned: success
base:
  description: The installed base set's version.
  type: str
  returned: success
plugins:
  description: The plugins installed, each by name and version, sorted by name.
  type: list
  elements: dict
  returned: success
  sample:
    - {name: os-isc-dhcp, version: "1.0_1"}
    - {name: os-tailscale, version: "1.4"}
"""

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.opnsense_api import OPNsenseError, api_argument_spec, open_client, release_of


def installed(entries):
    """The installed entries of a firmware listing, each {name, version}, sorted by name."""
    return sorted(
        ({"name": e.get("name"), "version": e.get("version")} for e in entries or [] if str(e.get("installed")) == "1"),
        key=lambda e: str(e["name"]),
    )


def gather(info):
    """What the module returns, from firmware/info's answer: never the whole product record, whose mirror and
    repository settings are the router's business."""
    product = info.get("product") if isinstance(info.get("product"), dict) else {}
    packages = {p["name"]: p["version"] for p in installed(info.get("package"))}
    version = str(info.get("product_version") or product.get("product_version") or "")
    return {
        "version": version,
        "release": release_of(version),
        "series": str(product.get("product_series") or ""),
        "kernel": str(packages.get("kernel") or ""),
        "base": str(packages.get("base") or ""),
        "plugins": installed(info.get("plugin")),
    }


def main():
    module = AnsibleModule(argument_spec=api_argument_spec(), supports_check_mode=True)
    try:
        info = open_client(module).get("/api/core/firmware/info")
    except OPNsenseError as exc:
        module.fail_json(msg=str(exc))
    if not isinstance(info, dict):
        module.fail_json(msg="the router's firmware information is not a JSON object")
    module.exit_json(changed=False, api_host=module.params["api_host"], **gather(info))


if __name__ == "__main__":
    main()
