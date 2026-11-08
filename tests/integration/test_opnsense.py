"""Runs router.yml's play against fake_opnsense.py and checks that it only reads.

A dry run and a real run each read the router's release and plugins once, change nothing on it, and report them.
The key and secret go only to a server presenting the pinned certificate: another certificate is refused before any
request is sent. A wrong key pair is refused with the router's 401, a release older than the role's floor (or a
development one) before anything else, in a dry run too, and without opnsense_address the play ends before the role
runs. The play runs with this repo's own group_vars for the router and addresses on loopback.

Run: just test idempotence
"""

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fake_opnsense  # noqa: E402
from test_idempotence import expect, free_port, read, run, write  # noqa: E402

PLAYBOOK = "router.yml"
ROUTER = "127.0.0.1"
INFO = {"method": "GET", "path": "/api/core/firmware/info", "credentials": True}


def certificate(work, name):
    """A self-signed certificate and key in `work`, and the certificate's SHA-256 fingerprint as openssl prints it."""
    cert, key = work / f"{name}.pem", work / f"{name}.key"
    subprocess.run(
        [
            *("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert, "-days", "1"),
            *("-subj", "/CN=localhost"),
        ],
        check=True,
        capture_output=True,
    )
    out = subprocess.run(
        ["openssl", "x509", "-in", cert, "-noout", "-fingerprint", "-sha256"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return cert, key, out.strip().split("=", 1)[1]


@pytest.fixture
def router(tmp_path):
    cert, key, fingerprint = certificate(tmp_path, "router")
    _, _, other = certificate(tmp_path, "other")
    state, calls = tmp_path / "state.json", tmp_path / "calls.jsonl"
    write(state, fake_opnsense.initial_state())
    calls.touch()
    port = free_port()
    log = (tmp_path / "fake.log").open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            HERE / "fake_opnsense.py",
            "--port",
            str(port),
            "--cert",
            cert,
            "--key",
            key,
            "--state",
            state,
            "--call-log",
            calls,
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("fake OPNsense did not start")
    calls.write_text("")
    yield {"port": port, "state": state, "calls": calls, "fingerprint": fingerprint, "other": other}
    proc.terminate()
    proc.wait(timeout=10)
    log.close()


def router_vars(router, **more):
    return {
        "opnsense_address": ROUTER,
        "opnsense_api_port": router["port"],
        "opnsense_api_cert_sha256": router["fingerprint"],
        "vault_opnsense_api_key": fake_opnsense.API_KEY,
        "vault_opnsense_api_secret": fake_opnsense.API_SECRET,
        **more,
    }


def calls_of(router):
    return [json.loads(line) for line in router["calls"].read_text().splitlines()]


def test_router_play_reads_and_changes_nothing(router):
    before = read(router["state"])
    for check in (True, False):
        result = run(PLAYBOOK, router_vars(router), check=check)
        expect(result, "opnsense", changed=0, note=f"{'dry ' if check else ''}run")
        out = result[2]
        assert f"OPNsense {fake_opnsense.RELEASE} at {ROUTER}" in out, out[-3000:]
        assert "plugins: os-isc-dhcp" in out and "os-tailscale" not in out, out[-3000:]
        assert fake_opnsense.API_SECRET not in out, "the API secret reached Ansible output"
        assert "product_mirror" not in out and "pkg.opnsense.org" not in out, out[-3000:]
    assert calls_of(router) == [INFO, INFO], "each run reads the firmware information once, and nothing else"
    assert read(router["state"]) == before


def test_router_play_sends_nothing_to_another_certificate(router):
    rc, _, out = run(PLAYBOOK, router_vars(router, opnsense_api_cert_sha256=router["other"]), check=True)
    assert rc != 0, out[-3000:]
    assert "not the pinned one; the API key was not sent" in out, out[-3000:]
    assert calls_of(router) == [], "a request reached a server whose certificate is not the pinned one"


def test_router_play_refuses_an_unpinned_certificate(router):
    rc, _, out = run(PLAYBOOK, router_vars(router, opnsense_api_cert_sha256=""), check=True)
    assert rc != 0, out[-3000:]
    assert "nothing would prove that what answers is the router" in out, out[-3000:]
    assert calls_of(router) == []


def test_router_play_reports_a_refused_key(router):
    rc, _, out = run(PLAYBOOK, router_vars(router, vault_opnsense_api_secret="not-the-secret"), check=True)
    assert rc != 0, out[-3000:]
    assert "refused the API key and secret" in out and "scripts/runbooks/opnsense.md" in out, out[-3000:]
    assert "not-the-secret" not in out


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("26.1.2", False),
        ("26.1.11_10", False),
        ("26.7", False),
        ("26.7.1_3", False),
        ("26.7.a_17", False),
        ("26.7.2", True),
        ("27.1", True),
    ],
)
def test_router_play_needs_a_supported_release(router, version, supported):
    write(router["state"], fake_opnsense.initial_state(version))
    result = run(PLAYBOOK, router_vars(router), check=True)
    if supported:
        expect(result, "opnsense", changed=0, note=version)
    else:
        rc, _, out = result
        assert rc != 0, out[-3000:]
        assert f"The router runs OPNsense {version}; this role needs 26.7.2 or later" in out, out[-3000:]


def test_router_play_ends_without_an_address(router):
    given = {k: v for k, v in router_vars(router).items() if k not in ("opnsense_address", "opnsense_api_cert_sha256")}
    result = run(PLAYBOOK, given, check=True)
    expect(result, "opnsense", changed=0, note="no address")
    assert "so this setup leaves the router alone" in result[2], result[2][-3000:]
    assert calls_of(router) == []


def test_router_play_refuses_a_pin_without_an_address(router):
    """A pin left without its address is what a misspelt opnsense_address looks like."""
    rc, _, out = run(PLAYBOOK, {k: v for k, v in router_vars(router).items() if k != "opnsense_address"}, check=True)
    assert rc != 0, out[-3000:]
    assert "sets opnsense_api_cert_sha256 but no opnsense_address" in out, out[-3000:]
    assert calls_of(router) == []
