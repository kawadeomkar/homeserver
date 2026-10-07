#!/usr/bin/python

DOCUMENTATION = r"""
---
module: truenas_config
short_description: Set fields of a TrueNAS singleton configuration
description:
  - Reads C(<namespace>.config), compares the fields given in O(settings), and calls
    C(<namespace>.update) with the ones that differ. Works for C(system.general), C(nfs),
    C(systemdataset), C(network.configuration) and similar namespaces.
  - Only the managed fields are returned, never the whole configuration. Some configurations carry
    secrets (C(system.general.config) includes the UI certificate's private key).
  - A field holding an object with an C(id), such as C(ui_certificate), is compared by that id.
options:
  namespace:
    description: API namespace, for example C(system.general).
    type: str
    required: true
  settings:
    description: Fields to set, as the update method takes them.
    type: dict
    required: true
  match:
    description:
      - Fields that must also have these values, compared but never sent. If one differs, all of
        O(settings) are sent even when they already match.
      - Used for C(systemdataset), whose C(pool) reads as C(boot-pool) even when no pool was ever
        chosen (C(pool_set) false), in which case TrueNAS moves it to the first data pool imported.
    type: dict
    default: {}
  update_options:
    description: Extra fields sent with an update but not compared, such as C(ui_restart_delay).
    type: dict
    default: {}
  job:
    description: The update method is a job; wait for it.
    type: bool
    default: false
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
)


def normalised(config, keys):
    out = {}
    for key in keys:
        value = config.get(key)
        if isinstance(value, dict) and "id" in value:
            value = value["id"]
        out[key] = value
    return out


def main():
    spec = api_argument_spec()
    spec.update(
        namespace=dict(type="str", required=True),
        settings=dict(type="dict", required=True),
        match=dict(type="dict", default={}),
        update_options=dict(type="dict", default={}),
        job=dict(type="bool", default=False),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    namespace = module.params["namespace"]
    settings = module.params["settings"]
    match = module.params["match"]
    keys = list(dict.fromkeys([*settings, *match]))

    api = connect(module)
    with api:
        try:
            current = normalised(api.call(f"{namespace}.config"), keys)
            changes = differences(current, settings)
            mismatched = differences(current, match)
            result = {
                "changed": bool(changes or mismatched),
                "config": current,
                "diff": {"before": current, "after": {**current, **settings, **match}},
            }
            if not result["changed"] or module.check_mode:
                module.exit_json(**result)

            payload = {**(changes or settings), **module.params["update_options"]}
            if module.params["job"]:
                api.job(f"{namespace}.update", payload)
            else:
                api.call(f"{namespace}.update", payload)

            after = normalised(api.call(f"{namespace}.config"), keys)
            still = differences(after, {**settings, **match})
            if still:
                module.fail_json(
                    msg=f"{namespace}.update was accepted but these fields did not take: {sorted(still)}",
                    config=after,
                )
            result["config"] = after
        except TrueNASError as exc:
            module.fail_json(msg=str(exc))
    module.exit_json(**result)


if __name__ == "__main__":
    main()
