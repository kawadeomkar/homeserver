#!/usr/bin/python

DOCUMENTATION = r"""
---
module: truenas_interface
short_description: Give a TrueNAS interface a static IPv4 address, with automatic rollback
description:
  - Turns DHCP off on the interface, sets one static IPv4 address, and sets the default gateway and
    name servers, which are no longer learned once DHCP is off.
  - Uses TrueNAS's own safety net. The change is committed with a check-in timeout; the module then
    reconnects on the new address and checks in. If it cannot, TrueNAS rolls the interface back by
    itself when the timeout expires, so a wrong value does not lock anyone out.
  - Without O(interface), the interface is the one carrying the connection the module came in on.
  - Static IPv6 aliases already configured on the interface are kept.
options:
  interface:
    description: Interface name, for example C(enp7s0).
    type: str
  address:
    description: The static address in CIDR form, for example C(192.0.2.10/24).
    type: str
    required: true
  gateway:
    description: IPv4 default gateway.
    type: str
    required: true
  nameservers:
    description: Up to three name servers.
    type: list
    elements: str
    default: []
  ipv6_auto:
    description: IPv6 autoconfiguration on the interface. Left as it is when not given.
    type: bool
  checkin_timeout:
    description: Seconds TrueNAS waits for the check-in before rolling back.
    type: int
    default: 90
  conflict_check:
    description:
      - Before moving the NAS to O(address), check from the controller that no other device already
        answers there (a TCP connection to O(api_port), ping, then the ARP or neighbour table), and fail
        if one does. Runs in check mode too. Best effort; a silent device can still be missed.
    type: bool
    default: true
notes:
  - Connection and timing options (api_host, api_port, api_key, validate_certs, api_cert_sha256,
    api_timeout, api_connect_wait, api_job_timeout, api_login_wait) are shared by every module in this role; see
    module_utils/truenas_api.py.
"""

RETURN = r"""
api_host:
  description: The address to use for later calls. The static address once it is applied.
  type: str
  returned: success
interface:
  description: The interface that was checked or changed.
  type: str
  returned: success
"""

import ipaddress
import re
import shutil
import socket
import subprocess
import sys
import time

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.truenas_api import (
    TrueNASCallError,
    TrueNASConnectionError,
    TrueNASError,
    api_argument_spec,
    connect,
    is_rate_limited,
    open_client,
)


def describe(iface, network):
    return {
        "interface": iface["name"],
        "ipv4_dhcp": iface["ipv4_dhcp"],
        "ipv6_auto": iface["ipv6_auto"],
        "aliases": [f"{a['address']}/{a['netmask']}" for a in iface["aliases"]],
        "ipv4gateway": network.get("ipv4gateway"),
        "nameservers": [network.get(f"nameserver{i}") for i in (1, 2, 3) if network.get(f"nameserver{i}")],
    }


MAC = re.compile(r"(?:[0-9a-f]{1,2}:){5}[0-9a-f]{1,2}", re.IGNORECASE)


def neighbour_mac(address):
    """The controller's ARP / neighbour-table entry for address, if it has a resolved one."""
    if sys.platform == "darwin" and shutil.which("arp"):
        cmd = ["arp", "-n", address]
    elif shutil.which("ip"):
        cmd = ["ip", "neigh", "show", address]
    else:
        return None
    out = subprocess.run(cmd, capture_output=True, text=True, check=False).stdout
    if any(word in out.upper() for word in ("INCOMPLETE", "FAILED", "NO ENTRY")):
        return None
    found = MAC.search(out)
    return found.group(0) if found else None


def address_in_use(address, port, timeout=2):
    """Evidence, seen from the controller, that some device already answers on address; None if none.

    A refused TCP connection is evidence too: only a live host sends the refusal.
    """
    try:
        with socket.create_connection((address, port), timeout=timeout):
            return f"something accepts connections on port {port}"
    except ConnectionRefusedError:
        return f"a device refused a connection on port {port}"
    except OSError:
        pass
    ping = shutil.which("ping")
    if ping:
        wait = ["-t", str(timeout)] if sys.platform == "darwin" else ["-W", str(timeout)]
        if subprocess.run([ping, "-c", "1", *wait, address], capture_output=True, check=False).returncode == 0:
            return "it answers ping"
    mac = neighbour_mac(address)
    if mac:
        return f"it answers ARP as {mac}"
    return None


