"""Runs router.yml's play against fake_opnsense.py: the read, the backups and the restores.

The read: each run reads the router's release and plugins and refuses one below the role's floor (or a development
one), in a dry run too. The key pair goes only to a server presenting the pinned certificate: another certificate is
refused before any request is sent; a wrong pair is refused with the router's 401; without opnsense_address the play
ends before the role runs.

The backups: the first run backs the router's configuration up, encrypted so that openssl itself opens it with the
password, mode 0600 in a 0700 directory; a run with nothing changed changes nothing; a revision made on the router is
backed up by the next real run and announced by a dry run, which writes nothing; a router whose history lacks the
newest backup (restored or reinstalled by hand) is refused, unless told to accept it. Nothing ever writes to the
router.

The restores, only when asked: onto the router as it is known, its configuration backed up first and nothing done
when it already runs the backup; onto a fresh install at another address, whose certificate is only read and printed
until it is pinned, then logged in to as root, restored, waited for at the router's own address and backed up. A dry
run proves the login and the password and restores nothing; a wrong root password and an interface mismatch stop the
run. No password, key or secret the configuration holds reaches Ansible's output.

The play runs with this repo's own group_vars for the router and addresses on loopback. Run: just test idempotence
"""

import json
import socket
import stat
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
BACKUP_PASSWORD = "backup-password-sentinel-5d0e"
SECRETS = (fake_opnsense.API_SECRET, fake_opnsense.ROOT_PASSWORD, fake_opnsense.CONFIG_SECRET, BACKUP_PASSWORD)


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


def wait_for(port):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False


