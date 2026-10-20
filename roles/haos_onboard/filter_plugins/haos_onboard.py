"""What roles/haos_onboard reads from the QEMU guest agent and from Home Assistant's onboarding API.

Pure: it runs on the controller, never connects anywhere, and is unit-tested in tests/unit/test_haos_onboard.py.
"""

import ipaddress
import json


def guest_address(report, mac):
    """The VM's IPv4 address from `qm guest cmd <vmid> network-get-interfaces`, on the interface with this MAC; empty
    when the agent reports none yet. Home Assistant OS also has Docker's bridges, which never carry the VM's MAC."""
    try:
        interfaces = json.loads(report) if isinstance(report, str) else report
    except ValueError:
        return ""
    for interface in interfaces if isinstance(interfaces, list) else []:
        if str(interface.get("hardware-address", "")).lower() != str(mac).lower():
            continue
        for address in interface.get("ip-addresses") or []:
            if address.get("ip-address-type") != "ipv4":
                continue
            found = ipaddress.ip_address(address.get("ip-address", "0.0.0.0"))
            if not (found.is_link_local or found.is_unspecified):
                return str(found)
    return ""


def onboarding_user_done(steps):
    """Whether Home Assistant's onboarding status (GET /api/onboarding) says the owner account exists. Anything but
    that list, such as the landing page's refusal, is an error, never a yes."""
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        raise ValueError(f"not Home Assistant's onboarding status: {steps!r}")
    user = [step for step in steps if step.get("step") == "user"]
    if not user:
        raise ValueError(f"Home Assistant's onboarding status has no user step: {steps!r}")
    return bool(user[0].get("done"))


class FilterModule:
    def filters(self):
        return {"haos_guest_address": guest_address, "haos_onboarding_user_done": onboarding_user_done}
