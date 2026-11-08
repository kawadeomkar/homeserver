"""Unit tests for roles/opnsense's client and its info module's parsing. No router is involved; the integration tests
(tests/integration/test_opnsense.py) run the role against fake_opnsense.py, the certificate pin and the 401 included.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

ROLE = Path(__file__).resolve().parents[2] / "roles" / "opnsense"
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))
import fake_opnsense  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The module imports its client as ansible.module_utils.opnsense_api, as Ansible ships it.
api = load("ansible.module_utils.opnsense_api", ROLE / "module_utils" / "opnsense_api.py")
info = load("opnsense_info", ROLE / "library" / "opnsense_info.py")


@pytest.mark.parametrize(
    ("version", "release"),
    [
        ("26.7.8_2", "26.7.8"),
        ("26.7.8", "26.7.8"),
        ("26.7", "26.7"),
        ("26.7_4", "26.7"),
        (" 26.1.2 ", "26.1.2"),
        ("26.7.a_17", ""),
        ("unknown", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_release_drops_the_hotfix_and_refuses_what_is_no_release(version, release):
    assert api.release_of(version) == release


def test_the_client_refuses_to_send_the_key_unproven():
    with pytest.raises(api.OPNsenseCertificateError, match="the API key was not sent"):
        api.OPNsenseClient("192.0.2.1", "key", "secret")


@pytest.mark.parametrize(("host", "key", "secret"), [("", "k", "s"), ("192.0.2.1", "", "s"), ("192.0.2.1", "k", "")])
def test_the_client_needs_an_address_and_both_halves_of_the_key(host, key, secret):
    with pytest.raises(api.OPNsenseError):
        api.OPNsenseClient(host, key, secret, cert_sha256="AA" * 32)


def test_the_pin_is_compared_without_colons_or_case():
    client = api.OPNsenseClient("192.0.2.1", "k", "s", cert_sha256=":".join(["Ab"] * 32))
    assert client.cert_sha256 == "ab" * 32


def test_info_reports_the_release_sets_and_installed_plugins_only():
    state = fake_opnsense.initial_state("26.7.8_2", plugins=("os-tailscale", "os-isc-dhcp"))
    got = info.gather(fake_opnsense.firmware_info(state))
    assert got == {
        "version": "26.7.8_2",
        "release": "26.7.8",
        "series": "26.7",
        "kernel": "26.7.8",
        "base": "26.7.8",
        "plugins": [{"name": "os-isc-dhcp", "version": "1.0"}, {"name": "os-tailscale", "version": "1.0"}],
    }


def test_info_never_returns_the_product_record():
    got = info.gather(fake_opnsense.firmware_info(fake_opnsense.initial_state()))
    assert "product" not in got and "pkg.opnsense.org" not in repr(got)


def test_info_reads_the_version_from_the_product_record_when_the_top_level_lacks_it():
    answer = fake_opnsense.firmware_info(fake_opnsense.initial_state("26.7.2"))
    del answer["product_version"]
    assert info.gather(answer)["release"] == "26.7.2"
