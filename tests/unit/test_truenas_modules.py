"""Unit tests for the comparison logic in roles/truenas. No NAS is involved."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROLE = Path(__file__).resolve().parents[2] / "roles" / "truenas"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The modules import their client as ansible.module_utils.truenas_api, as Ansible ships it.
api = load("ansible.module_utils.truenas_api", ROLE / "module_utils" / "truenas_api.py")
dataset = load("truenas_dataset", ROLE / "library" / "truenas_dataset.py")


def prop(rawvalue, source="LOCAL"):
    return {"rawvalue": rawvalue, "value": str(rawvalue).upper(), "source": source}


class TestDifferences:
    def test_equal_values_give_nothing(self):
        assert api.differences({"a": 1, "b": "x", "extra": 2}, {"a": 1, "b": "x"}) == {}

    def test_changed_scalar_is_returned(self):
        assert api.differences({"a": 1}, {"a": 2}) == {"a": 2}

    def test_missing_key_is_a_difference(self):
        assert api.differences({}, {"a": None}) == {} and api.differences({}, {"a": 1}) == {"a": 1}

    def test_scalar_lists_ignore_order(self):
        assert api.differences({"hosts": ["b", "a"]}, {"hosts": ["a", "b"]}) == {}
        assert api.differences({"hosts": ["a"]}, {"hosts": ["a", "b"]}) == {"hosts": ["a", "b"]}

    def test_nested_dict_compares_only_given_keys_and_returns_merged(self):
        current = {"schedule": {"minute": "00", "hour": "00", "dow": "7"}}
        assert api.differences(current, {"schedule": {"dow": "7"}}) == {}
        assert api.differences(current, {"schedule": {"hour": "03"}}) == {
            "schedule": {"minute": "00", "hour": "03", "dow": "7"}
        }


def test_pick_never_returns_unrequested_keys():
    config = {"ui_httpsredirect": False, "ui_certificate": {"privatekey": "secret"}}
    assert api.pick(config, ["ui_httpsredirect", "missing"]) == {"ui_httpsredirect": False}


def test_validation_errors_are_formatted():
    error = {"message": "Validation error", "data": {"reason": "[EINVAL] bad", "extra": [["x.path", "nope", 22]]}}
    assert api._format_error("m.create", error) == "m.create: [EINVAL] bad (x.path: nope)"


class TestSizes:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            (0, 0),
            (None, 0),
            (65536, 65536),
            ("64K", 65536),
            ("1T", 1099511627776),
            ("1TiB", 1099511627776),
            ("512", 512),
        ],
    )
    def test_to_bytes(self, given, expected):
        assert dataset.to_bytes(given) == expected

    def test_rejects_garbage(self):
        with pytest.raises(ValueError):
            dataset.to_bytes("lots")

    def test_quota_goes_to_the_api_as_an_integer(self):
        assert dataset.for_api("quota", "1T") == 1099511627776
        assert dataset.for_api("quota", 0) is None
        assert dataset.for_api("recordsize", "64K") == "64K"


class TestMatches:
    def test_recordsize_compares_bytes(self):
        assert dataset.matches("recordsize", prop("65536"), "64K")
        assert not dataset.matches("recordsize", prop("131072"), "64K")

    def test_quota(self):
        assert dataset.matches("quota", prop("0"), 0)
        assert dataset.matches("quota", prop("0"), None)
        assert dataset.matches("quota", prop("1099511627776"), 1099511627776)
        assert not dataset.matches("quota", prop("0"), "1T")

    def test_enums_ignore_case(self):
        assert dataset.matches("sync", prop("disabled"), "DISABLED")
        assert dataset.matches("compression", prop("lz4"), "LZ4")
        assert not dataset.matches("sync", prop("standard"), "DISABLED")

    def test_old_spellings(self):
        assert dataset.matches("acltype", prop("posixacl"), "POSIX")

    def test_inherit_means_not_set_locally(self):
        assert dataset.matches("sync", prop("standard", "INHERITED"), "INHERIT")
        assert not dataset.matches("sync", prop("standard", "LOCAL"), "INHERIT")

    def test_missing_property_never_matches(self):
        assert not dataset.matches("sync", None, "STANDARD")


interface = load("truenas_interface", ROLE / "library" / "truenas_interface.py")


def test_a_refused_connection_counts_as_an_address_in_use():
    import socket

    with socket.socket() as s:  # find a loopback port nothing listens on
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert "refused" in interface.address_in_use("127.0.0.1", port)


def test_a_listening_port_counts_as_an_address_in_use():
    import socket

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        assert "accepts connections" in interface.address_in_use("127.0.0.1", server.getsockname()[1])
