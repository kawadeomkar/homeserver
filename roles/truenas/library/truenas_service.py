#!/usr/bin/python

DOCUMENTATION = r"""
---
module: truenas_service
short_description: Enable, disable, start or stop TrueNAS services
description:
  - For each service in O(services), C(enabled) is whether it starts at boot and C(state) is whether
    it runs now. Either may be left out to leave that aspect alone.
  - Takes all services at once so a play logs in once; TrueNAS rate-limits logins.
options:
  services:
    description:
      - >-
        Service name, as C(service.query) reports it (C(nfs), C(cifs), C(ssh), ...), mapped to
        C({enabled: <bool>, state: started|stopped}).
    type: dict
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

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.truenas_api import (
    TrueNASError,
    api_argument_spec,
    connect,
    pick,
)


def ensure(api, module, name, enabled, state):
    """Bring one service to the wanted state. Returns (actions, before, after)."""
    found = api.call("service.query", [["service", "=", name]])
    if not found:
        module.fail_json(msg=f"TrueNAS has no service named {name!r}")
    service = found[0]
    before = pick(service, ["enable", "state"])
    after = dict(before)
    actions = []
    if enabled is not None and service["enable"] != enabled:
        actions.append("enable" if enabled else "disable")
        after["enable"] = enabled
    running = service["state"] == "RUNNING"
    if state == "started" and not running:
        actions.append("start")
    elif state == "stopped" and running:
        actions.append("stop")
    if state:
        after["state"] = "RUNNING" if state == "started" else "STOPPED"
    if not actions or module.check_mode:
        return actions, before, after

    if enabled is not None and service["enable"] != enabled:
        api.call("service.update", service["id"], {"enable": enabled})
    # silent=False makes a failed start raise with the service's own logs instead of quietly
    # returning false.
    if "start" in actions:
        api.job("service.control", "START", name, {"silent": False})
    elif "stop" in actions:
        api.job("service.control", "STOP", name, {"silent": False})
    final = pick(api.call("service.query", [["service", "=", name]])[0], ["enable", "state"])
    if state and final["state"] != after["state"]:
        module.fail_json(msg=f"{name} is {final['state']} after trying to {actions[-1]} it")
    return actions, before, final


def main():
    spec = api_argument_spec()
    spec.update(services=dict(type="dict", required=True))
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    wanted = {}
    for name, want in module.params["services"].items():
        want = want or {}
        unknown = sorted(set(want) - {"enabled", "state"})
        if unknown:
            module.fail_json(msg=f"service {name}: unknown field(s) {unknown}; use enabled and state")
        if "enabled" in want and not isinstance(want["enabled"], bool):
            module.fail_json(msg=f"service {name}: enabled must be true or false")
        if want.get("state") not in (None, "started", "stopped"):
            module.fail_json(msg=f"service {name}: state must be started or stopped")
        wanted[name] = (want.get("enabled"), want.get("state"))

    actions, before, after = {}, {}, {}
    api = connect(module)
    with api:
        try:
            for name, (enabled, state) in wanted.items():
                done, before[name], after[name] = ensure(api, module, name, enabled, state)
                if done:
                    actions[name] = done
        except TrueNASError as exc:
            module.fail_json(msg=str(exc), actions=actions)
    module.exit_json(changed=bool(actions), actions=actions, diff={"before": before, "after": after})


if __name__ == "__main__":
    main()
