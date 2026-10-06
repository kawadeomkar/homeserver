#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
---
module: truenas_dataset
short_description: Create TrueNAS filesystem datasets or correct their properties
description:
  - Creates each dataset with C(pool.dataset.create) if it is missing, otherwise compares each of its
    properties with C(pool.dataset.query) and sends the ones that differ to C(pool.dataset.update).
  - Never deletes a dataset.
  - In check mode a dataset whose pool is not imported yet is reported as one that would be created.
  - Takes a list so a play logs in once for all of them; TrueNAS rate-limits logins.
options:
  datasets:
    description:
      - The datasets, in order (a parent before its children). Each has C(name), for example
        C(tank/proxmox/vm), and optionally C(properties), C(share_type) and C(create_ancestors).
      - C(properties) are as C(pool.dataset.create) and C(pool.dataset.update) take them, for
        example C(sync), C(compression), C(atime), C(recordsize), C(quota), C(acltype), C(aclmode).
      - Sizes (C(quota), C(refquota), C(reservation), C(refreservation)) are bytes, or a string with a
        binary suffix (C(1T) is 1 TiB). C(0) means none. C(recordsize) is a string such as C(64K).
      - C(INHERIT) is satisfied by any value that is not set locally on the dataset.
      - C(share_type) is a preset applied on create only; the default C(GENERIC) leaves ACL settings
        to C(properties).
      - C(acltype) and C(aclmode) are only changed on an existing dataset when it sets
        C(allow_acl_change), since that changes who can read the files already in it.
    type: list
    elements: dict
    required: true
notes:
  - Connection and timing options (api_host, api_port, api_key, validate_certs, api_cert_sha256,
    api_timeout, api_connect_wait, api_job_timeout, api_login_wait) are shared by every module in this role; see
    module_utils/truenas_api.py.
"""

import re

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.truenas_api import (
    TrueNASError,
    api_argument_spec,
    connect,
)

SIZE_PROPERTIES = {"quota", "refquota", "reservation", "refreservation"}
BLOCK_PROPERTIES = {"recordsize", "special_small_block_size"}
SUFFIXES = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
# Older OpenZFS spells some values differently.
ALIASES = {"posixacl": "posix", "noacl": "off"}
SHARE_TYPES = ["GENERIC", "MULTIPROTOCOL", "NFS", "SMB", "APPS"]


def to_bytes(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    found = re.fullmatch(r"\s*(\d+)\s*([KMGTP]?)I?B?\s*", str(value).upper())
    if not found:
        raise ValueError(f"not a size: {value!r}")
    return int(found.group(1)) * SUFFIXES[found.group(2)]


def matches(key, have, want):
    """Whether the queried property object ``have`` satisfies the desired value ``want``."""
    if not isinstance(have, dict):
        return False
    if isinstance(want, str) and want.upper() == "INHERIT":
        return have.get("source") in ("INHERITED", "DEFAULT", "NONE")
    raw = have.get("rawvalue")
    if key in SIZE_PROPERTIES or key in BLOCK_PROPERTIES:
        try:
            return to_bytes(int(raw or 0)) == to_bytes(want)
        except ValueError:
            return False
    raw = str(raw if raw is not None else have.get("value") or "").lower()
    return ALIASES.get(raw, raw) == ALIASES.get(str(want).lower(), str(want).lower())


def for_api(key, value):
    """Sizes go to the API as integers; everything else as given."""
    if key in SIZE_PROPERTIES and not (isinstance(value, str) and value.upper() == "INHERIT"):
        return to_bytes(value) or None
    return value


def ensure(api, module, spec):
    """Create one dataset or correct its properties. Returns (action or None, before, after)."""
    name = spec["name"].strip("/")
    wanted = spec.get("properties") or {}
    try:
        payload = {key: for_api(key, value) for key, value in wanted.items()}
    except ValueError as exc:
        module.fail_json(msg=f"{name}: {exc}")

    found = api.call("pool.dataset.query", [["id", "=", name]], {"extra": {"retrieve_children": False}})
    if not found:
        pool = name.split("/")[0]
        if not api.call("pool.query", [["name", "=", pool]]):
            if module.check_mode:
                # The pool stage would import it first.
                return "create", {}, dict(wanted)
            module.fail_json(msg=f"cannot create {name}: pool {pool} is not imported")
        if not module.check_mode:
            api.call(
                "pool.dataset.create",
                {
                    "name": name,
                    "type": "FILESYSTEM",
                    "share_type": spec.get("share_type") or "GENERIC",
                    "create_ancestors": bool(spec.get("create_ancestors")),
                    **payload,
                },
            )
        return "create", {}, dict(wanted)

    dataset = found[0]
    if dataset.get("type") != "FILESYSTEM":
        module.fail_json(msg=f"{name} exists but is a {dataset.get('type')}, not a filesystem")
    before = {key: (dataset.get(key) or {}).get("rawvalue") for key in wanted}
    changes = {key: payload[key] for key, want in wanted.items() if not matches(key, dataset.get(key), want)}
    acl = sorted(set(changes) & {"acltype", "aclmode"})
    if acl and not spec.get("allow_acl_change"):
        # Switching ACL type under existing files changes who can read them; never do it implicitly.
        module.fail_json(
            msg=f"{name} already exists with a different {' and '.join(acl)} "
            f"({', '.join(f'{k}={before[k]}' for k in acl)}). Set allow_acl_change on this dataset to change it."
        )
    if not changes:
        return None, before, before
    if not module.check_mode:
        api.call("pool.dataset.update", name, changes)
        after = api.call("pool.dataset.query", [["id", "=", name]], {"extra": {"retrieve_children": False}})[0]
        still = sorted(k for k in changes if not matches(k, after.get(k), wanted[k]))
        if still:
            module.fail_json(msg=f"{name}: the update was accepted but these did not take: {still}")
    return "update " + ",".join(sorted(changes)), before, {**before, **{k: wanted[k] for k in changes}}


def main():
    spec = api_argument_spec()
    spec.update(datasets=dict(type="list", elements="dict", required=True))
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    for entry in module.params["datasets"]:
        if not entry.get("name"):
            module.fail_json(msg=f"a dataset has no name: {entry}")
        if entry.get("share_type", "GENERIC") not in SHARE_TYPES:
            module.fail_json(msg=f"{entry['name']}: share_type must be one of {SHARE_TYPES}")

    actions, before, after = {}, {}, {}
    api = connect(module)
    with api:
        try:
            # In order: a parent listed before its children is created first.
            for entry in module.params["datasets"]:
                name = entry["name"].strip("/")
                action, before[name], after[name] = ensure(api, module, entry)
                if action:
                    actions[name] = action
        except TrueNASError as exc:
            module.fail_json(msg=str(exc), actions=actions)
    module.exit_json(changed=bool(actions), actions=actions, diff={"before": before, "after": after})


if __name__ == "__main__":
    main()
