"""Unit tests for roles/opnsense's client, its info module's parsing, the backups' encrypted format and the web
pages' parsing. No router is involved; the integration tests (tests/integration/test_opnsense.py) run the role against
fake_opnsense.py, the certificate pin, the 401, the backups and the restores included.
"""

import base64
import importlib.util
import subprocess
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


# ------------------------------------------------------------------------------------------- the backup format ---
crypt = load("ansible.module_utils.opnsense_crypt", ROLE / "module_utils" / "opnsense_crypt.py")
CONFIG = b'<?xml version="1.0"?>\n<opnsense>\n  <system><hostname>router</hostname></system>\n</opnsense>\n'


def test_a_backup_reads_back_and_names_its_release():
    text = crypt.encrypt(CONFIG, "correct horse", version="OPNsense 26.7.8_2")
    assert text.startswith("---- BEGIN config.xml ----\nVersion: OPNsense 26.7.8_2\nCipher: AES-256-CBC\n")
    assert "PBKDF2: 100000\nHash: SHA512\n\n" in text and text.endswith("---- END config.xml ----\n")
    assert all(len(line) <= 76 for line in text.splitlines()[6:-1])
    assert crypt.decrypt(text, "correct horse") == CONFIG
    assert crypt.headers(text)["version"] == "OPNsense 26.7.8_2"


def test_openssl_opens_a_backup_as_the_router_would():
    """What fake_opnsense.py's restore does is what Base::decrypt() does on the router: openssl enc -d."""
    assert fake_opnsense.openssl_decrypt(crypt.encrypt(CONFIG, "correct horse"), "correct horse") == CONFIG.decode()


def test_a_backup_the_router_made_reads_here(tmp_path):
    """Base::encrypt()'s own command, run here: openssl enc -e with the router's defaults, wrapped as it wraps it."""
    plain, sealed, secret = tmp_path / "config.xml", tmp_path / "config.enc", tmp_path / "pass"
    plain.write_bytes(CONFIG)
    secret.write_text("correct horse")
    subprocess.run(
        [
            *("openssl", "enc", "-e", "-aes-256-cbc", "-md", "sha512", "-pbkdf2", "-iter", "100000"),
            *("-in", plain, "-out", sealed, "-pass", f"file:{secret}"),
        ],
        check=True,
    )
    body = base64.b64encode(sealed.read_bytes()).decode()
    lines = [body[i : i + 76] for i in range(0, len(body), 76)]
    text = "\n".join(
        [
            *("---- BEGIN config.xml ----", "Version: OPNsense 26.7.8", "Cipher: AES-256-CBC", "PBKDF2: 100000"),
            *("Hash: SHA512", "", *lines, "---- END config.xml ----", ""),
        ]
    )
    assert crypt.decrypt(text, "correct horse") == CONFIG


def test_a_wrong_password_is_said_so():
    with pytest.raises(crypt.CryptError, match="password is wrong"):
        crypt.decrypt(crypt.encrypt(CONFIG, "correct horse"), "battery staple")


def test_only_the_format_the_router_writes_is_read():
    with pytest.raises(crypt.CryptError, match="not an encrypted"):
        crypt.decrypt(CONFIG.decode(), "correct horse")
    old = crypt.encrypt(CONFIG, "correct horse").replace("PBKDF2: 100000\n", "")
    with pytest.raises(crypt.CryptError, match=r"before OPNsense 21\.7"):
        crypt.decrypt(old, "correct horse")


# --------------------------------------------------------------------------------------------- the web pages ---
LOGIN = (
    '<html><body><form method="post"><input type="hidden" name="k3y" value="t0k&amp;en" autocomplete="new-password" />'
    '<input name="usernamefld" /></form></body></html>'
)
RESTORED = (
    '<div class="col-xs-12">\n  <div class="alert alert-info" role="alert" style="min-height: 65px;">\n    \n'
    '    <div style="margin-top: 8px;">The configuration has been restored. The system is rebooting now.</div>\n'
    "  </div>\n</div>\n"
)
REFUSED = (
    '<div class="col-xs-12"><div class="alert alert-danger" role="alert">\n<button type="button" class="close" '
    'data-dismiss="alert"><span aria-hidden="true">&times;</span><span class="sr-only">Close</span></button>\n'
    "<p>The following input errors were detected:</p>\n<ul><li>The uploaded file could not be decrypted.</li>"
    "<li>Another &amp; more.</li></ul></div></div>"
)


def test_the_csrf_token_is_read_from_the_form():
    assert api.csrf_token(LOGIN) == ("k3y", "t0k&en")
    assert api.csrf_token("<html></html>") is None


def test_the_pages_messages_are_read_without_their_buttons():
    assert api.alerts(RESTORED, "info") == ["The configuration has been restored. The system is rebooting now."]
    assert api.alerts(REFUSED, "danger") == ["The uploaded file could not be decrypted.", "Another & more."]
    assert api.alerts(RESTORED, "danger") == [] and api.alerts(REFUSED, "info") == []