@pytest.fixture
def router(tmp_path):
    cert, key, fingerprint = certificate(tmp_path, "router")
    fresh_cert, fresh_key, fresh = certificate(tmp_path, "fresh")
    _, _, other = certificate(tmp_path, "other")
    state, calls = tmp_path / "state.json", tmp_path / "calls.jsonl"
    write(state, fake_opnsense.initial_state())
    calls.touch()
    port, bootstrap = free_port(), free_port()
    log = (tmp_path / "fake.log").open("w")
    proc = subprocess.Popen(
        [
            *(sys.executable, HERE / "fake_opnsense.py", "--port", str(port), "--cert", cert, "--key", key),
            *("--state", state, "--call-log", calls),
            *("--bootstrap-port", str(bootstrap), "--bootstrap-cert", fresh_cert, "--bootstrap-key", fresh_key),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    if not (wait_for(port) and wait_for(bootstrap)):
        proc.kill()
        pytest.fail("fake OPNsense did not start")
    calls.write_text("")
    yield {
        "port": port,
        "bootstrap": bootstrap,
        "state": state,
        "calls": calls,
        "fingerprint": fingerprint,
        "fresh": fresh,
        "other": other,
        "backups": tmp_path / "backups",
    }
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
        "vault_opnsense_backup_password": BACKUP_PASSWORD,
        "vault_opnsense_root_password": fake_opnsense.ROOT_PASSWORD,
        "router_backup_dir": str(router["backups"]),
        "opnsense_bootstrap_port": router["bootstrap"],
        "opnsense_restore_poll": 1,
        "opnsense_restore_wait": 30,
        **more,
    }


def calls_of(router):
    return [json.loads(line) for line in router["calls"].read_text().splitlines()]


def requests(router):
    """(address, method, path) of each request that reached a fake, dropped ones aside."""
    return [(c["address"], c["method"], c["path"]) for c in calls_of(router) if not c.get("dropped")]


def stored(router):
    return sorted(p.name for p in router["backups"].glob("config-*.xml"))


def opened(path):
    """A backup's configuration, decrypted by openssl as the router decrypts it."""
    return fake_opnsense.openssl_decrypt(path.read_text(), BACKUP_PASSWORD)


def no_secret(out):
    for secret in SECRETS:
        assert secret not in out, "a secret reached Ansible output"


# --------------------------------------------------------------------------------------------------- the read ---
def test_router_play_reads_and_backs_up_what_changed(router):
    before = read(router["state"])["history"]
    dry = run(PLAYBOOK, router_vars(router), check=True)
    expect(dry, "opnsense", changed=1, note="first dry run")
    assert f"OPNsense {fake_opnsense.RELEASE} at {ROUTER}" in dry[2], dry[2][-3000:]
    assert "plugins: os-isc-dhcp" in dry[2] and "os-tailscale" not in dry[2], dry[2][-3000:]
    assert "Would back up config-" in dry[2] and not router["backups"].exists(), dry[2][-3000:]

    first = run(PLAYBOOK, router_vars(router))
    expect(first, "opnsense", changed=1, note="first run")
    newest = fake_opnsense.running(read(router["state"]))
    assert stored(router) == [newest["id"]]
    backup = router["backups"] / newest["id"]
    assert stat.S_IMODE(router["backups"].stat().st_mode) == 0o700 and stat.S_IMODE(backup.stat().st_mode) == 0o600
    text = backup.read_text()
    assert text.startswith("---- BEGIN config.xml ----\n") and fake_opnsense.CONFIG_SECRET not in text
    assert f"Version: OPNsense {fake_opnsense.RELEASE}" in text
    assert opened(backup) == newest["content"], "openssl does not open the backup to the router's configuration"

    for check in (False, True):
        again = run(PLAYBOOK, router_vars(router), check=check)
        expect(again, "opnsense", changed=0, note=f"{'dry ' if check else ''}run with nothing changed")
        assert "The newest backup is the revision the router runs" in again[2], again[2][-3000:]

    write(router["state"], fake_opnsense.save_revision(read(router["state"]), "/firewall_rules.php made changes"))
    dry = run(PLAYBOOK, router_vars(router), check=True)
    expect(dry, "opnsense", changed=1, note="dry run after a change on the router")
    assert len(stored(router)) == 1 and "/firewall_rules.php made changes" in dry[2], dry[2][-3000:]
    changed = run(PLAYBOOK, router_vars(router))
    expect(changed, "opnsense", changed=1, note="run after a change on the router")
    assert len(stored(router)) == 2
    assert opened(router["backups"] / stored(router)[-1]) == fake_opnsense.running(read(router["state"]))["content"]
    expect(run(PLAYBOOK, router_vars(router)), "opnsense", changed=0, note="run after the change was backed up")

    for result in (dry, first, again, changed):
        no_secret(result[2])
    assert {method for _, method, _ in requests(router)} == {"GET"}, "a run wrote to the router"
    assert read(router["state"])["history"][: len(before)] == before


def test_router_play_refuses_a_router_that_does_not_descend_from_the_newest_backup(router):
    expect(run(PLAYBOOK, router_vars(router)), "opnsense", changed=1, note="first run")
    state = read(router["state"])
    state["history"] = [fake_opnsense.revision("router", time.time(), "System restore flushed local history")]
    write(router["state"], state)
    rc, _, out = run(PLAYBOOK, router_vars(router))
    assert rc != 0 and "whose history lacks the newest backup" in out, out[-3000:]
    assert "-e opnsense_restore=latest" in out and "-e opnsense_backup_accept=true" in out, out[-3000:]
    assert len(stored(router)) == 1
    accepted = run(PLAYBOOK, router_vars(router, opnsense_backup_accept=True))
    expect(accepted, "opnsense", changed=1, note="accepted")
    assert "as asked, though it does not descend from" in accepted[2] and len(stored(router)) == 2, accepted[2][-3000:]


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
        expect(result, "opnsense", changed=1, note=version)
    else:
        rc, _, out = result
        assert rc != 0, out[-3000:]
        assert f"The router runs OPNsense {version}; this role needs 26.7.2 or later" in out, out[-3000:]


def test_router_play_ends_without_an_address(router):
    given = {k: v for k, v in router_vars(router).items() if k not in ("opnsense_address", "opnsense_api_cert_sha256")}
    given.pop("router_backup_dir")
    result = run(PLAYBOOK, given, check=True)
    expect(result, "opnsense", changed=0, note="no address")
    assert "so this setup leaves the router alone" in result[2], result[2][-3000:]
    assert calls_of(router) == []


def test_router_play_refuses_a_pin_without_an_address(router):
    """A pin left without its address is what a misspelt opnsense_address looks like."""
    given = {k: v for k, v in router_vars(router).items() if k not in ("opnsense_address", "router_backup_dir")}
    rc, _, out = run(PLAYBOOK, given, check=True)
    assert rc != 0, out[-3000:]
    assert "sets opnsense_api_cert_sha256 but no opnsense_address" in out, out[-3000:]
    assert calls_of(router) == []


@pytest.mark.parametrize(
    ("more", "message"),
    [
        pytest.param({"vault_opnsense_backup_password": ""}, "put a long random one in the vault", id="no-password"),
        pytest.param(
            {"router_backup_dir": str(HERE / ".router-backups")},
            "opnsense_backup_dir must be outside the repository",
            id="inside-the-repository",
        ),
        pytest.param({"opnsense_restore": "yesterday"}, "opnsense_restore takes latest", id="restore-what"),
        pytest.param(
            {"opnsense_restore": "latest", "vault_opnsense_root_password": ""},
            "opnsense_restore needs opnsense_gui_password",
            id="restore-without-root-password",
        ),
    ],
)
def test_router_play_refuses_backup_settings_that_cannot_work(router, more, message):
    rc, _, out = run(PLAYBOOK, router_vars(router, **more), check=True)
    assert rc != 0 and message in out, out[-3000:]
    assert calls_of(router) == []


# ----------------------------------------------------------------------------------------------- the restores ---
def test_restore_onto_the_router_as_it_is_known(router):
    expect(run(PLAYBOOK, router_vars(router)), "opnsense", changed=1, note="first backup")
    (backup,) = stored(router)
    kept = opened(router["backups"] / backup)
    write(router["state"], fake_opnsense.save_revision(read(router["state"]), "a change to roll back", "unwanted"))

    dry = run(PLAYBOOK, router_vars(router, opnsense_restore="latest"), check=True)
    expect(dry, "opnsense", note="dry run of a restore")
    assert f"Would restore {backup}" in dry[2] and "nothing was uploaded" in dry[2], dry[2][-3000:]
    assert read(router["state"])["restores"] == [] and stored(router) == [backup]

    restored = run(PLAYBOOK, router_vars(router, opnsense_restore="latest"))
    expect(restored, "opnsense", changed=True, note="restore")
    (made,) = read(router["state"])["restores"]
    assert made["address"] == "router" and made["content"] == kept
    assert made["reboot"] and made["flush_history"] and made["keepconsole"]
    state = read(router["state"])
    assert fake_opnsense.running(state)["description"] == "System restore flushed local history"
    # The change it rolled back was backed up first, and what the restore left is the newest backup now.
    assert len(stored(router)) == 3
    assert "unwanted" in opened(router["backups"] / stored(router)[1])
    assert stored(router)[-1] == fake_opnsense.running(state)["id"]
    expect(run(PLAYBOOK, router_vars(router)), "opnsense", changed=0, note="run after the restore")
    for result in (dry, restored):
        no_secret(result[2])


def test_restore_does_nothing_when_the_router_runs_the_backup(router):
    expect(run(PLAYBOOK, router_vars(router)), "opnsense", changed=1, note="first backup")
    result = run(PLAYBOOK, router_vars(router, opnsense_restore="latest"))
    expect(result, "opnsense", changed=0, note="restore of what runs")
    assert "already, so nothing is restored" in result[2], result[2][-3000:]
    assert read(router["state"])["restores"] == []


def test_restore_onto_a_fresh_router(router):
    expect(run(PLAYBOOK, router_vars(router)), "opnsense", changed=1, note="first backup")
    (backup,) = stored(router)
    kept = opened(router["backups"] / backup)
    write(router["state"], fake_opnsense.fresh_state())
    router["calls"].write_text("")

    rc, _, out = run(PLAYBOOK, router_vars(router, opnsense_restore="latest"))
    assert rc != 0 and f"presents certificate SHA-256 {router['fresh']}" in out, out[-3000:]
    assert f"-e opnsense_bootstrap_cert_sha256={router['fresh']}" in out, out[-3000:]
    assert [r for r in requests(router) if r[0] == "bootstrap"] == [], "something was sent to an unpinned router"
    assert read(router["state"])["restores"] == []

    pinned = router_vars(router, opnsense_restore="latest", opnsense_bootstrap_cert_sha256=router["fresh"])
    dry = run(PLAYBOOK, pinned, check=True)
    expect(dry, "opnsense", note="dry run onto the fresh router")
    assert f"Would restore {backup}" in dry[2] and read(router["state"])["restores"] == [], dry[2][-3000:]

    restored = run(PLAYBOOK, pinned)
    expect(restored, "opnsense", changed=True, note="restore onto the fresh router")
    (made,) = read(router["state"])["restores"]
    assert made["address"] == "bootstrap" and made["content"] == kept
    state = read(router["state"])
    assert not state["fresh"] and stored(router)[-1] == fake_opnsense.running(state)["id"]
    assert f"OPNsense {fake_opnsense.RELEASE} at {ROUTER}" in restored[2], restored[2][-3000:]
    expect(run(PLAYBOOK, router_vars(router)), "opnsense", changed=0, note="run after the restore")
    for result in (dry, restored):
        no_secret(result[2])


def test_restore_refuses_a_wrong_root_password(router):
    expect(run(PLAYBOOK, router_vars(router)), "opnsense", changed=1, note="first backup")
    write(router["state"], fake_opnsense.fresh_state())
    given = router_vars(
        router,
        opnsense_restore="latest",
        opnsense_bootstrap_cert_sha256=router["fresh"],
        vault_opnsense_root_password="not-the-root-password",
    )
    rc, _, out = run(PLAYBOOK, given)
    assert rc != 0 and "refused the web UI login of root" in out, out[-3000:]
    assert "not-the-root-password" not in out and read(router["state"])["restores"] == []


def test_restore_stops_for_the_console_when_the_interfaces_do_not_match(router):
    expect(run(PLAYBOOK, router_vars(router)), "opnsense", changed=1, note="first backup")
    state = fake_opnsense.save_revision(read(router["state"]), "a change to roll back")
    state["interface_mismatch"] = True
    write(router["state"], state)
    rc, _, out = run(PLAYBOOK, router_vars(router, opnsense_restore="latest"))
    assert rc != 0 and "1) Assign interfaces" in out, out[-3000:]
    (made,) = read(router["state"])["restores"]
    assert made["reboot"] and read(router["state"])["down_until"] == 0, "the router rebooted despite the mismatch"