def check_in(module, address, deadline):
    """Reconnect on the new address and confirm the change before TrueNAS rolls it back."""
    last_error = None
    while time.monotonic() < deadline:
        try:
            with open_client(module, host=address, connect_wait=0, connect_timeout=5, login_wait=0) as api:
                if api.call("interface.checkin_waiting") is not None:
                    api.call("interface.checkin")
                if not api.call("interface.has_pending_changes"):
                    return api.call("interface.query")
                # Pending changes but no rollback timer yet: the commit is still applying them.
                last_error = "the commit had not finished"
        except TrueNASConnectionError as exc:
            # The NAS may still be bringing the address up.
            last_error = exc
        except TrueNASError as exc:
            # TrueNAS's login rate limit resets within a minute; keep trying until the deadline.
            if not is_rate_limited(exc):
                raise
            last_error = exc
        time.sleep(2)
    raise TrueNASError(
        f"could not check in on {address} before the deadline ({last_error}). "
        "TrueNAS rolls the interface back by itself when the timeout expires; it should be back on "
        "its previous address shortly."
    )


def main():
    spec = api_argument_spec()
    spec.update(
        interface=dict(type="str"),
        address=dict(type="str", required=True),
        gateway=dict(type="str", required=True),
        nameservers=dict(type="list", elements="str", default=[]),
        ipv6_auto=dict(type="bool"),
        checkin_timeout=dict(type="int", default=90),
        conflict_check=dict(type="bool", default=True),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)
    p = module.params

    try:
        wanted = ipaddress.ip_interface(p["address"])
        ipaddress.ip_address(p["gateway"])
    except ValueError as exc:
        module.fail_json(msg=f"invalid address: {exc}")
    if wanted.version != 4 or "/" not in p["address"]:
        module.fail_json(msg=f"address must be IPv4 in CIDR form, got {p['address']!r}")
    if wanted.ip in (wanted.network.network_address, wanted.network.broadcast_address):
        module.fail_json(msg=f"{p['address']} is a network or broadcast address")
    if ipaddress.ip_address(p["gateway"]) not in wanted.network:
        module.fail_json(msg=f"gateway {p['gateway']} is not inside {wanted.network}")
    if len(p["nameservers"]) > 3:
        module.fail_json(msg="TrueNAS takes at most three name servers")
    if p["checkin_timeout"] < 45:
        module.fail_json(msg="checkin_timeout below 45 seconds leaves too little time to reconnect")

    address = str(wanted.ip)
    alias = {"type": "INET", "address": address, "netmask": wanted.network.prefixlen}
    servers = (p["nameservers"] + ["", "", ""])[:3]
    want_network = {"ipv4gateway": p["gateway"], **{f"nameserver{i + 1}": servers[i] for i in range(3)}}

    api = connect(module)
    try:
        name = p["interface"] or (api.call("interface.websocket_interface") or {}).get("name")
        if not name:
            module.fail_json(msg="cannot tell which interface the connection uses; set interface")
        found = api.call("interface.query", [["name", "=", name]])
        if not found:
            module.fail_json(msg=f"TrueNAS has no interface {name}")
        iface = found[0]
        network = api.call("network.configuration.config")

        # interface.query reports the saved configuration, which an interrupted run can leave ahead
        # of what is live. Never build on, or report "ok" over, changes nobody committed.
        if api.call("interface.has_pending_changes") or api.call("interface.checkin_waiting") is not None:
            module.fail_json(
                msg="TrueNAS has uncommitted or unconfirmed interface changes. Commit or roll them back "
                "in the UI (Network page) first."
            )

        current_v4 = [(a["address"], a["netmask"]) for a in iface["aliases"] if a.get("type", "INET") == "INET"]
        kept_v6 = [a for a in iface["aliases"] if a.get("type") == "INET6"]
        iface_change = {}
        if iface["ipv4_dhcp"]:
            iface_change["ipv4_dhcp"] = False
        if current_v4 != [(alias["address"], alias["netmask"])]:
            iface_change["aliases"] = [alias, *kept_v6]
        if p["ipv6_auto"] is not None and iface["ipv6_auto"] != p["ipv6_auto"]:
            iface_change["ipv6_auto"] = p["ipv6_auto"]
        if iface_change:
            # interface.update keeps fields it is not given; send the full alias list every time.
            iface_change.setdefault("aliases", [alias, *kept_v6])
        # TrueNAS wants the name servers it is given to be consecutive, so send all three or none.
        network_change = {k: v for k, v in want_network.items() if network.get(k) != v}
        if any(k.startswith("nameserver") for k in network_change):
            network_change.update({k: v for k, v in want_network.items() if k.startswith("nameserver")})
        live = [a["address"] for a in iface["state"]["aliases"] if a.get("type", "INET") == "INET"]
        # Moving to an address another device already holds makes the check-in talk to that device,
        # and the change rolls back after the full timeout. Refuse up front, in check mode too.
        if iface_change and address not in live and p["conflict_check"]:
            evidence = address_in_use(address, p["api_port"])
            if evidence:
                module.fail_json(
                    msg=f"{address} is already in use by another device on the network ({evidence}). Choose a "
                    "free address outside the router's DHCP pool, or reserve it on the router for the NAS and "
                    "free it, then re-run. Nothing was changed."
                )
        if not iface_change and address not in live:
            module.fail_json(
                msg=f"{name} is configured with {address} but does not have it; the saved configuration "
                "was never applied. Apply or discard it in the UI (Network page), then re-run."
            )

        before = describe(iface, network)
        after = {
            **before,
            "ipv4_dhcp": False,
            "aliases": [f"{address}/{alias['netmask']}"] + [f"{a['address']}/{a['netmask']}" for a in kept_v6],
            "ipv4gateway": p["gateway"],
            "nameservers": [s for s in servers if s],
        }
        if p["ipv6_auto"] is not None:
            after["ipv6_auto"] = p["ipv6_auto"]
        result = {
            "changed": bool(iface_change or network_change),
            "interface": name,
            "api_host": p["api_host"],
            "diff": {"before": before, "after": after},
        }
        if not result["changed"] or module.check_mode:
            api.close()
            module.exit_json(**result)

        if network_change:
            # First, and applied at once: nothing about the interface has changed yet, so a refusal
            # here leaves nothing to undo. The gateway is the one this subnet already routes through,
            # valid with DHCP and with the static address alike.
            api.call("network.configuration.update", network_change)
        if iface_change:
            api.call("interface.update", name, iface_change)

        if not iface_change:
            api.close()
            module.exit_json(**result)

        started = time.monotonic()
        try:
            # The address the connection uses goes away during the commit, so the answer may
            # never arrive. A transport error here is expected, an error answer is not.
            api.call(
                "interface.commit",
                {"rollback": True, "checkin_timeout": p["checkin_timeout"]},
                timeout=15,
            )
        except TrueNASCallError as exc:
            # Refused before anything was applied; drop the saved change. Report the refusal even if
            # the clean-up fails.
            try:
                if api.call("interface.has_pending_changes"):
                    api.call("interface.rollback", timeout=30)
            except TrueNASError as cleanup:
                raise TrueNASError(f"{exc}; discarding the saved change also failed: {cleanup}") from exc
            raise
        except TrueNASConnectionError:
            pass
        finally:
            api.close()

        interfaces = check_in(module, address, started + p["checkin_timeout"] - 5)
        final = next((i for i in interfaces if i["name"] == name), None)
        if final is None or final["ipv4_dhcp"] or alias["address"] not in [a["address"] for a in final["aliases"]]:
            module.fail_json(msg=f"{name} does not show the static address after check-in", **result)
        result["api_host"] = address
    except TrueNASError as exc:
        api.close()
        module.fail_json(msg=str(exc))
    module.exit_json(**result)


if __name__ == "__main__":
    main()
