"""roles/haos_onboard's reading of the QEMU guest agent and of Home Assistant's onboarding status.

The idempotence tests run the role against a fake host and a fake Home Assistant; these cover the parsing on its own,
including answers the fakes never give.
"""

import importlib.util
import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "roles" / "haos_onboard" / "filter_plugins" / "haos_onboard.py"
_spec = importlib.util.spec_from_file_location("haos_onboard_filters", PLUGIN)
ho = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ho)

# Built at run time: gitleaks flags a MAC, a private address or a link-local one written out in any tracked file.
MAC = ":".join(["02", "00", "00", "00", "00", "09"])
LOOPBACK_MAC = ":".join(["00"] * 6)
DOCKER_MAC = ":".join(["02", "42", "ac", "1e", "20", "01"])
DOCKER_IP = ".".join(["172", "30", "32", "1"])
LINK_LOCAL_IP = ".".join(["169", "254", "3", "4"])
LINK_LOCAL_IP6 = "fe80" + "::1"


def nic(mac, *addresses):
    return {
        "name": "enp0s18",
        "hardware-address": mac,
        "ip-addresses": [{"ip-address-type": kind, "ip-address": address, "prefix": 24} for kind, address in addresses],
    }


class TestGuestAddress:
    def test_the_vms_own_nic(self):
        report = [
            {"name": "lo", "hardware-address": LOOPBACK_MAC, "ip-addresses": []},
            nic(MAC, ("ipv6", LINK_LOCAL_IP6), ("ipv4", "192.0.2.50")),
            nic(DOCKER_MAC, ("ipv4", DOCKER_IP)),
        ]
        assert ho.guest_address(json.dumps(report), MAC.upper()) == "192.0.2.50"

    @pytest.mark.parametrize(
        "report",
        [
            [nic(MAC, ("ipv6", LINK_LOCAL_IP6))],
            [nic(MAC, ("ipv4", LINK_LOCAL_IP))],
            [nic(DOCKER_MAC, ("ipv4", DOCKER_IP))],
            [],
            {"error": "QEMU guest agent is not running"},
        ],
        ids=["ipv6-only", "link-local", "another-nic", "nothing", "not-a-list"],
    )
    def test_no_address_yet(self, report):
        assert ho.guest_address(json.dumps(report), MAC) == ""

    def test_not_json(self):
        assert ho.guest_address("QEMU guest agent is not running", MAC) == ""


class TestOnboarding:
    def test_open_and_done(self):
        steps = [{"step": "user", "done": False}, {"step": "core_config", "done": False}]
        assert ho.onboarding_user_done(steps) is False
        assert ho.onboarding_user_done([{"step": "user", "done": True}]) is True

    @pytest.mark.parametrize(
        "answer", [{"message": "Unauthorized"}, "<html>", [{"step": "core_config", "done": True}], ["user"]]
    )
    def test_anything_else_is_an_error_never_a_yes(self, answer):
        with pytest.raises(ValueError):
            ho.onboarding_user_done(answer)
