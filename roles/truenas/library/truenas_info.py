#!/usr/bin/python
# -*- coding: utf-8 -*-

DOCUMENTATION = r"""
---
module: truenas_info
short_description: Connect to TrueNAS and report its state, read-only
description:
  - Tries each address in O(api_hosts) in order and reports the first one that accepts the API key,
    so a play can reach a NAS that is either on its configured static address or still on the
    address it was installed with.
  - Makes no changes. C(pool.import_find) is a job, but it only scans disks.
  - Runs on the controller and needs the websocket-client Python package there.
options:
  api_hosts:
    description: Addresses to try, in order.
    type: list
    elements: str
    required: true
  api_port:
    description: HTTPS port of the TrueNAS web server.
    type: int
    default: 443
  api_cert_sha256:
    description: SHA-256 fingerprint of the NAS's certificate. When set, an address whose server presents
      another certificate is skipped before the API key is sent.
    type: str
    default: ""
  api_key:
    description: TrueNAS API key.
    type: str
  validate_certs:
    description: Verify the NAS's TLS certificate. The default install uses a self-signed one.
    type: bool
    default: false
  api_timeout:
    description: Seconds to wait for each API answer.
    type: int
    default: 120
  api_connect_wait:
    description: Seconds to keep retrying the list of addresses before giving up.
    type: int
    default: 60
  api_job_timeout:
    description: Seconds to wait for the pool scan job.
    type: int
    default: 1800
  api_login_wait:
    description: Seconds to wait out TrueNAS's login rate limit.
    type: int
    default: 75
  importable:
    description: Also scan for pools that can be imported. Takes a few seconds.
    type: bool
    default: true
"""

RETURN = r"""
api_host:
  description: The address that answered. Later tasks should use it.
  type: str
  returned: success
version:
  description: TrueNAS version without the product prefix, for example C(25.10.7).
  type: str
  returned: success
"""

import time

from ansible.module_utils.basic import AnsibleModule
from ansible.module_utils.truenas_api import (
    TrueNASError,
    api_argument_spec,
    open_client,
    pick,
)


def _address(alias):
    return f"{alias['address']}/{alias['netmask']}"


def gather(api, importable):
    network = api.call("network.configuration.config")
    disks = api.call("disk.details", {"type": "BOTH"})
    result = {
        "version": api.call("system.version").removeprefix("TrueNAS-"),
        "ready": api.call("system.ready"),
        "hostname": api.call("system.info").get("hostname"),
        "websocket_interface": (api.call("interface.websocket_interface") or {}).get("name"),
        "websocket_local_ip": api.call("interface.websocket_local_ip"),
        "interfaces": [
            {
                **pick(iface, ["name", "ipv4_dhcp", "ipv6_auto"]),
                "aliases": [_address(a) for a in iface["aliases"]],
                "addresses": [_address(a) for a in iface["state"]["aliases"]],
                "link_state": iface["state"].get("link_state"),
            }
            for iface in api.call("interface.query")
        ],
        "network": {
            **pick(network, ["hostname", "domain", "ipv4gateway", "nameserver1", "nameserver2", "nameserver3"]),
            "effective": network.get("state", {}),
        },
        "network_pending_changes": api.call("interface.has_pending_changes"),
        "pools": [
            {
                **pick(p, ["id", "name", "guid", "status", "healthy", "path"]),
                "autotrim": (p.get("autotrim") or {}).get("rawvalue"),
            }
            for p in api.call("pool.query")
        ],
        "disks": [
            pick(d, ["name", "serial", "model", "size", "imported_zpool", "exported_zpool"])
            for kind in ("used", "unused")
            for d in disks.get(kind, [])
        ],
        "services": {s["service"]: pick(s, ["enable", "state"]) for s in api.call("service.query")},
        "system_dataset": pick(api.call("systemdataset.config"), ["pool", "pool_set"]),
        "datasets": [d["id"] for d in api.call("pool.dataset.query", [], {"extra": {"flat": True}})],
        "nfs_shares": [
            pick(s, ["id", "path", "hosts", "networks", "enabled", "ro", "maproot_user", "maproot_group"])
            for s in api.call("sharing.nfs.query")
        ],
    }
    if importable:
        result["importable_pools"] = api.job("pool.import_find")
    return result


def main():
    spec = api_argument_spec()
    spec.pop("api_host")
    spec.update(
        api_hosts=dict(type="list", elements="str", required=True),
        importable=dict(type="bool", default=True),
    )
    module = AnsibleModule(argument_spec=spec, supports_check_mode=True)

    hosts = [h for h in dict.fromkeys(module.params["api_hosts"]) if h]
    if not hosts:
        module.fail_json(msg="api_hosts is empty: there is no address to reach the NAS on")

    # Each pass tries every address once with a short timeout, so a static address that is not
    # live yet costs a few seconds rather than the whole connect window.
    deadline = time.monotonic() + module.params["api_connect_wait"]
    errors = {}
    while True:
        for host in hosts:
            try:
                api = open_client(module, host=host, connect_wait=0, connect_timeout=5)
            except TrueNASError as exc:
                if "rejected the API key" in str(exc) or "refused the login" in str(exc):
                    module.fail_json(msg=str(exc))
                errors[host] = str(exc)
                continue
            with api:
                try:
                    result = gather(api, module.params["importable"])
                except TrueNASError as exc:
                    module.fail_json(msg=str(exc))
            module.exit_json(changed=False, api_host=host, **result)
        if time.monotonic() >= deadline:
            module.fail_json(msg="TrueNAS did not answer on any of the given addresses", errors=errors)
        time.sleep(2)


if __name__ == "__main__":
    main()
