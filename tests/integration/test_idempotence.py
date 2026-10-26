"""Runs each role repeatedly against stateful fakes and checks that it is idempotent.

For each role: a dry run changes nothing; the first real run applies the configuration; a second
run changes nothing and does not fail; a dry run afterwards reports nothing; drift introduced behind
the role's back is corrected by one run and left alone by the next. The Proxmox storage role is also
told the node's own storage to check, which must change nothing and must fail on a storage that cannot hold
VM disks. The Proxmox apt role starts from the repository files a fresh install has, and must disable the
enterprise ones before it refreshes the package lists, or the fake apt-get answers 401 as the real one does;
it says a reboot is needed only when an upgrade changed what takes effect at boot.

The roles run with this repo's own configuration (group_vars/all/storage.yml, group_vars/nas,
group_vars/proxmox) and test addresses on loopback, with a NAS and, for the Proxmox role, without one.
Nothing here can reach a real machine: the inventory, config and addresses are all local to this
directory.

Needs the project virtualenv and openssl. Run: just test idempotence
"""

import base64
import hashlib
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fake_kubectl  # noqa: E402
import fake_pve  # noqa: E402
import fake_truenas  # noqa: E402

RECAP = re.compile(r"^(\S+)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", re.MULTILINE)
PLAYBOOK = str(Path(sys.executable).parent / "ansible-playbook")
NAS_ADDRESS = "127.0.0.1"
PROXMOX_ADDRESS = "127.0.0.20"
# The cluster's nodes: the worker alone mounts the NAS's datasets.
NODE_ADDRESSES = {"homeserver-cp-1": "127.0.0.30", "homeserver-worker-1": "127.0.0.31"}
WORKER_ADDRESS = NODE_ADDRESSES["homeserver-worker-1"]
# Both nodes at the least the settings check allows, for a small host without a NAS: 64 GiB of disks between them.
SMALL_SIZES = {
    "homeserver-cp-1": {"memory_mib": 2048, "system_disk_gib": 10},
    "homeserver-worker-1": {
        "cores": 2,
        "memory_mib": 4096,
        "system_disk_gib": 32,
        "data_disk_gib": 11,
        "media_disk_gib": 11,
    },
}
# Sentinels that must never reach Ansible's output.
MAIL_PASSWORD = "mail-password-sentinel-6c1f"
ROTATED_MAIL_PASSWORD = "mail-password-sentinel-rotated-91ad"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(playbook, extra, check=False, env=None):
    """Run a test playbook; return (return code, {host: stats}, combined output). `env` adds to the environment,
    such as a HOME of the test's own."""
    # ANSIBLE_FORCE_COLOR overrides ANSIBLE_NOCOLOR, and a coloured PLAY RECAP does not match RECAP.
    environment = {k: v for k, v in os.environ.items() if k != "ANSIBLE_FORCE_COLOR"}
    environment |= {"ANSIBLE_CONFIG": str(HERE / "ansible.cfg"), "ANSIBLE_NOCOLOR": "1", **(env or {})}
    cmd = [
        PLAYBOOK,
        str(HERE / playbook),
        "--diff",
        "-e",
        json.dumps({"ansible_python_interpreter": sys.executable, **extra}),
    ]
    if check:
        cmd.append("--check")
    proc = subprocess.run(cmd, env=environment, cwd=HERE, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    out = proc.stdout + proc.stderr
    stats = {
        h: {"ok": int(o), "changed": int(c), "unreachable": int(u), "failed": int(f)}
        for h, o, c, u, f in RECAP.findall(out)
    }
    return proc.returncode, stats, out


def expect(result, host, changed=None, note=""):
    """Assert a run succeeded, and how much it changed (None: anything; True: something; or a number)."""
    rc, stats, out = result
    problem = "run failed" if rc else f"no PLAY RECAP line for {host}"
    assert rc == 0 and host in stats and stats[host]["failed"] == 0 and stats[host]["unreachable"] == 0, (
        f"{note}: {problem} (rc {rc})\n{out[-6000:]}"
    )
    got = stats[host]["changed"]
    if changed is True:
        assert got > 0, f"{note}: expected changes, got none\n{out[-3000:]}"
    elif changed is not None:
        assert got == changed, f"{note}: expected changed={changed}, got {got}\n{out[-6000:]}"


def changed_tasks(out):
    """Names of the tasks that reported a change, for failure messages."""
    tasks, current = [], None
    for line in out.splitlines():
        if line.startswith("TASK ["):
            current = line
        elif line.startswith("changed:") and current:
            tasks.append(current)
    return tasks


# ---------------------------------------------------------------------------------- TrueNAS ---


@pytest.fixture(scope="module")
def nas(tmp_path_factory):
    work = tmp_path_factory.mktemp("nas")
    cert, key, state = work / "cert.pem", work / "key.pem", work / "state.json"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            key,
            "-out",
            cert,
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
        ],
        check=True,
        capture_output=True,
    )
    fingerprint = (
        subprocess.run(
            ["openssl", "x509", "-in", cert, "-noout", "-fingerprint", "-sha256"],
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
        .split("=", 1)[1]
    )
    calls = Path(os.environ.get("FAKE_TRUENAS_CALL_LOG") or work / "calls.jsonl")
    # Run in parallel, each worker starts its own fake NAS, so a test may read the log before any call.
    calls.touch()
    port = free_port()
    log = (work / "fake.log").open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            HERE / "fake_truenas.py",
            "--port",
            str(port),
            "--cert",
            cert,
            "--key",
            key,
            "--state",
            state,
            "--address",
            NAS_ADDRESS,
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
        pytest.fail("fake TrueNAS did not start")
    yield {"port": port, "state": state, "fingerprint": fingerprint, "calls": calls}
    proc.terminate()
    proc.wait(timeout=10)
    log.close()


def nas_vars(nas):
    return {
        "truenas_api_hosts": [NAS_ADDRESS],
        "truenas_api_port": nas["port"],
        "truenas_api_cert_sha256": nas["fingerprint"],
        "truenas_api_key": fake_truenas.API_KEY,
        "truenas_static_address": f"{NAS_ADDRESS}/8",
        "truenas_gateway": "127.0.0.254",
        "truenas_nameservers": ["127.0.0.53"],
        "proxmox_address": PROXMOX_ADDRESS,
        "truenas_ui_restart_wait": 0,
        "truenas_api_connect_wait": 5,
        "kubernetes_node_addresses": NODE_ADDRESSES,
        "vm_storage_ephemeral_reservation": "100G",
        "truenas_mail_server": "smtp.example.org",
        "truenas_mail_user": "nas@example.org",
        "truenas_alert_email": "admin@example.org",
        "vault_truenas_mail_password": MAIL_PASSWORD,
    }


def read(path):
    return json.loads(Path(path).read_text())


def write(path, state):
    Path(path).write_text(json.dumps(state, indent=1, sort_keys=True))


def semantic(state):
    """The state without job bookkeeping, which read-only jobs (pool.import_find) also touch."""
    return {k: v for k, v in state.items() if k not in ("jobs", "next_id")}


# Every share the NAS ends up with, and the hosts it is exported to: the VM shares to the Proxmox host, the
# cluster's to its worker.
EXPECTED_SHARES = {
    "/mnt/persistent/proxmox/vm": [PROXMOX_ADDRESS],
    "/mnt/ephemeral/proxmox/vm": [PROXMOX_ADDRESS],
    "/mnt/ephemeral/proxmox/claude-on-proxmox": [PROXMOX_ADDRESS],
    "/mnt/persistent/proxmox/backup": [PROXMOX_ADDRESS],
    "/mnt/persistent/nextcloud": [WORKER_ADDRESS],
    "/mnt/persistent/photoprism": [WORKER_ADDRESS],
    "/mnt/ephemeral/frigate": [WORKER_ADDRESS],
    "/mnt/ephemeral/jellyfin": [WORKER_ADDRESS],
}


def test_truenas_role_is_idempotent(nas):
    extra = nas_vars(nas)
    outputs = []

    before = semantic(read(nas["state"]))
    result = run("truenas.yml", extra, check=True)
    outputs.append(result[2])
    expect(result, "truenas", changed=True, note="dry run on a fresh NAS")
    assert semantic(read(nas["state"])) == before, "the dry run changed the NAS"

    result = run("truenas.yml", extra)
    outputs.append(result[2])
    expect(result, "truenas", changed=True, note="first run")

    result = run("truenas.yml", extra)
    outputs.append(result[2])
    expect(result, "truenas", changed=0, note=f"second run (changed: {changed_tasks(result[2])})")

    result = run("truenas.yml", extra, check=True)
    outputs.append(result[2])
    expect(result, "truenas", changed=0, note="dry run after applying")

    s = read(nas["state"])
    iface = s["interfaces"]["enp7s0"]
    assert iface["ipv4_dhcp"] is False and iface["aliases"] == [{"type": "INET", "address": NAS_ADDRESS, "netmask": 8}]
    assert s["interface_snapshot"] is None and s["checkin_waiting"] is None, "the address change was not checked in"
    assert (s["network"]["ipv4gateway"], s["network"]["nameserver1"]) == ("127.0.0.254", "127.0.0.53")
    assert s["sysds_pool"] == "boot-pool", "the system dataset moved off the boot pool"
    assert sorted(p["name"] for p in s["pools"]) == ["ephemeral", "persistent"]
    assert all(p["autotrim"]["rawvalue"] == "on" for p in s["pools"])
    assert len(s["scrubs"]) == 2
    ds = s["datasets"]
    assert ds["persistent/proxmox/vm"]["sync"]["rawvalue"] == "standard"
    assert ds["ephemeral/proxmox/vm"]["sync"]["rawvalue"] == "disabled"
    assert ds["ephemeral/proxmox"]["quota"]["rawvalue"] == "1099511627776"
    assert ds["ephemeral/proxmox"]["reservation"]["rawvalue"] == str(100 * 2**30)
    assert ds["persistent/proxmox/backup"]["recordsize"]["rawvalue"] == "1048576"
    assert ds["persistent/proxmox/vm"]["recordsize"]["rawvalue"] == "65536"
    assert ds["persistent/proxmox/vm"]["quota"]["rawvalue"] == "1099511627776"
    # The project pool: a dataset of its own with the ephemeral class's properties.
    pool = ds["ephemeral/proxmox/claude-on-proxmox"]
    assert (pool["sync"]["rawvalue"], pool["recordsize"]["rawvalue"]) == ("disabled", "65536")
    assert ds["ephemeral/proxmox"]["acltype"]["rawvalue"] == "posix"
    assert ds["ephemeral/jellyfin"]["acltype"]["rawvalue"] == "nfsv4", "the media dataset's ACLs were changed"
    # The cluster's datasets: 1M records, the media dataset left as it was and only exported.
    for name, sync in (("persistent/nextcloud", "standard"), ("persistent/photoprism", "standard")):
        assert (ds[name]["sync"]["rawvalue"], ds[name]["recordsize"]["rawvalue"]) == (sync, "1048576")
    assert ds["ephemeral/frigate"]["quota"]["rawvalue"] == str(500 * 2**30)
    assert ds["ephemeral/frigate"]["sync"]["rawvalue"] == "disabled"
    assert sorted(sh["path"] for sh in s["shares"]) == sorted(EXPECTED_SHARES)
    assert {sh["path"]: sh["hosts"] for sh in s["shares"]} == EXPECTED_SHARES
    assert all(sh["maproot_user"] == "root" for sh in s["shares"])
    # Snapshot tasks: the persistent VMs' disks hourly and daily, the cluster's irreplaceable datasets hourly, daily
    # and weekly, nothing on the ephemeral pool.
    tasks = {(t["dataset"], t["naming_schema"].split("-")[0]): t for t in s["snapshot_tasks"]}
    assert sorted(tasks) == sorted(
        [("persistent/proxmox/vm", "hourly"), ("persistent/proxmox/vm", "daily")]
        + [
            (d, kind)
            for d in ("persistent/nextcloud", "persistent/photoprism")
            for kind in ("hourly", "daily", "weekly")
        ]
    )
    assert (
        tasks[("persistent/proxmox/vm", "daily")]["lifetime_value"],
        tasks[("persistent/proxmox/vm", "daily")]["lifetime_unit"],
    ) == (7, "DAY")
    assert (
        tasks[("persistent/nextcloud", "daily")]["lifetime_value"],
        tasks[("persistent/nextcloud", "daily")]["lifetime_unit"],
    ) == (30, "DAY")
    assert tasks[("persistent/photoprism", "weekly")]["schedule"]["dow"] == "7"
    assert {key: t["allow_empty"] for key, t in tasks.items()} == {key: key[1] != "hourly" for key in tasks}
    # Alert mail, with the password from the vault.
    assert (s["mail"]["outgoingserver"], s["mail"]["smtp"], s["mail"]["pass"]) == (
        "smtp.example.org",
        True,
        MAIL_PASSWORD,
    )
    assert [(a["name"], a["attributes"]["email"]) for a in s["alert_services"]] == [("Email", "admin@example.org")]
    services = {sv["service"]: sv for sv in s["services"]}
    assert services["nfs"]["enable"] and services["nfs"]["state"] == "RUNNING"
    assert not services["ssh"]["enable"] and services["ssh"]["state"] == "STOPPED"
    assert s["general"]["ui_httpsredirect"] is True

    # Drift behind the role's back, in every stage that corrects things.
    s["datasets"]["ephemeral/proxmox/vm"]["sync"] = fake_truenas.prop("standard", "LOCAL")
    s["datasets"]["ephemeral/proxmox"]["quota"] = fake_truenas.prop("0", "DEFAULT")
    s["shares"][0]["hosts"] = ["127.0.0.99"]
    services["ssh"].update(enable=True, state="RUNNING")
    s["nfs"]["protocols"] = ["NFSV4"]
    s["general"]["ui_httpsredirect"] = False
    s["pools"][0]["autotrim"] = fake_truenas.prop("off", "LOCAL")
    s["scrubs"][0]["threshold"] = 7
    s["ntp"][0]["minpoll"] = 4
    s["network"]["nameserver1"] = "127.0.0.99"
    s["snapshot_tasks"][0]["lifetime_value"] = 9
    s["alert_services"][0]["level"] = "CRITICAL"
    write(nas["state"], s)

    result = run("truenas.yml", extra)
    outputs.append(result[2])
    expect(result, "truenas", changed=True, note="run after drift")
    result = run("truenas.yml", extra)
    outputs.append(result[2])
    expect(result, "truenas", changed=0, note=f"second run after drift (changed: {changed_tasks(result[2])})")

    s = read(nas["state"])
    assert s["datasets"]["ephemeral/proxmox/vm"]["sync"]["rawvalue"] == "disabled"
    assert s["datasets"]["ephemeral/proxmox"]["quota"]["rawvalue"] == "1099511627776"
    assert {sh["path"]: sh["hosts"] for sh in s["shares"]} == EXPECTED_SHARES
    assert next(sv for sv in s["services"] if sv["service"] == "ssh")["state"] == "STOPPED"
    assert sorted(s["nfs"]["protocols"]) == ["NFSV3", "NFSV4"]
    assert s["general"]["ui_httpsredirect"] is True
    assert all(p["autotrim"]["rawvalue"] == "on" for p in s["pools"])
    assert all(t["threshold"] == 35 for t in s["scrubs"])
    assert all(n["minpoll"] == 6 for n in s["ntp"])
    assert s["network"]["nameserver1"] == "127.0.0.53"
    assert s["snapshot_tasks"][0]["lifetime_value"] == 1 and s["alert_services"][0]["level"] == "WARNING"

    # A new alert address in local.yml: alertservice.update takes the whole service, which the fake enforces.
    moved = {**extra, "truenas_alert_email": "alerts@example.org"}
    expect(run("truenas.yml", moved), "truenas", changed=1, note="a new alert address")
    assert [a["attributes"]["email"] for a in read(nas["state"])["alert_services"]] == ["alerts@example.org"]
    expect(run("truenas.yml", extra), "truenas", changed=1, note="the alert address back")

    # The single disk of the ephemeral pool lost and the pool made anew: the media dataset, which the role otherwise
    # leaves alone, is created again so that its share can be.
    s = read(nas["state"])
    del s["datasets"]["ephemeral/jellyfin"]
    s["shares"] = [sh for sh in s["shares"] if sh["path"] != "/mnt/ephemeral/jellyfin"]
    write(nas["state"], s)
    expect(run("truenas.yml", extra), "truenas", changed=True, note="the media dataset lost")
    s = read(nas["state"])
    assert s["datasets"]["ephemeral/jellyfin"]["recordsize"]["rawvalue"] == "1048576"
    assert {sh["path"]: sh["hosts"] for sh in s["shares"]} == EXPECTED_SHARES
    expect(run("truenas.yml", extra), "truenas", changed=0, note="the media dataset made again, second run")

    # A reservation removed from the configuration is removed on the NAS too: it is sent as 0, none.
    unreserved = {**extra, "vm_storage_ephemeral_reservation": None}
    expect(run("truenas.yml", unreserved), "truenas", changed=1, note="the reservation removed")
    assert read(nas["state"])["datasets"]["ephemeral/proxmox"]["reservation"]["rawvalue"] == "0"
    expect(run("truenas.yml", unreserved), "truenas", changed=0, note="the reservation removed, second run")
    expect(run("truenas.yml", extra), "truenas", changed=1, note="the reservation back")

    # A rotated mail password reaches the NAS on the next run, as a change, and the run after changes nothing.
    rotated = {**extra, "vault_truenas_mail_password": ROTATED_MAIL_PASSWORD}
    result = run("truenas.yml", rotated, check=True)
    outputs.append(result[2])
    expect(result, "truenas", changed=1, note="dry run with a rotated mail password")
    assert "<secret, changed>" in result[2], result[2][-3000:]
    for n in (1, 2):
        result = run("truenas.yml", rotated)
        outputs.append(result[2])
        expect(result, "truenas", changed=1 if n == 1 else 0, note=f"run {n} with a rotated mail password")
    assert read(nas["state"])["mail"]["pass"] == ROTATED_MAIL_PASSWORD

    for out in outputs:
        assert fake_truenas.PRIVATE_KEY not in out, "the UI certificate's private key reached Ansible output"
        assert fake_truenas.API_KEY not in out, "the API key reached Ansible output"
        assert MAIL_PASSWORD not in out and ROTATED_MAIL_PASSWORD not in out, "the mail password reached Ansible output"


def test_truenas_role_without_a_cluster(nas):
    """A NAS without a cluster (no kubernetes_node_addresses in local.yml): the VM storage's datasets and shares as
    with one, and none of the cluster's datasets, snapshot tasks or shares."""
    extra = {k: v for k, v in nas_vars(nas).items() if k != "kubernetes_node_addresses"}
    # A fresh NAS: the module's fake is shared, and the tests before this one built a cluster's datasets on it.
    shared = read(nas["state"])
    write(nas["state"], fake_truenas.initial_state(NAS_ADDRESS))
    try:
        expect(run("truenas.yml", extra), "truenas", changed=True, note="first run without a cluster")
        expect(run("truenas.yml", extra), "truenas", changed=0, note="second run without a cluster")
        s = read(nas["state"])
    finally:
        write(nas["state"], shared)
    cluster = {"persistent/nextcloud", "persistent/photoprism", "ephemeral/frigate"}
    assert not cluster & set(s["datasets"]), sorted(cluster & set(s["datasets"]))
    assert not cluster & {t["dataset"] for t in s["snapshot_tasks"]}
    vm_shares = {path: hosts for path, hosts in EXPECTED_SHARES.items() if hosts == [PROXMOX_ADDRESS]}
    assert {sh["path"]: sh["hosts"] for sh in s["shares"]} == vm_shares


def test_truenas_role_with_defaults_changes_nothing(nas):
    """A user who sets only the connection: the role reports and leaves the NAS alone."""
    extra = {k: v for k, v in nas_vars(nas).items() if k.startswith("truenas_api")}
    before = semantic(read(nas["state"]))
    result = run("truenas_defaults.yml", extra)
    expect(result, "truenas", changed=0, note="role with only its defaults")
    assert semantic(read(nas["state"])) == before


# ---------------------------------------------------------------------------------- Proxmox ---


# A fresh Proxmox VE 9 install's repository files: the enterprise ones enabled, as the installer leaves them.
KEYRING = "Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg\n"
FRESH_SOURCES = {
    "pve-enterprise.sources": (
        "Types: deb\nURIs: https://enterprise.proxmox.com/debian/pve\nSuites: trixie\nComponents: pve-enterprise\n"
        + KEYRING
    ),
    "ceph.sources": (
        "Types: deb\nURIs: https://enterprise.proxmox.com/debian/ceph-tentacle\nSuites: trixie\n"
        "Components: enterprise\n" + KEYRING
    ),
    "debian.sources": (
        "Types: deb\nURIs: http://deb.debian.org/debian\nSuites: trixie trixie-updates\nComponents: main contrib\n"
        "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n"
    ),
}
NO_SUBSCRIPTION = (
    "Types: deb\nURIs: http://download.proxmox.com/debian/pve\nSuites: trixie\nComponents: pve-no-subscription\n"
    + KEYRING
)


def pve_vars(tmp_path, settled=False, nas=True):
    """Variables for the fake Proxmox host: its state file and a repository directory as a fresh install
    leaves it or, settled, as a run of this repo's configuration leaves it: the no-subscription repository
    written, the enterprise ones disabled, and the package lists refreshed with nothing left to upgrade."""
    state, sources = tmp_path / "pve.json", tmp_path / "sources"
    sources.mkdir()
    (tmp_path / "run").mkdir()
    for name, text in FRESH_SOURCES.items():
        enterprise = name in ("pve-enterprise.sources", "ceph.sources")
        (sources / name).write_text(("Enabled: false\n" if settled and enterprise else "") + text)
    if settled:
        (sources / "proxmox.sources").write_text(NO_SUBSCRIPTION)
    # Mode 0644, as the installer leaves them and the role writes proxmox.sources, whatever this process's umask:
    # under a user's 0002, a settled file would read 0664, and a run would report changing it.
    for path in sources.iterdir():
        path.chmod(0o644)
    if settled:
        s = fake_pve.fresh_state()
        s["packages"]["installed"] = s["packages"]["lists"] = dict(s["packages"]["repository"])
        write(state, s)
    extra = {
        "fake_pve_state": str(state),
        "proxmox_apt_sources_dir": str(sources),
        "proxmox_apt_suite": "trixie",
        "proxmox_apt_reboot_required_file": str(tmp_path / "run" / "reboot-required"),
        "proxmox_storage_systemd_dir": str(tmp_path / "systemd"),
    }
    return {**extra, "truenas_static_address": "127.0.0.10/24"} if nas else extra


def wait_dropin(extra):
    """The drop-in that holds the guests' start at boot until their storages are active."""
    return Path(extra["proxmox_storage_systemd_dir"]) / "pve-guests.service.d" / "proxmox-storage-wait.conf"


REQUESTED = "*** System restart required ***\n"


def sources_of(extra):
    """The repository files the fake host has, by name."""
    return {p.name: p.read_text() for p in Path(extra["proxmox_apt_sources_dir"]).iterdir()}


def test_proxmox_storage_role_is_idempotent(tmp_path):
    extra = pve_vars(tmp_path)
    state = Path(extra["fake_pve_state"])

    result = run("proxmox.yml", extra, check=True)
    expect(result, "homeserver", note="dry run")
    for sid in ("homeserver-persistent", "homeserver-ephemeral", "claude-on-proxmox-ephemeral", "homeserver-backup"):
        assert re.search(rf'{sid}"?:\s*"?add', result[2]), (
            f"the dry run does not plan to add {sid}\n{result[2][-3000:]}"
        )
    assert not state.exists() or read(state)["mutations"] == 0, "the dry run changed the host"

    expect(run("proxmox.yml", extra), "homeserver", changed=True, note="first run")
    result = run("proxmox.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run (changed: {changed_tasks(result[2])})")
    expect(run("proxmox.yml", extra, check=True), "homeserver", changed=0, note="dry run after applying")

    s = read(state)
    nfs = {e["storage"]: e for e in s["storage"] if e["type"] == "nfs"}
    assert sorted(nfs) == [
        "claude-on-proxmox-ephemeral",
        "homeserver-backup",
        "homeserver-ephemeral",
        "homeserver-persistent",
    ]
    for sid, entry in nfs.items():
        assert (entry["server"], entry["content"], entry["format"], entry["options"]) == (
            "127.0.0.10",
            "backup" if sid == "homeserver-backup" else "images",
            "qcow2",
            "vers=4.2",
        )
        assert "disable" not in entry
    assert nfs["homeserver-backup"]["export"] == "/mnt/persistent/proxmox/backup"
    assert nfs["homeserver-ephemeral"]["export"] == "/mnt/ephemeral/proxmox/vm"
    assert nfs["claude-on-proxmox-ephemeral"]["export"] == "/mnt/ephemeral/proxmox/claude-on-proxmox"
    assert s["node"]["startall-onboot-delay"] == 120
    # Then the start waits for the storages that hold VM disks, not the backups', and systemd was reloaded once.
    dropin = wait_dropin(extra).read_text()
    assert "for s in homeserver-persistent homeserver-ephemeral claude-on-proxmox-ephemeral; do" in dropin, dropin
    assert "ExecStartPre=-/usr/bin/timeout 600 /bin/sh -c" in dropin and '--storage "$$s"' in dropin, dropin
    assert s["daemon_reloads"] == 1

    # Drift: options, content and the enabled flag changed by hand.
    nfs["homeserver-persistent"].update(options="vers=3", content="images,rootdir", disable=1)
    write(state, s)
    expect(run("proxmox.yml", extra), "homeserver", changed=True, note="run after drift")
    result = run("proxmox.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run after drift (changed: {changed_tasks(result[2])})")
    entry = next(e for e in read(state)["storage"] if e["storage"] == "homeserver-persistent")
    assert (entry["options"], entry["content"], "disable" in entry) == ("vers=4.2", "images", False)

    # Two content types, which the fake hands back in a different order on every other read: a comparison
    # that did not sort both sides would see drift on one of the two runs after the correction.
    two = {**extra, "proxmox_storage_content": ["images", "rootdir"]}
    expect(run("proxmox.yml", two), "homeserver", changed=True, note="run with two content types")
    for n in (1, 2):
        result = run("proxmox.yml", two)
        note = f"run {n} after two content types (changed: {changed_tasks(result[2])})"
        expect(result, "homeserver", changed=0, note=note)
    entry = next(e for e in read(state)["storage"] if e["storage"] == "homeserver-persistent")
    assert sorted(entry["content"].split(",")) == ["images", "rootdir"]

    # A storage id that points somewhere else is refused, not replaced.
    s = read(state)
    next(e for e in s["storage"] if e["storage"] == "homeserver-ephemeral")["export"] = "/mnt/elsewhere"
    write(state, s)
    rc, _, out = run("proxmox.yml", extra)
    assert rc != 0 and "homeserver-ephemeral" in out and "Remove or rename them by hand" in out, out[-3000:]
    assert (
        next(e for e in read(state)["storage"] if e["storage"] == "homeserver-ephemeral")["export"] == "/mnt/elsewhere"
    )


def test_proxmox_storage_role_without_a_nas(tmp_path):
    """With no NAS address, this repo's configuration adds no NFS storage, sets no boot delay, and checks local-lvm."""
    extra = {
        **pve_vars(tmp_path, settled=True, nas=False),
        "truenas_static_address": "",
        "truenas_bootstrap_address": "",
    }
    state = Path(extra["fake_pve_state"])

    result = run("proxmox.yml", extra, check=True)
    expect(result, "homeserver", changed=0, note="dry run without a NAS")
    assert re.search(r'local-lvm"?:\s*"?present', result[2]), result[2][-3000:]
    assert "homeserver-" not in result[2], f"an NFS storage id appears without a NAS\n{result[2][-3000:]}"
    assert not state.exists() or read(state)["mutations"] == 0, "the dry run changed the host"
    for note in ("first run", "second run"):
        expect(run("proxmox.yml", extra), "homeserver", changed=0, note=f"{note} without a NAS")
    s = read(state)
    assert s["mutations"] == 0, "running without a NAS changed the host"
    assert [e["storage"] for e in s["storage"] if e["type"] == "nfs"] == []
    assert "startall-onboot-delay" not in s["node"], "the boot delay was set with no NAS to wait for"
    assert not wait_dropin(extra).exists() and "daemon_reloads" not in s

    # A wait left from a setup with a NAS is removed, and systemd reloaded.
    wait_dropin(extra).parent.mkdir(parents=True)
    wait_dropin(extra).write_text("[Service]\n")
    expect(run("proxmox.yml", extra), "homeserver", changed=2, note="a wait left from a NAS")
    assert not wait_dropin(extra).exists() and read(state)["daemon_reloads"] == 1
    expect(run("proxmox.yml", extra), "homeserver", changed=0, note="a wait left from a NAS, again")

    # A ZFS install's name on an LVM host: refused before anything runs.
    rc, _, out = run("proxmox.yml", {**extra, "proxmox_local_vm_storage": "local-zfs"}, check=True)
    assert rc != 0 and "local-zfs: does not exist" in out, out[-3000:]
    assert read(state)["mutations"] == 0

    # A blank (null) address and one that is only whitespace are no address either.
    blank = {**extra, "truenas_static_address": None, "truenas_bootstrap_address": " "}
    result = run("proxmox.yml", blank, check=True)
    expect(result, "homeserver", changed=0, note="dry run with blank addresses")
    assert "homeserver-" not in result[2] and re.search(r'local-lvm"?:\s*"?present', result[2]), result[2][-3000:]


def test_proxmox_storage_role_needs_the_static_address(tmp_path):
    """A NAS known only by its bootstrap address counts as a NAS, but Proxmox mounts it at its static address, so
    the run is refused, naming the variable the server comes from."""
    extra = {**pve_vars(tmp_path, settled=True, nas=False), "truenas_static_address": ""}
    rc, _, out = run("proxmox.yml", {**extra, "truenas_bootstrap_address": "127.0.0.10"}, check=True)
    expected = (
        "No NFS server for homeserver-persistent, homeserver-ephemeral, claude-on-proxmox-ephemeral, "
        "homeserver-backup: set proxmox_storage_nfs_server"
    )
    assert rc != 0 and expected in out, out[-3000:]


def test_proxmox_apt_role_is_idempotent(tmp_path):
    """From a fresh install's repository files: the dry run shows the files to write and change and no pending
    upgrade (the lists were never refreshed); the first run writes them, disables the enterprise ones before it
    refreshes the lists, and upgrades; later runs change nothing; drift is corrected."""
    extra = pve_vars(tmp_path)
    state, sources = Path(extra["fake_pve_state"]), Path(extra["proxmox_apt_sources_dir"])
    flag = Path(extra["proxmox_apt_reboot_required_file"])
    fresh = sources_of(extra)

    result = run("proxmox.yml", extra, check=True)
    expect(result, "homeserver", changed=True, note="dry run on a fresh install")
    out = result[2]
    assert "proxmox.sources" in out and "+Enabled: false" in out, (
        f"the dry run shows no repository change\n{out[-3000:]}"
    )
    assert "before the repository changes above: 0 upgraded, 0 newly installed" in out, out[-3000:]
    assert "No reboot needed." in out, out[-3000:]
    assert sources_of(extra) == fresh, "the dry run changed the repository files"
    assert read(state)["mutations"] == 0 and read(state)["apt_get_runs"] == 1, "the dry run did more than simulate"

    # The first run upgrades, which installs a kernel image and upgrades the kernel's meta package: only the
    # image asks for a reboot, and every run says so until the node boots, which empties /run.
    result = run("proxmox.yml", extra)
    expect(result, "homeserver", changed=True, note="first run")
    assert "2 upgraded, 1 newly installed" in result[2], result[2][-3000:]
    kernel = "Reboot the node when it is safe to: the upgrade changes proxmox-kernel-7.0.14-23-pve-signed;"
    assert f"{kernel} {flag} asks for one." in result[2], result[2][-3000:]
    assert flag.read_text() == REQUESTED
    settled = {
        **fresh,
        "proxmox.sources": NO_SUBSCRIPTION,
        "pve-enterprise.sources": "Enabled: false\n" + fresh["pve-enterprise.sources"],
        "ceph.sources": "Enabled: false\n" + fresh["ceph.sources"],
    }
    assert sources_of(extra) == settled
    s = read(state)
    # The old kernel stays installed beside the new one, as on a real node.
    assert s["packages"]["repository"].items() <= s["packages"]["installed"].items(), "not upgraded"

    result = run("proxmox.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run (changed: {changed_tasks(result[2])})")
    assert f"Reboot the node when it is safe to: {flag} asks for one." in result[2], result[2][-3000:]
    flag.unlink()
    result = run("proxmox.yml", extra, check=True)
    expect(result, "homeserver", changed=0, note="dry run after applying")
    assert "With the package lists as last refreshed: 0 upgraded" in result[2], result[2][-3000:]

    # Drift: the enterprise repository re-enabled by hand, the no-subscription one deleted, and a new release
    # in the repository. The lists are refreshed only after the files are put right, or the fake answers 401.
    (sources / "pve-enterprise.sources").write_text(fresh["pve-enterprise.sources"])
    (sources / "proxmox.sources").unlink()
    s = read(state)
    s["packages"]["repository"]["pve-manager"] = "9.2.4-1"
    write(state, s)
    result = run("proxmox.yml", extra)
    expect(result, "homeserver", changed=True, note="run after drift")
    assert sources_of(extra) == settled and "1 upgraded" in result[2]
    assert read(state)["packages"]["installed"]["pve-manager"] == "9.2.4-1"
    assert not flag.exists() and "No reboot needed." in result[2], "asked for a reboot without a kernel upgrade"
    result = run("proxmox.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run after drift (changed: {changed_tasks(result[2])})")

    # A new kernel in the repository, with the package lists refreshed by hand so a dry run can see it: the dry
    # run says it would need a reboot, and the real run asks for one.
    s = read(state)
    s["packages"]["repository"]["proxmox-kernel-7.0.20-1-pve-signed"] = "7.0.20-1"
    s["packages"]["lists"] = dict(s["packages"]["repository"])
    write(state, s)
    result = run("proxmox.yml", extra, check=True)
    expect(result, "homeserver", changed=True, note="dry run with a new kernel")
    assert "the upgrade changes proxmox-kernel-7.0.20-1-pve-signed." in result[2] and not flag.exists(), result[2][
        -3000:
    ]
    expect(run("proxmox.yml", extra), "homeserver", changed=True, note="run installing a new kernel")
    assert flag.exists()
    flag.unlink()

    # Without the upgrade, apt-get is never run; a file to disable that does not exist is left alone.
    runs = read(state)["apt_get_runs"]
    expect(run("proxmox.yml", {**extra, "proxmox_apt_upgrade": False}), "homeserver", changed=0, note="no upgrade")
    assert read(state)["apt_get_runs"] == runs, "apt-get ran with proxmox_apt_upgrade false"
    more = {**extra, "proxmox_apt_disabled": ["pve-enterprise", "ceph", "pbs-enterprise"]}
    expect(run("proxmox.yml", more), "homeserver", changed=0, note="a missing file to disable")
    assert sources_of(extra) == settled

    # An enterprise repository left enabled makes the refresh fail as it does for real, so a configuration
    # that forgets to disable one cannot pass.
    (sources / "pve-enterprise.sources").write_text(fresh["pve-enterprise.sources"])
    rc, _, out = run("proxmox.yml", {**extra, "proxmox_apt_disabled": []})
    assert rc != 0 and "401  Unauthorized" in out, out[-3000:]
    expect(run("proxmox.yml", extra), "homeserver", changed=True, note="run after the refusal")

    # Refused, each with what to change (the first by argument validation, the rest by the role's own check).
    proxmox = {
        "name": "proxmox",
        "uris": ["http://download.proxmox.com/debian/pve"],
        "components": ["pve-no-subscription"],
    }
    for given, message in (
        ({"proxmox_apt_sources": "proxmox"}, "we were unable to convert to dict"),
        ({"proxmox_apt_disabled": "pve-enterprise"}, "proxmox_apt_disabled must be a list of repository file names"),
        (
            {"proxmox_apt_sources": [{**proxmox, "components": "pve-no-subscription"}]},
            "needs uris and components, each a list",
        ),
        ({"proxmox_apt_sources": [{**proxmox, "components": []}]}, "Repository proxmox in proxmox_apt_sources needs"),
        (
            {"proxmox_apt_sources": [{**proxmox, "name": "ceph"}]},
            "unique across proxmox_apt_sources and proxmox_apt_disabled",
        ),
        ({"proxmox_apt_disabled": ["pve-enterprise", "../cron.d/x"]}, "none may contain a slash"),
        ({"proxmox_apt_disabled": ["pve-enterprise", ""]}, "none may be empty"),
    ):
        rc, _, out = run("proxmox.yml", {**extra, **given}, check=True)
        assert rc != 0 and message in out, f"{given}: expected {message!r}\n{out[-3000:]}"
    assert sources_of(extra) == settled


def offer(state, installed=None, repository=None, **more):
    """Change the fake node: packages installed and offered (the lists refreshed), anything else."""
    s = read(state)
    s["packages"]["installed"].update(installed or {})
    s["packages"]["repository"].update(repository or {})
    s["packages"]["lists"] = dict(s["packages"]["repository"])
    s.update(more)
    write(state, s)


def test_proxmox_apt_role_asks_for_a_reboot_only_for_what_takes_effect_at_boot(tmp_path):
    """From a settled host: an upgrade of a package that only shares the kernel packages' prefix asks for nothing.
    An unsigned kernel and the listed packages, dbus and CPU microcode here, are predicted by the dry run and
    recorded. An upgrade that fails after unpacking still records what it unpacked, and a flag file someone else
    wrote is honoured."""
    extra = pve_vars(tmp_path, settled=True)
    state, flag = Path(extra["fake_pve_state"]), Path(extra["proxmox_apt_reboot_required_file"])
    ask = "Reboot the node when it is safe to:"

    # proxmox-kernel-helper, which a real node has, shares the prefix and boots nothing.
    offer(state, {"proxmox-kernel-helper": "9.2.0"}, {"proxmox-kernel-helper": "9.2.1"})
    for check in (True, False):
        result = run("proxmox.yml", extra, check=check)
        expect(result, "homeserver", changed=True, note=f"{'dry run' if check else 'run'} upgrading the kernel helper")
        assert "No reboot needed." in result[2], result[2][-3000:]
    assert not flag.exists()

    # An unsigned kernel, as on a node without Secure Boot's build, with dbus and microcode: the dry run predicts
    # them and the run records them.
    offer(
        state,
        installed={"dbus": "1.16.2-2", "intel-microcode": "3.20251111.1"},
        repository={"dbus": "1.16.2-3", "intel-microcode": "3.20260210.1", "proxmox-kernel-7.0.20-1-pve": "7.0.20-1"},
    )
    changes = "the upgrade changes proxmox-kernel-7.0.20-1-pve, dbus, intel-microcode"
    result = run("proxmox.yml", extra, check=True)
    expect(result, "homeserver", changed=True, note="dry run upgrading a kernel, dbus and microcode")
    assert f"{ask} {changes}." in result[2] and not flag.exists(), result[2][-3000:]
    result = run("proxmox.yml", extra)
    expect(result, "homeserver", changed=True, note="run upgrading a kernel, dbus and microcode")
    assert f"{ask} {changes}; {flag} asks for one." in result[2] and flag.read_text() == REQUESTED, result[2][-3000:]
    flag.unlink()
    expect(run("proxmox.yml", extra), "homeserver", changed=0, note="run after the boot")

    # An upgrade that fails after unpacking a listed package still records the reboot it needs.
    offer(state, {"amd64-microcode": "3.20250311.1"}, {"amd64-microcode": "3.20260311.1"}, dpkg_fails=True)
    rc, _, out = run("proxmox.yml", extra)
    assert rc != 0 and "returned an error code" in out and flag.read_text() == REQUESTED, out[-3000:]
    offer(state, dpkg_fails=False)
    result = run("proxmox.yml", extra)
    assert f"{ask} {flag} asks for one." in result[2], result[2][-3000:]

    # A flag file someone else wrote is honoured.
    flag.unlink()
    flag.write_text(REQUESTED)
    assert f"{ask} {flag} asks for one." in run("proxmox.yml", extra)[2]


def test_proxmox_storage_role_checks_existing_storage(tmp_path):
    """Told the node's own storage ids, the role checks them and changes nothing; a bad one fails at once."""
    state = tmp_path / "pve.json"
    extra = {
        "fake_pve_state": str(state),
        "proxmox_storage_local": ["local-lvm"],
        "proxmox_storage_systemd_dir": str(tmp_path / "systemd"),
    }

    result = run("proxmox_local.yml", extra, check=True)
    expect(result, "homeserver", changed=0, note="dry run")
    assert re.search(r'local-lvm"?:\s*"?present', result[2]), (
        f"the dry run does not report local-lvm\n{result[2][-3000:]}"
    )
    assert not state.exists() or read(state)["mutations"] == 0, "the dry run changed the host"
    # Without a boot delay, nothing in a dry run uses the node's name, so it is not read.
    assert re.search(r"TASK \[proxmox_storage : Find the node's name\][^\n]*\nskipping:", result[2]), (
        f"the dry run read the node's name\n{result[2][-3000:]}"
    )
    result = run("proxmox_local.yml", extra)
    expect(result, "homeserver", changed=0, note="apply")
    assert read(state)["mutations"] == 0, "checking the node's storage changed the host"
    # The status is read only for real, and must be read for the node's own storage too.
    status = result[2][result[2].find("Read each enabled storage's status") :]
    assert "ok: [homeserver] => (item=local-lvm)" in status, f"local-lvm's status was not read\n{result[2][-3000:]}"

    # Enabled but not active, as after a pool that failed to come up. A dry run cannot see it; the real run
    # names it.
    s = read(state)
    s["inactive"] = ["local-lvm"]
    write(state, s)
    rc, _, out = run("proxmox_local.yml", extra)
    assert rc != 0 and "local-lvm: enabled but not active" in out, f"an inactive storage passed\n{out[-3000:]}"
    # Restricted to another cluster node.
    s = read(state)
    s["inactive"] = []
    next(e for e in s["storage"] if e["storage"] == "local-lvm")["nodes"] = "elsewhere"
    write(state, s)
    rc, _, out = run("proxmox_local.yml", extra)
    assert rc != 0 and "local-lvm: not enabled on this node" in out, f"another node's storage passed\n{out[-3000:]}"
    assert read(state)["mutations"] == 0, "a failed status check changed the host"
    s = read(state)
    next(e for e in s["storage"] if e["storage"] == "local-lvm").pop("nodes")
    write(state, s)

    # Refused, each with what to change: a storage that does not exist, one that cannot hold VM disks, an empty
    # id, a string for the list, an id in both lists, an NFS storage with no server, ids Proxmox's rule rejects
    # (an uppercase one passes it, case not mattering, and fails only for not existing), and an export listed
    # twice.
    nfs = [{"id": "nas-vm", "export": "/mnt/tank/vm"}]
    server = {"proxmox_storage_nfs_server": "127.0.0.10"}
    configured = (
        "Storage configured in Proxmox: local (dir: backup,import,iso,vztmpl), local-lvm (lvmthin: images,rootdir)."
    )
    for given, message in (
        ({"proxmox_storage_local": ["local-zfs"]}, f"local-zfs: does not exist. {configured} The installer creates"),
        ({"proxmox_storage_local": ["local"]}, f"local: has no images in its content. {configured}"),
        ({"proxmox_storage_local": [""]}, "none may be empty"),
        ({"proxmox_storage_local": "local-lvm"}, "proxmox_storage_local must be a list of storage ids"),
        ({"proxmox_storage_nfs": [{**nfs[0], "id": "local-lvm"}], **server}, "unique across proxmox_storage_nfs and"),
        ({"proxmox_storage_nfs": nfs}, "No NFS server for nas-vm: set proxmox_storage_nfs_server"),
        ({"proxmox_storage_local": ["1lvm", "lvm-", "x"]}, "letter or digit; these are not: 1lvm, lvm-, x."),
        ({"proxmox_storage_nfs": [{**nfs[0], "id": "nas vm"}], **server}, "these are not: nas vm."),
        ({"proxmox_storage_local": ["Local-LVM"]}, f"Local-LVM: does not exist. {configured}"),
        (
            {"proxmox_storage_nfs": [nfs[0], {"id": "nas-vm-too", "export": "/mnt/tank/vm"}], **server},
            "proxmox_storage_nfs lists these more than once: 127.0.0.10:/mnt/tank/vm.",
        ),
    ):
        rc, _, out = run("proxmox_local.yml", {**extra, **given}, check=True)
        assert rc != 0 and message in out, f"{given}: expected {message!r}\n{out[-3000:]}"
    # For real, a bad id is refused before any NFS storage is added.
    rc, _, out = run(
        "proxmox_local.yml", {**extra, "proxmox_storage_local": ["local-zfs"], "proxmox_storage_nfs": nfs, **server}
    )
    assert rc != 0 and "local-zfs: does not exist" in out, out[-3000:]
    s = read(state)
    assert s["mutations"] == 0 and all(e["type"] != "nfs" for e in s["storage"]), "storage was added before the refusal"
    # One disabled by hand: no word about installers, since the storage is there.
    next(e for e in s["storage"] if e["storage"] == "local-lvm")["disable"] = 1
    write(state, s)
    rc, _, out = run("proxmox_local.yml", extra, check=True)
    assert rc != 0 and "local-lvm: is disabled" in out, out[-3000:]
    assert "local-lvm (lvmthin: images,rootdir, disabled)" in out and "The installer" not in out, out[-3000:]
    assert read(state)["mutations"] == 0, "a failed check changed the host"


def test_proxmox_storage_role_refuses_an_export_another_id_mounts(tmp_path):
    """One export, one storage id. An export the configuration wants under one id while the node mounts it
    under another, as a renamed id leaves behind until the old one is removed by hand, is refused in check
    mode and for real, naming both ids, before anything is added, whether or not the wanted id exists yet.
    With the old ids gone, the run proceeds."""
    # Settled: the apt role then changes nothing, so every mutation counted below is the storage role's.
    extra = pve_vars(tmp_path, settled=True)
    state = Path(extra["fake_pve_state"])
    run("proxmox.yml", extra, check=True)

    def mounted(sid, export):
        settings = {"content": "images", "format": "qcow2", "options": "vers=4.2", "path": f"/mnt/pve/{sid}"}
        return {"storage": sid, "type": "nfs", "server": "127.0.0.10", "export": export, **settings}

    pairs = (("truenas-persistent", "homeserver-persistent"), ("truenas-ephemeral", "homeserver-ephemeral"))
    s = read(state)
    s["storage"] += [mounted("truenas-persistent", "/mnt/persistent/proxmox/vm")]
    s["storage"] += [mounted("truenas-ephemeral", "/mnt/ephemeral/proxmox/vm")]
    write(state, s)
    for check in (True, False):
        rc, _, out = run("proxmox.yml", extra, check=check)
        assert rc != 0 and "already mounted under another storage id" in out and "pvesm remove" in out, out[-3000:]
        for old, new in pairs:
            assert f"is mounted as {old}, wanted as {new}" in out, out[-3000:]
        s = read(state)
        nfs = sorted(e["storage"] for e in s["storage"] if e["type"] == "nfs")
        assert s["mutations"] == 0 and nfs == ["truenas-ephemeral", "truenas-persistent"], "storage was added"
    # The wanted id exists beside the old one, as after a run that added it: the old one still has to go.
    s["storage"] += [mounted("homeserver-persistent", "/mnt/persistent/proxmox/vm")]
    write(state, s)
    rc, _, out = run("proxmox.yml", extra, check=True)
    assert rc != 0 and "is mounted as truenas-persistent, wanted as homeserver-persistent" in out, out[-3000:]
    # The old ids removed by hand.
    s = read(state)
    s["storage"] = [e for e in s["storage"] if not e["storage"].startswith("truenas-")]
    write(state, s)
    expect(run("proxmox.yml", extra), "homeserver", changed=True, note="run once the old ids are gone")
    expect(run("proxmox.yml", extra), "homeserver", changed=0, note="second run once the old ids are gone")
    nfs = sorted(e["storage"] for e in read(state)["storage"] if e["type"] == "nfs")
    assert nfs == ["claude-on-proxmox-ephemeral", "homeserver-backup", "homeserver-ephemeral", "homeserver-persistent"]


def test_truenas_role_refuses_an_unpinned_certificate(nas):
    """With the wrong certificate fingerprint, the API key is never sent; with none, and no certificate validation
    either, nothing is even tried, in a dry run too."""
    extra = {k: v for k, v in nas_vars(nas).items() if k.startswith("truenas_api")}
    extra["truenas_api_cert_sha256"] = "00" * 32
    extra["truenas_api_connect_wait"] = 0
    calls = Path(nas["calls"])

    def logged():
        return calls.read_text() if calls.exists() else ""

    logins_before = logged().count("auth.login_with_api_key")
    rc, _, out = run("truenas_defaults.yml", extra)
    assert rc != 0 and "not the pinned one" in out and "the API key was not sent" in out, out[-3000:]
    assert logged().count("auth.login_with_api_key") == logins_before, "a login was attempted"

    before = logged()
    extra["truenas_api_cert_sha256"] = ""
    for check in (True, False):
        rc, _, out = run("truenas_defaults.yml", extra, check=check)
        assert rc != 0 and "truenas_api_cert_sha256 is empty, so nothing would prove" in out, out[-3000:]
    assert logged() == before, "the NAS was asked something"


# ----------------------------------------------------------------------------------- Backups ---


def seed_storage(state, *entries):
    """Add storages to the fake host, as proxmox_storage or the owner would have."""
    s = read(state) if Path(state).exists() else fake_pve.fresh_state()
    s["storage"] += list(entries)
    write(state, s)


BACKUP_NFS = {
    "storage": "homeserver-backup",
    "type": "nfs",
    "server": "127.0.0.10",
    "export": "/mnt/persistent/proxmox/backup",
    "content": "backup",
    "format": "qcow2",
    "options": "vers=4.2",
    "path": "/mnt/pve/homeserver-backup",
}


def backup_job(state, jid="vm-nightly"):
    return read(state).get("backup_jobs", {}).get(jid)


@pytest.mark.cluster
def test_proxmox_backup_with_a_nas(tmp_path):
    """With a NAS and a cluster: one nightly job of both nodes to the NAS's backup storage. A dry run on a fresh
    host passes, the storage pending; a real run without the storage is refused; drift is corrected."""
    state = tmp_path / "pve.json"
    extra = {
        "fake_pve_state": str(state),
        "truenas_static_address": "127.0.0.10/24",
        "kubernetes_node_addresses": NODE_ADDRESSES,
    }

    result = run("proxmox_backup.yml", extra, check=True)
    expect(result, "homeserver", changed=1, note="dry run on a fresh host")
    assert re.search(r'created by proxmox_storage on a real run"?:\s*\[\s*"homeserver-backup"', result[2]), result[2][
        -3000:
    ]
    assert re.search(r'vm-nightly"?:\s*"?create', result[2]), result[2][-3000:]
    assert not state.exists() or read(state)["mutations"] == 0, "the dry run changed the host"

    rc, _, out = run("proxmox_backup.yml", extra)
    assert rc != 0 and "homeserver-backup: does not exist" in out, out[-3000:]
    assert backup_job(state) is None

    seed_storage(state, BACKUP_NFS)
    expect(run("proxmox_backup.yml", extra), "homeserver", changed=1, note="first run")
    result = run("proxmox_backup.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run (changed: {changed_tasks(result[2])})")
    expect(run("proxmox_backup.yml", extra, check=True), "homeserver", changed=0, note="dry run after applying")
    job = backup_job(state)
    assert {k: job[k] for k in ("storage", "vmid", "schedule", "mode", "compress", "prune-backups")} == {
        "storage": "homeserver-backup",
        "vmid": "300,301",
        "schedule": "02:00",
        "mode": "snapshot",
        "compress": "zstd",
        "prune-backups": "keep-daily=7,keep-weekly=4",
    }
    # Proxmox's own placeholder, untouched by Ansible's templating.
    assert job["notes-template"] == "{{guestname}}"

    # Home Assistant's VM joins the job once local.yml gives its MAC, once even when also listed as an extra VM.
    haos = extra | {"haos_mac": mac(9), "vm_backup_extra_vmids": [310]}
    expect(run("proxmox_backup.yml", haos), "homeserver", changed=1, note="Home Assistant's VM added")
    assert backup_job(state)["vmid"] == "300,301,310"
    expect(run("proxmox_backup.yml", haos), "homeserver", changed=0, note="Home Assistant's VM added, again")
    expect(run("proxmox_backup.yml", extra), "homeserver", changed=1, note="Home Assistant's VM taken away")

    # Drift: the schedule and the VMIDs changed by hand, the job disabled. VMIDs in another order are no change.
    s = read(state)
    s["backup_jobs"]["vm-nightly"].update(schedule="03:30", vmid="301,300,999", enabled="0")
    write(state, s)
    result = run("proxmox_backup.yml", extra, check=True)
    expect(result, "homeserver", changed=1, note="dry run after drift")
    assert "schedule" in result[2] and "enabled" in result[2], result[2][-3000:]
    expect(run("proxmox_backup.yml", extra), "homeserver", changed=1, note="run after drift")
    expect(run("proxmox_backup.yml", extra), "homeserver", changed=0, note="second run after drift")
    job = backup_job(state)
    assert (job["schedule"], job["vmid"], job["enabled"]) == ("02:00", "300,301", "1")
    s = read(state)
    s["backup_jobs"]["vm-nightly"]["vmid"] = "301,300"
    write(state, s)
    expect(run("proxmox_backup.yml", extra), "homeserver", changed=0, note="VMIDs in another order")

    # Without a cluster, the VMs local.yml names alone: the same job, corrected to them.
    alone = {k: v for k, v in extra.items() if k != "kubernetes_node_addresses"} | {"vm_backup_extra_vmids": [310]}
    expect(run("proxmox_backup.yml", alone), "homeserver", changed=1, note="extra VMs without a cluster")
    assert backup_job(state)["vmid"] == "310"
    expect(run("proxmox_backup.yml", alone), "homeserver", changed=0, note="extra VMs, second run")


@pytest.mark.cluster
def test_proxmox_backup_without_a_nas(tmp_path):
    """Without a NAS: no job and no read without vm_backup_storage; a directory storage that is not a checked
    mount point is refused, naming the fix; then one job, for the cluster's two nodes and the extra VMs."""
    state = tmp_path / "pve.json"
    extra = {
        "fake_pve_state": str(state),
        "truenas_static_address": "",
        "kubernetes_node_addresses": NODE_ADDRESSES,
        "kubernetes_node_sizes": SMALL_SIZES,
    }
    for check in (True, False):
        expect(run("proxmox_backup.yml", extra, check=check), "homeserver", changed=0, note="no backup storage")
    assert not state.exists() or read(state)["mutations"] == 0

    usb = {"storage": "usb-backup", "type": "dir", "path": "/mnt/usb", "content": "backup"}
    seed_storage(state, usb)
    with_usb = {**extra, "vm_backup_storage": "usb-backup", "vm_backup_extra_vmids": [310]}
    for check in (True, False):
        rc, _, out = run("proxmox_backup.yml", with_usb, check=check)
        assert rc != 0 and "usb-backup: is a directory that is not a checked mount point" in out, out[-3000:]
        assert "pvesm set usb-backup --is_mountpoint yes --create-base-path 0" in out, out[-3000:]
    assert backup_job(state) is None

    # "off" is unset to Proxmox too, and a btrfs storage is a directory as much.
    s = read(state)
    next(e for e in s["storage"] if e["storage"] == "usb-backup")["is_mountpoint"] = "off"
    write(state, s)
    rc, _, out = run("proxmox_backup.yml", with_usb, check=True)
    assert rc != 0 and "usb-backup: is a directory that is not a checked mount point" in out, out[-3000:]
    seed_storage(state, {"storage": "usb-btrfs", "type": "btrfs", "path": "/mnt/b", "content": "backup"})
    rc, _, out = run("proxmox_backup.yml", {**with_usb, "vm_backup_storage": "usb-btrfs"}, check=True)
    assert rc != 0 and "usb-btrfs: is a directory that is not a checked mount point" in out, out[-3000:]

    s = read(state)
    next(e for e in s["storage"] if e["storage"] == "usb-backup")["is_mountpoint"] = "yes"
    write(state, s)
    expect(run("proxmox_backup.yml", with_usb, check=True), "homeserver", changed=1, note="dry run, mount point")
    expect(run("proxmox_backup.yml", with_usb), "homeserver", changed=1, note="first run")
    expect(run("proxmox_backup.yml", with_usb), "homeserver", changed=0, note="second run")
    job = backup_job(state)
    assert (job["storage"], job["vmid"], job["notes-template"]) == ("usb-backup", "300,301,310", "{{guestname}}")

    # A storage that holds no backups, and one that does not exist, are refused.
    seed_storage(
        state, {"storage": "usb-iso", "type": "dir", "path": "/mnt/iso", "content": "iso", "is_mountpoint": "1"}
    )
    for storage, message in (
        ("usb-iso", "usb-iso: has no backup in its content"),
        ("nowhere", "nowhere: does not exist"),
    ):
        rc, _, out = run("proxmox_backup.yml", {**with_usb, "vm_backup_storage": storage}, check=True)
        assert rc != 0 and message in out, out[-3000:]


# ---------------------------------------------------------------------------------- Node exporter ---

EXPORTER = "prometheus-node-exporter.service"


def exporter_vars(tmp_path, cluster=True):
    """Variables for the node exporter's test playbook: the fake host's state, a scratch systemd directory, and with a
    cluster its two nodes, the addresses the exporter lets in."""
    extra = {
        "fake_pve_state": str(tmp_path / "pve.json"),
        "proxmox_node_exporter_systemd_dir": str(tmp_path / "systemd"),
        "truenas_static_address": "127.0.0.10/24",
    }
    return extra | {"kubernetes_node_addresses": NODE_ADDRESSES} if cluster else extra


def exporter_dropin(extra):
    """The drop-in that lets only the scrapers and the host itself reach the exporter."""
    return Path(extra["proxmox_node_exporter_systemd_dir"]) / f"{EXPORTER}.d" / "10-allowed-addresses.conf"


def test_proxmox_node_exporter_role_is_idempotent(tmp_path):
    """With a cluster: the exporter installed once, after the drop-in that lets in only the cluster's nodes and the
    host itself is loaded, so it never runs without it; then nothing changes, and drift is corrected once. New
    addresses restart it with them."""
    extra = exporter_vars(tmp_path)
    state = Path(extra["fake_pve_state"])

    result = run("proxmox_node_exporter.yml", extra, check=True)
    expect(result, "homeserver", changed=True, note="dry run")
    assert "Would install prometheus-node-exporter" in result[2], result[2][-3000:]
    assert not state.exists() or read(state)["mutations"] == 0, "the dry run changed the host"
    assert not exporter_dropin(extra).exists()

    expect(run("proxmox_node_exporter.yml", extra), "homeserver", changed=4, note="first run")
    result = run("proxmox_node_exporter.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run (changed: {changed_tasks(result[2])})")
    expect(run("proxmox_node_exporter.yml", extra, check=True), "homeserver", changed=0, note="dry run after applying")

    allowed = "[Service]\nIPAddressDeny=any\nIPAddressAllow=localhost 127.0.0.30 127.0.0.31\n"
    assert exporter_dropin(extra).read_text().endswith(allowed), exporter_dropin(extra).read_text()
    s = read(state)
    assert s["packages"]["installed"]["prometheus-node-exporter"] == fake_pve.DEBIAN["prometheus-node-exporter"]
    # The package's scripts started it, and systemd had the drop-in by then.
    assert s["units"][EXPORTER]["enabled"] and s["units"][EXPORTER]["active"]
    assert s["units"][EXPORTER]["started_with"].endswith(allowed), s["units"][EXPORTER]

    # Drift: stopped and disabled by hand. Enabled and started again, once.
    s["units"][EXPORTER].update(enabled=False, active=False)
    write(state, s)
    expect(run("proxmox_node_exporter.yml", extra), "homeserver", changed=2, note="run after drift")
    expect(run("proxmox_node_exporter.yml", extra), "homeserver", changed=0, note="second run after drift")
    assert read(state)["units"][EXPORTER]["active"] and read(state)["units"][EXPORTER]["enabled"]

    # The worker at another address: the drop-in rewritten, systemd reloaded and the running exporter restarted.
    moved = extra | {"kubernetes_node_addresses": NODE_ADDRESSES | {"homeserver-worker-1": "127.0.0.32"}}
    result = run("proxmox_node_exporter.yml", moved, check=True)
    expect(result, "homeserver", changed=True, note="dry run with a new address")
    assert "Would restart it (its addresses changed)" in result[2], result[2][-3000:]
    expect(run("proxmox_node_exporter.yml", moved), "homeserver", changed=3, note="run with a new address")
    expect(run("proxmox_node_exporter.yml", moved), "homeserver", changed=0, note="second run with a new address")
    assert read(state)["units"][EXPORTER]["started_with"].endswith(allowed.replace("127.0.0.31", "127.0.0.32"))

    # A string for the list is refused before anything runs, naming the variable.
    rc, _, out = run("proxmox_node_exporter.yml", extra | {"proxmox_node_exporter_scrapers": "127.0.0.31"})
    assert rc != 0 and "proxmox_node_exporter_scrapers must be a list, not a string." in out, out[-3000:]


def test_proxmox_node_exporter_without_a_cluster(tmp_path):
    """Without a cluster there is no Prometheus to serve: nothing is installed, written or run."""
    extra = exporter_vars(tmp_path, cluster=False)
    state = Path(extra["fake_pve_state"])
    for check in (True, False):
        expect(run("proxmox_node_exporter.yml", extra, check=check), "homeserver", changed=0, note="no cluster")
    assert not state.exists() and not exporter_dropin(extra).exists()


# --------------------------------------------------------------------------------------- The cluster's VMs ---


def mac(number):
    """A locally administered MAC, built here because gitleaks flags a MAC written out in any tracked file."""
    return ":".join(["02", "00", "00", "00", "00", f"{number:02x}"])


NODE_MACS = {"homeserver-cp-1": mac(1), "homeserver-worker-1": mac(2)}
PERSISTENT_NFS = {
    "storage": "homeserver-persistent",
    "type": "nfs",
    "server": "127.0.0.10",
    "export": "/mnt/persistent/proxmox/vm",
    "content": "images",
    "format": "qcow2",
    "options": "vers=4.2",
    "path": "/mnt/pve/homeserver-persistent",
}
# Where the worker's system disk goes with a NAS.
EPHEMERAL_NFS = PERSISTENT_NFS | {
    "storage": "homeserver-ephemeral",
    "export": "/mnt/ephemeral/proxmox/vm",
    "path": "/mnt/pve/homeserver-ephemeral",
}
ISO = "local:iso/talos-v1.14.2-ce4c9805-metal-amd64.iso"


def vm_state(state, local_lvm_gib=500, **more):
    """A fake Proxmox host as the Proxmox play leaves it, with local-lvm of the given size."""
    s = fake_pve.fresh_state()
    s["storage_total"] = {"local-lvm": local_lvm_gib * 2**30}
    s.update(more)
    write(state, s)


def vms_of(state):
    return read(state).get("vms", {})


def restart_all(state):
    """Every VM restarted by hand: what waited for a restart is applied, a None removing its setting."""
    s = read(state)
    for vm in s["vms"].values():
        for key, value in vm["pending"].items():
            if value is None:
                vm["config"].pop(key, None)
            else:
                vm["config"][key] = value
        vm["pending"] = {}
    write(state, s)


def fake_pve_command(state, *argv):
    """A command run on the fake Proxmox host by hand, as the owner would: qm set 301 --delete scsi0, say."""
    env = {**os.environ, "FAKE_PVE_STATE": str(state), "FAKE_PVE_PYTHON": sys.executable}
    return subprocess.run([HERE / "fake_pve_bin" / argv[0], *argv[1:]], env=env, capture_output=True, text=True)


def cluster_vars(tmp_path, nas=True, **more):
    """The kubernetes test playbook's variables: the same two nodes in both setups; without a NAS, sized for a small
    host, the worker with a media disk."""
    extra = {
        "fake_pve_state": str(tmp_path / "pve.json"),
        "truenas_static_address": "127.0.0.10/24" if nas else "",
        "kubernetes_node_addresses": NODE_ADDRESSES,
        "kubernetes_node_macs": NODE_MACS,
    }
    if not nas:
        extra["kubernetes_node_sizes"] = SMALL_SIZES
    return extra | more


@pytest.mark.cluster
def test_proxmox_vm_with_a_nas(tmp_path):
    """With a NAS: a dry run on a fresh host passes, the NAS's storages pending; a real run without them is refused;
    then both VMs are created, every disk on the NAS but the EFI disks, started and left alone; drift is corrected; a
    shrink is refused."""
    extra = cluster_vars(tmp_path)
    state = Path(extra["fake_pve_state"])
    vm_state(state)

    result = run("kubernetes.yml", extra, check=True)
    expect(result, "homeserver", changed=True, note="dry run on a fresh host")
    out = result[2]
    assert re.search(
        r'storage pending: just deploy proxmox adds it: run that first"?:\s*\[\s*"homeserver-ephemeral",'
        r'\s*"homeserver-persistent"',
        out,
    ), out[-3000:]
    assert re.search(r'homeserver-worker-1"?:\s*"?create', out) and "Would download: talos-v1.14.2" in out, out[-3000:]
    assert read(state)["mutations"] == 0, "the dry run changed the host"

    rc, _, out = run("kubernetes.yml", extra)
    assert rc != 0 and "homeserver-persistent: does not exist (just deploy proxmox adds it: run that first)" in out, (
        out[-3000:]
    )
    assert vms_of(state) == {}

    seed_storage(state, PERSISTENT_NFS, EPHEMERAL_NFS)
    expect(run("kubernetes.yml", extra), "homeserver", changed=True, note="first run")
    result = run("kubernetes.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run (changed: {changed_tasks(result[2])})")
    expect(run("kubernetes.yml", extra, check=True), "homeserver", changed=0, note="dry run after applying")
    assert f'"homeserver-worker-1": "{mac(2).upper()}"' in result[2], "the MACs are not reported"

    vms = vms_of(state)
    assert sorted(vms) == ["300", "301"] and all(vm["status"] == "running" for vm in vms.values())
    worker = vms["301"]["config"]
    assert worker["efidisk0"].startswith("local-lvm:vm-301-disk-0,"), worker["efidisk0"]
    assert worker["scsi0"].startswith("homeserver-ephemeral:301/vm-301-disk-0.qcow2,") and "size=64G" in worker["scsi0"]
    control_plane = vms["300"]["config"]
    assert control_plane["efidisk0"].startswith("local-lvm:vm-300-disk-0,"), control_plane["efidisk0"]
    assert control_plane["scsi0"].startswith("homeserver-persistent:300/vm-300-disk-0.qcow2,"), control_plane["scsi0"]
    assert (
        worker["scsi1"].startswith("homeserver-persistent:301/vm-301-disk-0.qcow2,") and "size=200G" in worker["scsi1"]
    )
    assert "serial=talos-data" in worker["scsi1"] and "backup=0" not in worker["scsi1"]
    assert (worker["ide2"], worker["tags"], worker["boot"]) == (f"{ISO},media=cdrom", "k8s;talos", "order=scsi0;ide2")
    assert worker["watchdog"] == "model=i6300esb,action=reset" and worker["startup"].startswith("order=2,")
    assert worker["net0"].lower().startswith(f"virtio={mac(2)},")
    assert vms["300"]["config"]["startup"].startswith("order=1,") and "scsi1" not in vms["300"]["config"]
    isos = [v["volid"] for v in read(state)["content"]["local"]]
    assert isos == [ISO], "the ISO was not downloaded once"
    # Proxmox checks the download against the pinned SHA-256.
    download = read(state)["downloads"][0]
    assert download["checksum-algorithm"] == "sha256" and re.fullmatch(r"[0-9a-f]{64}", download["checksum"])

    # Drift: cores changed by hand on the running control plane, tags in another order and case. The cores go back,
    # pending until a restart, which every run reports and none makes; the tags are no change.
    s = read(state)
    s["vms"]["300"]["config"].update(cores="4", tags="talos;K8S")
    write(state, s)
    result = run("kubernetes.yml", extra)
    expect(result, "homeserver", changed=1, note="run after drift")
    assert re.search(r'needs a restart[^"]*"?:\s*\{\s*"300":\s*\[\s*"cores"', result[2]), result[2][-3000:]
    result = run("kubernetes.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run after drift (changed: {changed_tasks(result[2])})")
    assert re.search(r'"300":\s*\[\s*"cores"', result[2]), "the pending restart is no longer reported"
    assert vms_of(state)["300"]["status"] == "running", "the role restarted a VM"
    s = read(state)
    s["vms"]["300"]["config"] |= s["vms"]["300"]["pending"]
    s["vms"]["300"]["pending"] = {}
    write(state, s)
    result = run("kubernetes.yml", extra)
    expect(result, "homeserver", changed=0, note="run after the restart")
    assert "needs a restart" not in result[2]

    # The data disk's serial changed by hand: set again on the same volume, its size and contents untouched.
    s = read(state)
    s["vms"]["301"]["config"]["scsi1"] = s["vms"]["301"]["config"]["scsi1"].replace("talos-data", "other")
    write(state, s)
    expect(run("kubernetes.yml", extra), "homeserver", changed=1, note="a disk's serial changed by hand")
    data = vms_of(state)["301"]["config"]["scsi1"]
    assert data.startswith("homeserver-persistent:301/vm-301-disk-0.qcow2,") and "serial=talos-data" in data, data
    assert "size=200G" in data, data
    expect(run("kubernetes.yml", extra), "homeserver", changed=0, note="a disk's serial set again")

    # The worker's system disk replaced by hand (scripts/runbooks/dead-worker.md): Proxmox drops the deleted drive
    # from the boot order and never adds the new one back, so the role adds the disk and puts the order back,
    # pending until a restart.
    assert fake_pve_command(state, "qm", "set", "301", "--delete", "scsi0").returncode == 0
    assert vms_of(state)["301"]["config"]["boot"] == "order=ide2"
    result = run("kubernetes.yml", extra)
    expect(result, "homeserver", changed=1, note="a system disk replaced by hand")
    worker = vms_of(state)["301"]
    assert worker["pending"]["boot"] == "order=scsi0;ide2"
    assert worker["config"]["scsi0"].startswith("homeserver-ephemeral:"), worker["config"]["scsi0"]
    assert re.search(r'"301":\s*\[[^\]]*"boot"', result[2]), result[2][-3000:]
    expect(run("kubernetes.yml", extra), "homeserver", changed=0, note="a system disk replaced, again")

    # The iGPU passed to the running worker: the PCI device waits for a restart, and is reported until then.
    s = read(state)
    s["pci_mappings"] = ["igpu"]
    write(state, s)
    gpu = extra | {"kubernetes_gpu_mapping": "igpu"}
    result = run("kubernetes.yml", gpu)
    expect(result, "homeserver", changed=1, note="the iGPU for the running worker")
    assert vms_of(state)["301"]["pending"]["hostpci0"] == "mapping=igpu,pcie=1" and "hostpci0" not in str(
        vms_of(state)["300"]
    )
    assert re.search(r'"301":\s*\[[^\]]*"hostpci0"', result[2]), result[2][-3000:]
    expect(run("kubernetes.yml", gpu), "homeserver", changed=0, note="the iGPU, again")
    restart_all(state)
    # Taken away again: removed at the next restart too.
    expect(run("kubernetes.yml", extra), "homeserver", changed=1, note="the iGPU taken away")
    restart_all(state)
    assert "hostpci0" not in vms_of(state)["301"]["config"]
    expect(run("kubernetes.yml", extra), "homeserver", changed=0, note="the iGPU taken away, again")

    # A disk configured larger is grown; one configured smaller is refused, before anything changes.
    grown = extra | {"kubernetes_node_sizes": {"homeserver-worker-1": {"system_disk_gib": 80}}}
    expect(run("kubernetes.yml", grown), "homeserver", changed=1, note="a larger system disk")
    assert "size=80G" in vms_of(state)["301"]["config"]["scsi0"]
    expect(run("kubernetes.yml", grown), "homeserver", changed=0, note="a larger system disk, again")
    shrunk = extra | {"kubernetes_node_sizes": {"homeserver-worker-1": {"system_disk_gib": 32}}}
    mutations = read(state)["mutations"]
    for check in (True, False):
        rc, _, out = run("kubernetes.yml", shrunk, check=check)
        assert rc != 0 and "scsi0 is 80 GiB, configured as 32 GiB: a disk is never shrunk" in out, out[-3000:]
    assert read(state)["mutations"] == mutations

    # A setup without a cluster creates nothing and downloads nothing, and reads nothing.
    without = {k: v for k, v in extra.items() if not k.startswith("kubernetes_")}
    result = run("kubernetes.yml", without)
    expect(result, "homeserver", changed=0, note="no cluster")
    assert re.search(r"Read the node's VMs\] \*+\nskipping:", result[2]), result[2][-3000:]


@pytest.mark.cluster
def test_proxmox_vm_without_a_nas(tmp_path):
    """Without a NAS: the same two VMs, the worker's data disk on local-lvm and its media disk never backed up; the
    disk budget counts both VMs' disks and other guests', a container's (listed as rootdir) as much as a VM's, and
    refuses a plan that would overfill the thin pool, until told otherwise."""
    extra = cluster_vars(tmp_path, nas=False)
    state = Path(extra["fake_pve_state"])
    # An 80 GiB pool holding another VM's 40 GiB disk: the 64 GiB more the two nodes need do not fit.
    vm_state(state, local_lvm_gib=80)
    other = {"volid": "local-lvm:vm-200-disk-0", "content": "images", "vmid": 200, "size": 40 * 2**30, "format": "raw"}
    s = read(state)
    s["content"] = {"local-lvm": [other]}
    write(state, s)
    for check in (True, False):
        rc, _, out = run("kubernetes.yml", extra, check=check)
        assert (
            rc != 0 and "local-lvm: its disks would be provisioned at 104 GiB (40 GiB there now, 64 GiB more" in out
        ), out[-3000:]
        assert "more than its 80 GiB" in out, out[-3000:]
    assert vms_of(state) == {}
    expect(
        run("kubernetes.yml", extra | {"proxmox_vm_allow_overcommit": True}, check=True),
        "homeserver",
        changed=True,
        note="overcommit allowed",
    )
    container = other | {"volid": "local-lvm:vm-201-disk-0", "content": "rootdir", "vmid": 201}
    s = read(state)
    s["content"] = {"local-lvm": [container]}
    write(state, s)
    rc, _, out = run("kubernetes.yml", extra, check=True)
    assert rc != 0 and "provisioned at 104 GiB (40 GiB there now, 64 GiB more" in out, out[-3000:]

    # Without the other VM's disk it fits, and still fits on the next run, its own disks counted once.
    s = read(state)
    s["content"] = {}
    write(state, s)
    expect(run("kubernetes.yml", extra), "homeserver", changed=True, note="first run")
    result = run("kubernetes.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run (changed: {changed_tasks(result[2])})")
    expect(run("kubernetes.yml", extra, check=True), "homeserver", changed=0, note="dry run after applying")
    vms = vms_of(state)
    assert sorted(vms) == ["300", "301"] and all(vm["config"]["tags"] == "k8s;talos" for vm in vms.values())
    assert [k for k in sorted(vms["300"]["config"]) if re.fullmatch(r"scsi\d+", k)] == ["scsi0"]
    config = vms["301"]["config"]
    assert config["scsi1"].startswith("local-lvm:vm-301-disk-2,") and "serial=talos-data" in config["scsi1"]
    assert config["scsi2"].startswith("local-lvm:vm-301-disk-3,") and "backup=0" in config["scsi2"]
    assert "format=" not in config["scsi1"]

    # Drift on the worker: its memory changed by hand is put back, pending until a restart, which the run reports and
    # never makes.
    s = read(state)
    s["vms"]["301"]["config"]["memory"] = "2048"
    write(state, s)
    result = run("kubernetes.yml", extra)
    expect(result, "homeserver", changed=1, note="run after drift")
    assert re.search(r'needs a restart[^"]*"?:\s*\{\s*"301":\s*\[\s*"memory"', result[2]), result[2][-3000:]
    expect(run("kubernetes.yml", extra), "homeserver", changed=0, note="second run after drift")
    restart_all(state)

    # Growing a disk counts against the pool as well: 64 GiB there, 19 GiB more does not fit in 80.
    worker = SMALL_SIZES["homeserver-worker-1"] | {"data_disk_gib": 30}
    grown = extra | {"kubernetes_node_sizes": SMALL_SIZES | {"homeserver-worker-1": worker}}
    mutations = read(state)["mutations"]
    for check in (True, False):
        rc, _, out = run("kubernetes.yml", grown, check=check)
        assert rc != 0 and "provisioned at 83 GiB (64 GiB there now, 19 GiB more" in out, out[-3000:]
    assert read(state)["mutations"] == mutations

    # The VMs' disk storage disabled: refused, naming it.
    s = read(state)
    next(e for e in s["storage"] if e["storage"] == "local-lvm")["disable"] = 1
    write(state, s)
    rc, _, out = run("kubernetes.yml", extra, check=True)
    assert rc != 0 and "local-lvm: is disabled (proxmox_vm_vms)" in out, out[-3000:]


@pytest.mark.cluster
def test_proxmox_vm_moves_disks_only_when_told(tmp_path):
    """A cluster whose system disks are on the installer's storage, as it was built with a NAS before 2026-10-25: the
    play refuses each disk on another storage than configured, in a dry run too, naming the command, and changes
    nothing. Told to, a dry run lists the moves and makes none; a real run moves both, the VMs running, into qcow2 on
    the NAS, deleting the old volumes, and leaves the EFI disks where they are. A second run changes nothing."""
    extra = cluster_vars(tmp_path)
    state = Path(extra["fake_pve_state"])
    vm_state(state)
    seed_storage(state, PERSISTENT_NFS, EPHEMERAL_NFS)
    expect(run("kubernetes.yml", extra), "homeserver", changed=True, note="first run")
    for vmid in ("300", "301"):
        by_hand = fake_pve_command(state, "qm", "disk", "move", vmid, "scsi0", "local-lvm", "--delete", "1")
        assert by_hand.returncode == 0, by_hand.stderr
    mutations = read(state)["mutations"]

    for check in (True, False):
        rc, _, out = run("kubernetes.yml", extra, check=check)
        assert rc != 0, out[-3000:]
        for vmid, storage in (("300", "homeserver-persistent"), ("301", "homeserver-ephemeral")):
            assert f"VM {vmid}'s scsi0 is on local-lvm, configured on {storage}. The role moves a disk only" in out, (
                out[-3000:]
            )
        assert "run just deploy kubernetes -e proxmox_vm_move_disks=true" in out, out[-3000:]
    assert read(state)["mutations"] == mutations, "the refused run changed the host"

    told = extra | {"proxmox_vm_move_disks": True}
    result = run("kubernetes.yml", told, check=True)
    expect(result, "homeserver", changed=True, note="dry run of the moves")
    assert re.search(r'"homeserver-cp-1":\s*\[\s*"move scsi0 to homeserver-persistent"', result[2]), result[2][-3000:]
    assert re.search(r'"homeserver-worker-1":\s*\[\s*"move scsi0 to homeserver-ephemeral"', result[2]), result[2][
        -3000:
    ]
    assert read(state)["mutations"] == mutations, "the dry run changed the host"

    expect(run("kubernetes.yml", told), "homeserver", changed=1, note="the moves")
    vms = vms_of(state)
    for vmid, storage, size in (("300", "homeserver-persistent", 32), ("301", "homeserver-ephemeral", 64)):
        disk = vms[vmid]["config"]["scsi0"]
        assert disk.startswith(f"{storage}:{vmid}/") and ".qcow2," in disk, disk
        assert "serial=talos-system" in disk and f"size={size}G" in disk, disk
        assert vms[vmid]["status"] == "running" and not any(k.startswith("unused") for k in vms[vmid]["config"])
    assert [m["running"] for m in read(state)["moves"][2:]] == [True, True], "a VM was not running when moved"
    assert sorted(v["volid"] for v in read(state)["content"]["local-lvm"]) == [
        "local-lvm:vm-300-disk-0",
        "local-lvm:vm-301-disk-0",
    ], "only the EFI disks stay on local-lvm"

    result = run("kubernetes.yml", told)
    expect(result, "homeserver", changed=0, note=f"second run (changed: {changed_tasks(result[2])})")
    expect(run("kubernetes.yml", extra, check=True), "homeserver", changed=0, note="dry run without the switch")


@pytest.mark.cluster
@pytest.mark.parametrize("built_with_nas", [True, False], ids=["nas-to-local", "local-to-nas"])
def test_a_built_cluster_cannot_switch_setups(tmp_path, built_with_nas):
    """Both setups build the same two VMs, their disks on other storages. With local.yml rewritten for the other
    setup, the cluster's play refuses before changing anything, in a dry run too, because the disks are on the other
    storages, naming the command that moves them. The backup job keeps both VMs either way. Put back, nothing
    changes."""
    built, switched = cluster_vars(tmp_path, nas=built_with_nas), cluster_vars(tmp_path, nas=not built_with_nas)
    state = Path(built["fake_pve_state"])
    vm_state(state)
    usb_storage = {
        "storage": "usb-backup",
        "type": "dir",
        "path": "/mnt/usb",
        "content": "backup",
        "is_mountpoint": "1",
    }
    seed_storage(state, PERSISTENT_NFS, EPHEMERAL_NFS, BACKUP_NFS, usb_storage)
    usb = {"vm_backup_storage": "usb-backup"}
    expect(run("kubernetes.yml", built), "homeserver", changed=True, note="build")
    expect(run("proxmox_backup.yml", built | usb), "homeserver", changed=1, note="backup job")
    mutations = read(state)["mutations"]
    assert backup_job(state)["vmid"] == "300,301"

    on, wanted = ("homeserver-persistent", "local-lvm") if built_with_nas else ("local-lvm", "homeserver-persistent")
    for check in (True, False):
        rc, _, out = run("kubernetes.yml", switched | usb, check=check)
        assert rc != 0 and f"VM 301's scsi1 is on {on}, configured on {wanted}. The role moves a disk" in out, out[
            -3000:
        ]
        assert "run just deploy kubernetes -e proxmox_vm_move_disks=true" in out, out[-3000:]
    assert read(state)["mutations"] == mutations, "the refused switch changed the host"
    expect(run("proxmox_backup.yml", switched | usb), "homeserver", changed=1, note="backup job after the switch")
    assert backup_job(state)["vmid"] == "300,301", "the switch dropped a VM from the backup"

    expect(run("kubernetes.yml", built), "homeserver", changed=0, note="local.yml put back")
    expect(run("proxmox_backup.yml", built | usb), "homeserver", changed=1, note="local.yml put back, backup")


@pytest.mark.cluster
def test_proxmox_vm_refuses_to_build_over_old_volumes(tmp_path):
    """A missing VM whose VMID still has a disk or a backup is being rebuilt: refused, naming the way back, until told
    to build it new, when its new disks take the next free names. A VMID another VM holds is refused."""
    extra = cluster_vars(tmp_path)
    state = Path(extra["fake_pve_state"])
    vm_state(state)
    seed_storage(state, PERSISTENT_NFS, EPHEMERAL_NFS)
    s = read(state)
    s["content"] = {
        "local-lvm": [{"volid": "local-lvm:vm-301-disk-0", "content": "images", "vmid": 301, "size": 2**30}],
        "local": [
            {"volid": "local:backup/vzdump-qemu-300-2026_10_01-02_00_00.vma.zst", "content": "backup", "vmid": 300},
            # An upload for import belongs to no VMID, and Proxmox lists it whatever --vmid asks for.
            {"volid": "local:import/appliance.ova", "content": "import"},
        ],
    }
    write(state, s)
    for check in (True, False):
        rc, _, out = run("kubernetes.yml", extra, check=check)
        assert rc != 0 and "VM 301 (homeserver-worker-1) does not exist, but its VMID still has volumes" in out, out[
            -3000:
        ]
        assert "VM 300 (homeserver-cp-1) does not exist" in out and "qmrestore" in out, out[-3000:]
        assert "scripts/runbooks/vm-restore.md" in out, out[-3000:]
        assert "run just deploy kubernetes -e proxmox_vm_ignore_existing_volumes=true" in out, out[-3000:]
        assert "appliance.ova" not in out
    assert vms_of(state) == {}

    # Only an import file left: not a rebuild.
    s = read(state)
    s["content"] = {"local": [{"volid": "local:import/appliance.ova", "content": "import"}]}
    write(state, s)
    expect(run("kubernetes.yml", extra, check=True), "homeserver", changed=True, note="an import file only")

    # A storage that is enabled but not active, as an unplugged USB backup disk is, cannot be ruled out.
    seed_storage(state, {"storage": "usb-backup", "type": "dir", "path": "/mnt/usb", "content": "backup"})
    s = read(state)
    s["inactive"] = ["usb-backup"]
    write(state, s)
    rc, _, out = run("kubernetes.yml", extra, check=True)
    assert rc != 0 and "usb-backup cannot be searched for its old disks and backups" in out, out[-3000:]
    s = read(state)
    s["inactive"] = []
    s["content"] = {
        "local-lvm": [{"volid": "local-lvm:vm-301-disk-0", "content": "images", "vmid": 301, "size": 2**30}],
        "local": [
            {"volid": "local:backup/vzdump-qemu-300-2026_10_01-02_00_00.vma.zst", "content": "backup", "vmid": 300}
        ],
    }
    write(state, s)

    rebuild = extra | {"proxmox_vm_ignore_existing_volumes": True}
    expect(run("kubernetes.yml", rebuild), "homeserver", changed=True, note="a deliberate rebuild")
    worker = vms_of(state)["301"]["config"]
    assert worker["efidisk0"].startswith("local-lvm:vm-301-disk-1,"), worker["efidisk0"]
    assert worker["scsi0"].startswith("homeserver-ephemeral:301/vm-301-disk-0.qcow2,"), worker["scsi0"]

    # VMID 300 now belongs to another VM, and 301 to a container.
    s = read(state)
    s["vms"]["300"]["config"]["name"] = "someone-else"
    del s["vms"]["301"]
    s["containers"] = [{"vmid": 301, "name": "pihole", "status": "running"}]
    write(state, s)
    rc, _, out = run("kubernetes.yml", extra, check=True)
    assert rc != 0 and "VMID 300 belongs to 'someone-else', not 'homeserver-cp-1'" in out, out[-3000:]
    assert "VMID 301 belongs to the container 'pihole', not to a VM named 'homeserver-worker-1'" in out, out[-3000:]


@pytest.mark.cluster
def test_proxmox_vm_starts_vms_restored_after_a_host_reinstall(tmp_path):
    """After a reinstall the VMs come back from their backups, stopped, their CD-ROM naming a Talos ISO that went with
    the boot disk, so starting them by hand fails (scripts/runbooks/vm-restore.md). just deploy kubernetes
    downloads the ISO again and then starts them, creating nothing."""
    extra = cluster_vars(tmp_path)
    state = Path(extra["fake_pve_state"])
    vm_state(state)
    seed_storage(state, PERSISTENT_NFS, EPHEMERAL_NFS)
    expect(run("kubernetes.yml", extra), "homeserver", changed=True, note="first run")
    s = read(state)
    s["content"]["local"] = []
    for vm in s["vms"].values():
        vm["status"] = "stopped"
    write(state, s)
    by_hand = fake_pve_command(state, "qm", "start", "300")
    assert by_hand.returncode != 0 and f"volume '{ISO}' does not exist" in by_hand.stderr, by_hand.stderr

    result = run("kubernetes.yml", extra)
    expect(result, "homeserver", changed=True, note="run after the restore")
    assert [v["volid"] for v in read(state)["content"]["local"]] == [ISO]
    assert sorted(vms_of(state)) == ["300", "301"] and all(vm["status"] == "running" for vm in vms_of(state).values())
    expect(run("kubernetes.yml", extra), "homeserver", changed=0, note="second run after the restore")


@pytest.mark.cluster
def test_proxmox_vm_recovers_from_a_failed_download_and_start(tmp_path):
    """A download that fails is reported, though pvesh exits 0, and nothing is created; the next run downloads it. A
    start that fails is reported; the next run starts the VM without creating it again. A node short of memory gets
    a warning, and the run goes on."""
    extra = cluster_vars(tmp_path)
    state = Path(extra["fake_pve_state"])
    vm_state(state, download_fails=True)
    seed_storage(state, PERSISTENT_NFS, EPHEMERAL_NFS)
    rc, _, out = run("kubernetes.yml", extra)
    assert rc != 0 and "Downloading talos-v1.14.2-ce4c9805-metal-amd64.iso" in out and "404 Not Found" in out, out[
        -3000:
    ]
    assert vms_of(state) == {} and read(state).get("content", {}).get("local", []) == []

    s = read(state)
    s.update(download_fails=False, start_fails=True, node_memory={"total": 16 * 2**30, "used": 2**30})
    write(state, s)
    rc, _, out = run("kubernetes.yml", extra)
    assert rc != 0 and "start failed" in out, out[-3000:]
    assert "The VMs would need 28 GiB of memory, with 16 GiB on the node" in out, out[-3000:]
    assert sorted(vms_of(state)) == ["300", "301"]
    assert [vm["status"] for vm in vms_of(state).values()].count("stopped") == 1, "not just the failed start is left"
    created = read(state)["mutations"]

    result = run("kubernetes.yml", extra)
    expect(result, "homeserver", changed=True, note="run after the failed start")
    assert all(vm["status"] == "running" for vm in vms_of(state).values())
    assert read(state)["mutations"] - created == 1, "more than the start happened"
    expect(run("kubernetes.yml", extra), "homeserver", changed=0, note="second run after the failed start")


# ------------------------------------------------------------------------------------------------ Talos ---

# Sentinels that must never reach Ansible's output: the secrets bundle's cluster secret, which the fake carries into
# every configuration it renders and echoes in its diffs.
TALOS_SENTINEL = "sentinel-in-the-talos-bundle"
TALOS_SECRETS = (
    f"certs:\n  os: {{crt: {base64.b64encode(b'TEST-OS-CA').decode()}, key: y}}\n"
    f"  k8s: {{crt: {base64.b64encode(b'TEST-K8S-CA').decode()}, key: z}}\n"
    f"cluster:\n  id: test\n  secret: {TALOS_SENTINEL}\n"
)


@pytest.fixture(scope="module")
def factory():
    """The Image Factory: POST /schematics answers 201 Created with the body's sha256 as the id, as the real one
    does (a schematic's id is the hash of its content)."""
    posted = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path != "/schematics":
                self.send_error(404)
                return
            posted.append(body.decode())
            reply = json.dumps({"id": hashlib.sha256(body).hexdigest()}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{server.server_address[1]}", "posted": posted}
    server.shutdown()


# The schematics of group_vars/kubernetes/talos.yml, and the digest the tests pin every installer image to.
SCHEMATICS = {"base": ["siderolabs/qemu-guest-agent"], "gpu": ["siderolabs/qemu-guest-agent", "siderolabs/i915"]}
INSTALLER_DIGEST = "sha256:" + "0" * 63 + "2"


def schematic_id(extensions):
    """The id the factory fixture gives a schematic, computed as the role's request makes it."""
    body = yaml.dump(
        {"customization": {"systemExtensions": {"officialExtensions": extensions}}},
        Dumper=yaml.SafeDumper,
        default_flow_style=False,
        indent=4,
        allow_unicode=True,
    )
    return hashlib.sha256(body.encode()).hexdigest()


def bundle_file(path, text=TALOS_SECRETS):
    """A secrets bundle in a file of its own, mode 0600, as talosctl gen secrets writes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return str(path)


def talos_vars(tmp_path, factory, nas=True, **more):
    """The talos test playbook's variables, isolated in tmp_path: its HOME, talos_config_dir and secrets bundle are
    the test's own, never the owner's. The bundle lies outside the test's home, so it is not among what a run left
    there."""
    extra = {
        "fake_talos_state": str(tmp_path / "talos.json"),
        "talos_config_dir": str(tmp_path / "home" / ".talos" / "homeserver"),
        "talos_secrets_file": bundle_file(tmp_path / "talos-secrets.yaml"),
        "talos_image_factory_url": factory["url"],
        "talos_probe_delay": 0,
        "talos_probe_retries": 3,
        "talos_install_retries": 5,
        # The fake factory's ids, pinned, and a digest of no real image for each installer.
        "talos_schematic_pins": {name: schematic_id(extensions) for name, extensions in SCHEMATICS.items()},
        "talos_installer_digests": {
            f"{schematic_id(extensions)}:{version}": INSTALLER_DIGEST
            for extensions in SCHEMATICS.values()
            for version in ("v1.14.2", "v1.14.3")
        },
        "truenas_static_address": "127.0.0.10/24" if nas else "",
        "kubernetes_node_addresses": NODE_ADDRESSES,
    }
    if not nas:
        extra["kubernetes_node_sizes"] = SMALL_SIZES
    return extra | more


def talos_env(tmp_path, client="v1.14.9"):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return {"HOME": str(home), "FAKE_TALOSCTL_CLIENT_TAG": client}


def talos_state(path, mode="maintenance", **more):
    """The fake nodes, the same two in both setups: booted from the v1.14.2 ISO into maintenance mode, unless told
    otherwise."""
    nodes = {a: {"mode": mode, "version": "v1.14.2", "schematic": "iso"} for a in NODE_ADDRESSES.values()}
    write(path, {"nodes": nodes, "mutations": 0, **more})


def talos_files(tmp_path):
    """Everything the run left under the test's home."""
    return sorted(str(p.relative_to(tmp_path)) for p in (tmp_path / "home").rglob("*") if p.is_file())


def no_talos_secret(outputs):
    for out in outputs:
        assert TALOS_SENTINEL not in out, "the Talos secrets reached Ansible's output"


@pytest.mark.cluster
def test_talos_with_a_nas(tmp_path, factory):
    """Two nodes in maintenance mode are installed, the cluster bootstrapped once, health-checked, and talosconfig and
    kubeconfig written under the test's home; later runs change nothing; drift is applied without a reboot, as
    Talos 1.14 applies anything, with a word about etcd when the control plane's changed, and an apply that fails
    stops the run with its error; a pin bump upgrades nothing and is reported. The secrets never show."""
    extra, env = talos_vars(tmp_path, factory), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    talos_state(state)
    outputs = []

    result = run("talos.yml", extra, check=True, env=env)
    outputs.append(result[2])
    expect(result, "homeserver-k8s", changed=True, note="dry run")
    assert "Would install homeserver-worker-1 at 127.0.0.31" in result[2] and "Would bootstrap etcd" in result[2]
    assert read(state)["mutations"] == 0 and talos_files(tmp_path) == [], "the dry run changed something"

    result = run("talos.yml", extra, env=env)
    outputs.append(result[2])
    expect(result, "homeserver-k8s", changed=True, note="first run")
    s = read(state)
    assert s["bootstrapped"] and s.get("bootstrap_calls") == 1
    assert all(n["mode"] == "configured" for n in s["nodes"].values())
    base = schematic_id(["siderolabs/qemu-guest-agent"])
    assert all(n["schematic"] == base and n["version"] == "v1.14.2" for n in s["nodes"].values())
    cp, worker = s["nodes"]["127.0.0.30"]["config"], s["nodes"]["127.0.0.31"]["config"]
    assert "# type: controlplane" in cp and "listen-metrics-urls" in cp and "$patch: delete" not in cp
    assert "# type: worker" in worker and "name: local-path-provisioner" in worker and "_drive-scsi1" in worker
    assert "hostname: homeserver-worker-1" in worker and "kind: WatchdogTimerConfig" in worker
    # Each node installs its schematic's installer at the pinned digest.
    assert f"/metal-installer/{base}:v1.14.2@{INSTALLER_DIGEST}" in worker
    # The firewall's rules for the pod network name the one talosctl gave, which the role asked it for.
    assert "kind: NetworkDefaultActionConfig\ningress: block" in worker
    assert '{"subnet": "198.51.100.0/24"}' in worker and '{"subnet": "198.51.100.0/24"}' in cp
    assert talos_files(tmp_path) == [
        "home/.talos/homeserver/bootstrapped",
        "home/.talos/homeserver/kubeconfig",
        "home/.talos/homeserver/talosconfig",
    ]
    config_dir = Path(extra["talos_config_dir"])
    assert oct(config_dir.stat().st_mode & 0o777) == "0o700"
    assert all(oct(f.stat().st_mode & 0o777) == "0o600" for f in config_dir.iterdir())
    talosconfig = yaml_load(config_dir / "talosconfig")
    assert talosconfig["contexts"]["homeserver"]["endpoints"] == ["127.0.0.30"]

    for check in (False, True):
        result = run("talos.yml", extra, check=check, env=env)
        outputs.append(result[2])
        expect(result, "homeserver-k8s", changed=0, note=f"{'dry run' if check else 'second run'} after the build")
    # The nodes' own version, never the client's (v1.14.9).
    assert "Talos v1.14.2, schematic " + base[:8] in result[2] and "v1.14.9" not in result[2], result[2][-3000:]

    # Drift: a node's configuration changed behind the role's back, on the worker, then on the control plane.
    s = read(state)
    s["nodes"]["127.0.0.31"].update(config="changed by hand\n", config_sha256="0")
    write(state, s)
    result = run("talos.yml", extra, env=env)
    outputs.append(result[2])
    expect(result, "homeserver-k8s", changed=1, note="run after drift")
    assert read(state)["nodes"]["127.0.0.31"]["config"] == worker
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=0, note="second run after drift")
    s = read(state)
    s["nodes"]["127.0.0.30"].update(config=cp.replace("listen-metrics-urls", "listen-other-urls"), config_sha256="0")
    s["apply_fails"] = "another apply configuration is already in progress"
    write(state, s)
    result = run("talos.yml", extra, env=env)
    outputs.append(result[2])
    assert result[0] != 0 and "homeserver-cp-1: talosctl apply-config failed:" in result[2], result[2][-3000:]
    assert "another apply configuration is already in progress" in result[2]
    s = read(state)
    del s["apply_fails"]
    write(state, s)
    result = run("talos.yml", extra, env=env)
    outputs.append(result[2])
    expect(result, "homeserver-k8s", changed=1, note="run after drift on the control plane")
    assert read(state)["nodes"]["127.0.0.30"]["config"] == cp, "applied without a reboot"
    assert "talosctl -n 127.0.0.30 service etcd restart" in result[2], result[2][-3000:]
    assert "talosctl -n 127.0.0.30 upgrade-k8s --to " in result[2], result[2][-3000:]
    result = run("talos.yml", extra, env=env)
    expect(result, "homeserver-k8s", changed=0, note="second run after drift on the control plane")
    assert "service etcd restart" not in result[2]

    # A pin bump: nothing is applied or upgraded, and the run says what waits for just talos-upgrade.
    before = read(state)
    bumped = extra | {"talos_version": "v1.14.3", "talos_kubernetes_version": "1.37.2"}
    result = run("talos.yml", bumped, env=env)
    outputs.append(result[2])
    expect(result, "homeserver-k8s", changed=0, note="run with bumped pins")
    after = read(state)
    assert after["nodes"] == before["nodes"] and after.get("upgrades") is None and after["k8s_version"] == "1.37.1"
    assert "just talos-upgrade" in result[2] and "homeserver-worker-1: Talos v1.14.3" in result[2], result[2][-3000:]
    assert "kubernetes: 1.37.2" in result[2]
    no_talos_secret(outputs)


def yaml_load(path):
    return yaml.safe_load(Path(path).read_text())


@pytest.mark.cluster
def test_talos_without_a_nas(tmp_path, factory):
    """The same two nodes as with a NAS: the control plane keeps its taint, and the worker carries the data volume
    and, without a NAS, the media volume."""
    extra, env = talos_vars(tmp_path, factory, nas=False), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    talos_state(state)
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=True, note="first run")
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=0, note="second run")
    nodes = read(state)["nodes"]
    cp, worker = nodes["127.0.0.30"]["config"], nodes["127.0.0.31"]["config"]
    assert "$patch: delete" not in cp and "UserVolumeConfig" not in cp, cp
    assert "name: local-path-provisioner" in worker and "name: media" in worker and "_drive-scsi2" in worker

    # A user volume whose disk selector matches nothing is never mounted: the run says so, naming the volume.
    s = read(state)
    s["nodes"]["127.0.0.31"]["volumes_missing"] = ["media"]
    write(state, s)
    rc, _, out = run("talos.yml", extra, env=env)
    assert rc != 0 and "homeserver-worker-1's user volumes are not ready: media (waiting)" in out, out[-3000:]


@pytest.mark.cluster
def test_talos_dry_run_on_a_fresh_host(tmp_path, factory):
    """No VM yet: every node is reported as one to install once it is up, without waiting for it, and nothing fails."""
    extra, env = talos_vars(tmp_path, factory, talos_probe_delay=60), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    talos_state(state, mode="absent")
    started = time.monotonic()
    result = run("talos.yml", extra, check=True, env=env)
    expect(result, "homeserver-k8s", changed=True, note="dry run with no node up")
    assert time.monotonic() - started < 50, "the dry run waited for the nodes"
    assert "Would install homeserver-cp-1 at 127.0.0.30 once its VM is up" in result[2], result[2][-3000:]
    assert read(state)["mutations"] == 0 and talos_files(tmp_path) == []


@pytest.fixture
def short_tmp():
    """An empty directory with a short path, for a test's TMPDIR. ansible-playbook binds a Unix socket in TMPDIR, and
    one under tmp_path can be too long for a socket's path: Python then moves the socket to /tmp, but its check counts
    four characters fewer than the path it makes, so on Linux a TMPDIR of 76 to 79 characters gets through and
    ansible-playbook fails with "Local RPC server did not start". tmp_path / "tmp" in CI's cluster job is 76."""
    with tempfile.TemporaryDirectory(prefix="talos-") as path:
        yield Path(path)


@pytest.mark.cluster
def test_talos_leaves_no_keys_behind(tmp_path, short_tmp, factory):
    """A scratch directory an interrupted run left, a day old, is removed by the next run, while a newer one, maybe
    another run's, stays. The rendered configurations a run kept on request go with the next run that does not ask."""
    extra, env = talos_vars(tmp_path, factory), talos_env(tmp_path)
    scratch = short_tmp
    env["TMPDIR"] = str(scratch)
    stale, fresh = scratch / "ansible.interrupted.talos", scratch / "ansible.running.talos"
    for directory in (stale, fresh):
        directory.mkdir()
        (directory / "secrets.yaml").write_text("cluster: {}\n")
    two_days_ago = time.time() - 2 * 86400
    os.utime(stale, (two_days_ago, two_days_ago))
    state = Path(extra["fake_talos_state"])
    talos_state(state)

    kept = extra | {"talos_keep_rendered": True}
    expect(run("talos.yml", kept, env=env), "homeserver-k8s", changed=True, note="a build keeping its renders")
    assert not stale.exists() and fresh.exists()
    rendered = Path(extra["talos_config_dir"]) / "rendered"
    assert sorted(p.name for p in rendered.iterdir()) == ["homeserver-cp-1.yaml", "homeserver-worker-1.yaml"]

    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=1, note="the renders removed")
    assert not rendered.exists()
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=0, note="nothing left to remove")


@pytest.mark.cluster
def test_talos_refuses_what_the_factory_answers_unpinned(tmp_path, factory):
    """A schematic id the factory gives other than the pinned one, and an installer image of the pinned version
    without a digest, are refused before any node is asked anything, in a dry run too."""
    extra, env = talos_vars(tmp_path, factory), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    talos_state(state)
    other = extra | {"talos_schematic_pins": extra["talos_schematic_pins"] | {"base": "ab" * 32}}
    rc, _, out = run("talos.yml", other, check=True, env=env)
    assert rc != 0 and f"gave the schematic base the id {schematic_id(SCHEMATICS['base'])}" in out, out[-3000:]
    assert f"talos_schematic_pins holds {'ab' * 32}" in out, out[-3000:]
    rc, _, out = run("talos.yml", extra | {"talos_version": "v1.14.9"}, check=True, env=env)
    assert rc != 0 and f"no digest for the installer image {schematic_id(SCHEMATICS['base'])}:v1.14.9" in out
    assert read(state)["mutations"] == 0


@pytest.mark.cluster
def test_talos_waits_for_a_slow_node_and_reports_a_failed_install(tmp_path, factory):
    """A node that answers only after a few questions is installed on the same run. One that never answers fails a
    real run after the retries, naming the MAC setting. One whose install fails reports why, not a timeout. A
    talosctl of another minor release is refused before anything connects."""
    extra, env = talos_vars(tmp_path, factory), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    talos_state(state)
    s = read(state)
    s["nodes"]["127.0.0.31"] = {"mode": "absent", "answers_after": 3, "version": "v1.14.2"}
    write(state, s)
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=True, note="a slow node")
    assert read(state)["nodes"]["127.0.0.31"]["mode"] == "configured"

    talos_state(state)
    s = read(state)
    s["nodes"]["127.0.0.31"] = {"mode": "absent"}
    write(state, s)
    rc, _, out = run("talos.yml", extra, env=env)
    assert rc != 0 and "homeserver-worker-1 at 127.0.0.31 does not answer" in out and "kubernetes_node_macs" in out, (
        out[-3000:]
    )

    # A node configured in maintenance mode answers with the talosconfig at once, still running from the ISO, so only
    # its install status tells. A selector that matches nothing keeps it pending, named before any bootstrap.
    talos_state(state)
    s = read(state)
    s["nodes"]["127.0.0.31"]["install_error"] = "no disk matched the selector"
    write(state, s)
    rc, _, out = run("talos.yml", extra | {"talos_config_dir": str(tmp_path / "home" / "other")}, env=env)
    assert rc != 0 and "homeserver-worker-1 did not install: still pending after 5 tries" in out, out[-3000:]
    assert ": no disk matched the selector. No disk matched the install selector" in out, out[-3000:]
    assert read(state)["nodes"]["127.0.0.31"]["mode"] == "configured" and "bootstrap_calls" not in read(state)

    talos_state(state)
    s = read(state)
    s["nodes"]["127.0.0.30"]["install_fails"] = "installer exited with code 1"
    write(state, s)
    rc, _, out = run("talos.yml", extra | {"talos_config_dir": str(tmp_path / "home" / "failed")}, env=env)
    assert rc != 0 and "homeserver-cp-1 did not install: the installer failed: installer exited with code 1" in out
    assert "bootstrap_calls" not in read(state)

    rc, _, out = run("talos.yml", extra, env=talos_env(tmp_path, client="v1.13.5"), check=True)
    assert rc != 0 and "talosctl on this machine is v1.13" in out and "install talosctl v1.14.x" in out, out[-3000:]


@pytest.mark.cluster
def test_talos_bootstraps_once(tmp_path, factory):
    """The record of a bootstrap stops a second one: with it and no running etcd the run fails, names the ways back,
    and bootstraps nothing, unless allowed. Without the record, a cluster whose first build failed half way is
    bootstrapped. A control plane not ready yet (FailedPrecondition) is asked again."""
    extra, env = talos_vars(tmp_path, factory), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    talos_state(state, bootstrap_precondition_count=2)
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=True, note="first run, retried bootstrap")
    assert read(state)["bootstrap_calls"] == 1

    # The control plane lost its etcd.
    s = read(state)
    s["bootstrapped"] = False
    write(state, s)
    rc, _, out = run("talos.yml", extra, env=env)
    assert rc != 0 and "talosctl bootstrap --recover-from=<snapshot>" in out and "vm-restore.md" in out, out[-3000:]
    assert read(state)["bootstrap_calls"] == 1, "bootstrapped over a lost cluster"
    expect(
        run("talos.yml", extra | {"talos_allow_bootstrap": True}, env=env),
        "homeserver-k8s",
        changed=True,
        note="a new cluster on purpose",
    )
    assert read(state)["bootstrap_calls"] == 2

    # No record, the nodes configured but etcd never bootstrapped: a first build that stopped half way.
    (Path(extra["talos_config_dir"]) / "bootstrapped").unlink()
    s = read(state)
    s["bootstrapped"] = False
    write(state, s)
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=True, note="a build that stopped half way")
    assert read(state)["bootstrap_calls"] == 3 and (Path(extra["talos_config_dir"]) / "bootstrapped").exists()


@pytest.mark.cluster
def test_talos_reinstalls_only_a_named_node(tmp_path, factory):
    """Once the cluster is bootstrapped, a node answering in maintenance mode gets nothing, in a dry run or a real
    one, since its configuration holds the cluster's keys and nothing proves who answers: the run fails and names
    the way to reinstall it. Named in talos_reinstall, as a list or a comma-separated string, it is installed."""
    extra, env = talos_vars(tmp_path, factory), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    talos_state(state)
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=True, note="first build")

    # The worker's system disk was wiped: it boots the ISO into maintenance mode again.
    s = read(state)
    s["nodes"][NODE_ADDRESSES["homeserver-worker-1"]] = {
        "mode": "maintenance",
        "version": "v1.14.2",
        "schematic": "iso",
    }
    write(state, s)
    mutations = read(state)["mutations"]
    for check in (True, False):
        rc, _, out = run("talos.yml", extra, check=check, env=env)
        assert rc != 0 and "homeserver-worker-1 answers at 127.0.0.31 in maintenance mode" in out, out[-3000:]
        assert "-e talos_reinstall=homeserver-worker-1" in out, out[-3000:]
    assert read(state)["mutations"] == mutations, "sent the configuration to a node no run named"

    expect(
        run("talos.yml", extra | {"talos_reinstall": "homeserver-worker-1, homeserver-cp-1"}, env=env),
        "homeserver-k8s",
        changed=True,
        note="the reinstall named",
    )
    assert read(state)["nodes"][NODE_ADDRESSES["homeserver-worker-1"]]["mode"] == "configured"
    expect(
        run("talos.yml", extra | {"talos_reinstall": ["homeserver-worker-1"]}, env=env),
        "homeserver-k8s",
        changed=0,
        note="the name left in place",
    )


@pytest.mark.cluster
def test_talos_refuses_another_clusters_credentials(tmp_path, factory):
    """talos_config_dir belongs to one cluster. A kubeconfig there naming another control plane (the home cluster's,
    on a test host), or credentials from another CA than the secrets bundle's, stop a run, a dry run too, before
    anything changes; local.yml's kubernetes_talos_config_dir gives a test host a directory of its own."""
    extra, env = talos_vars(tmp_path, factory), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    talos_state(state)
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=True, note="first run")
    home = Path(extra["talos_config_dir"])
    kubeconfig = yaml_load(home / "kubeconfig")
    assert kubeconfig["clusters"][0]["cluster"]["server"] == "https://127.0.0.30:6443"

    kubeconfig["clusters"][0]["cluster"]["server"] = "https://127.0.0.99:6443"
    (home / "kubeconfig").write_text(yaml.safe_dump(kubeconfig))
    kept = {f.name: f.read_bytes() for f in home.iterdir()}
    mutations = read(state)["mutations"]
    for check in (True, False):
        rc, _, out = run("talos.yml", extra, check=check, env=env)
        assert rc != 0 and "holds another cluster's credentials: its kubeconfig names the server" in out, out[-3000:]
        assert "https://127.0.0.99:6443, not https://127.0.0.30:6443" in out and "kubernetes_talos_config_dir" in out
    assert {f.name: f.read_bytes() for f in home.iterdir()} == kept and read(state)["mutations"] == mutations

    # The test host's own directory, from local.yml's setting through group_vars/kubernetes/talos.yml, with its
    # secrets bundle in it, where group_vars/kubernetes/talos.yml looks.
    own = {k: v for k, v in extra.items() if k not in ("talos_config_dir", "talos_secrets_file")}
    own |= {"kubernetes_talos_config_dir": "~/.talos/test"}
    bundle_file(tmp_path / "home" / ".talos" / "test" / "secrets.yaml")
    expect(run("talos.yml", own, env=env), "homeserver-k8s", changed=True, note="a directory of its own")
    assert (tmp_path / "home" / ".talos" / "test" / "kubeconfig").exists()
    assert {f.name: f.read_bytes() for f in home.iterdir()} == kept

    # Another secrets bundle: neither file was made from it.
    other = TALOS_SECRETS.replace(base64.b64encode(b"TEST-OS-CA").decode(), base64.b64encode(b"OTHER-CA").decode())
    other = other.replace(base64.b64encode(b"TEST-K8S-CA").decode(), base64.b64encode(b"OTHER-K8S").decode())
    other = bundle_file(tmp_path / "other-secrets.yaml", other)
    rc, _, out = run("talos.yml", own | {"talos_secrets_file": other}, check=True, env=env)
    assert rc != 0 and "its talosconfig trusts another CA" in out and "its kubeconfig trusts another CA" in out, out[
        -3000:
    ]
    no_talos_secret([out])


@pytest.mark.cluster
def test_talos_refuses_a_missing_or_open_secrets_bundle(tmp_path, factory):
    """The secrets bundle is a file of its own outside the repository, readable by its owner alone: missing, open
    to others, or inside the repository, it stops a run, a dry run too, before any node is asked, and is never
    printed."""
    extra, env = talos_vars(tmp_path, factory), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    talos_state(state)
    bundle = Path(extra["talos_secrets_file"])
    bundle.chmod(0o644)
    outputs = []
    for check in (True, False):
        rc, _, out = run("talos.yml", extra, check=check, env=env)
        assert rc != 0 and f"{bundle} has mode 0644" in out and "chmod 600" in out, out[-3000:]
        outputs.append(out)
    bundle.unlink()
    rc, _, out = run("talos.yml", extra, env=env)
    assert rc != 0 and f"There is no secrets bundle at {bundle}" in out, out[-3000:]
    assert "talosctl gen secrets -o" in out and "password manager" in out, out[-3000:]
    outputs.append(out)
    # The role knows the repository as the playbook's directory, which for these tests is this one.
    inside = HERE / "secrets.yaml"
    rc, _, out = run("talos.yml", extra | {"talos_secrets_file": str(inside)}, check=True, env=env)
    assert rc != 0 and "talos_secrets_file must be outside the repository" in out, out[-3000:]
    assert not inside.exists()
    assert read(state)["mutations"] == 0 and talos_files(tmp_path) == []
    no_talos_secret(outputs)


@pytest.mark.cluster
def test_talos_renews_expiring_credentials(tmp_path, factory):
    """Client certificates that expire within talos_renew_before_days are renewed: a dry run says so and writes
    nothing, the run replaces talosconfig and kubeconfig, and the next run leaves them alone."""
    extra, env = talos_vars(tmp_path, factory), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    talos_state(state, cert_days=10)
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=True, note="build, certificates of 10 days")
    home = Path(extra["talos_config_dir"])
    old = {name: (home / name).read_bytes() for name in ("talosconfig", "kubeconfig")}
    s = read(state)
    s["cert_days"] = 365
    write(state, s)
    result = run("talos.yml", extra, check=True, env=env)
    expect(result, "homeserver-k8s", changed=True, note="dry run, certificates expiring")
    assert "Would renew the kubeconfig" in result[2], result[2][-3000:]
    assert {name: (home / name).read_bytes() for name in old} == old, "the dry run wrote something"
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=True, note="renewal")
    new = {name: (home / name).read_bytes() for name in old}
    assert all(new[name] != old[name] for name in old), "not both renewed"
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=0, note="after the renewal")
    assert {name: (home / name).read_bytes() for name in old} == new


@pytest.mark.cluster
def test_talos_upgrade_and_etcd_snapshot(tmp_path, factory):
    """just talos-upgrade --check changes nothing and shows the plan; just talos-upgrade snapshots etcd first,
    upgrades the control plane before the worker, then Kubernetes, and has nothing left to do the second time.
    just etcd-snapshot writes a snapshot and nothing else."""
    extra, env = talos_vars(tmp_path, factory), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    talos_state(state)
    expect(run("talos.yml", extra, env=env), "homeserver-k8s", changed=True, note="build")

    # A snapshot needs the control plane alone: here the worker is down and the Image Factory unreachable.
    s = read(state)
    worker = s["nodes"]["127.0.0.31"]
    s["nodes"]["127.0.0.31"] = {"mode": "absent"}
    write(state, s)
    before = read(state)
    started = time.monotonic()
    snapshot = extra | {"talos_entry": "etcd-snapshot", "talos_image_factory_url": "http://127.0.0.1:9"}
    result = run("talos_upgrade.yml", snapshot | {"talos_probe_delay": 60}, env=env)
    expect(result, "homeserver-k8s", changed=True, note="etcd snapshot")
    assert time.monotonic() - started < 50, "the snapshot waited for the worker"
    after = read(state)
    assert (
        len(after["snapshots"]) == 1 and Path(after["snapshots"][0]).parent == Path(extra["talos_config_dir"]) / "etcd"
    )
    assert after["nodes"] == before["nodes"] and after["mutations"] == before["mutations"]
    after["nodes"]["127.0.0.31"] = worker
    write(state, after)

    bumped = extra | {"talos_version": "v1.14.3", "talos_kubernetes_version": "1.37.2"}
    result = run("talos_upgrade.yml", bumped, check=True, env=env)
    expect(result, "homeserver-k8s", changed=True, note="talos-upgrade --check")
    assert "Talos v1.14.2 -> v1.14.3" in result[2] and "1.37.1 -> 1.37.2" in result[2], result[2][-3000:]
    assert "no deprecated APIs found" in result[2] and read(state)["mutations"] == after["mutations"]
    assert not read(state).get("pre_pulls"), "the dry run pulled images onto the nodes"

    result = run("talos_upgrade.yml", bumped, env=env)
    expect(result, "homeserver-k8s", changed=True, note="talos-upgrade")
    s = read(state)
    assert s["upgrades"] == ["127.0.0.30", "127.0.0.31"], "the control plane was not upgraded first"
    assert len(s["snapshots"]) == 2 and s["k8s_version"] == "1.37.2"
    assert all(n["version"] == "v1.14.3" for n in s["nodes"].values())
    result = run("talos_upgrade.yml", bumped, env=env)
    expect(result, "homeserver-k8s", changed=0, note="talos-upgrade again")
    assert len(read(state)["snapshots"]) == 2

    # After the upgrade, a normal run renders with the versions now running: no drift.
    expect(run("talos.yml", bumped, check=True, env=env), "homeserver-k8s", changed=0, note="dry run after upgrading")


# ------------------------------------------------------------------------------------------------- Flux ---

# A sentinel in the age key's format, built here so gitleaks does not take it for a key.
AGE_KEY = "-".join(["AGE", "SECRET", "KEY", "1TESTSENTINELNOTAKEY"])


@pytest.fixture(scope="module")
def operator_releases():
    """The Flux Operator's release downloads: GET /<version>/install.yaml serves the fake's release of that version,
    whose SHA-256 the test then pins, as the real file's is."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            found = re.fullmatch(r"/(v\d+\.\d+\.\d+)/install\.yaml", self.path)
            if not found:
                self.send_error(404)
                return
            body = operator_release(found.group(1))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def operator_release(version):
    return yaml.safe_dump_all(fake_kubectl.operator_objects(version)).encode()


# What the operator's image is pinned to in the tests: a digest of no real image.
OPERATOR_DIGEST = "sha256:" + "0" * 63 + "1"


def flux_vars(tmp_path, operator_releases, nas=True, **more):
    """The flux test playbook's variables, isolated in tmp_path: a sentinel age key, never the owner's, and the
    operator's releases from the test's own server, pinned by the SHA-256 of what it serves. The same two nodes
    in both setups; without a NAS, the worker sized for a small host, with a media disk."""
    config_dir = tmp_path / "home" / ".talos" / "homeserver"
    config_dir.mkdir(parents=True, exist_ok=True)
    key = tmp_path / "home" / "homeserver.agekey"
    if not key.exists():
        key.write_text(f"# created: test\n# public key: age1test\n{AGE_KEY}\n")
        key.chmod(0o600)
    extra = {
        "fake_talos_state": str(tmp_path / "talos.json"),
        "talos_config_dir": str(config_dir),
        "sops_age_key_file": str(key),
        "flux_bootstrap_wait_timeout": "0s",
        "truenas_static_address": "127.0.0.10/24" if nas else "",
        "kubernetes_node_addresses": NODE_ADDRESSES,
        "kubernetes_lb_range": "127.0.0.40-127.0.0.49",
        "kubernetes_lb_traefik_ip": "127.0.0.40",
        "kubernetes_lb_frigate_ip": "127.0.0.41",
        "proxmox_address": PROXMOX_ADDRESS,
        "flux_sync_url": "https://example.invalid/homeserver.git",
        "flux_bootstrap_operator_url": operator_releases,
        "flux_bootstrap_operator_sha256": hashlib.sha256(operator_release("v0.61.0")).hexdigest(),
        "flux_bootstrap_operator_digest": OPERATOR_DIGEST,
    }
    if not nas:
        sizes = {"cores": 4, "memory_mib": 12288, "system_disk_gib": 32, "data_disk_gib": 50, "media_disk_gib": 100}
        extra["kubernetes_node_sizes"] = {"homeserver-worker-1": sizes}
    return extra | more


def flux_objects(state):
    return read(state).get("objects", {})


def no_age_key(outputs):
    encoded = base64.b64encode(f"# created: test\n# public key: age1test\n{AGE_KEY}\n".encode()).decode()
    for out in outputs:
        assert AGE_KEY not in out and encoded not in out, "the age key reached Ansible's output"


@pytest.mark.cluster
@pytest.mark.parametrize("nas", [True, False], ids=["nas", "without-a-nas"])
def test_flux_bootstrap(tmp_path, operator_releases, nas):
    """Before the cluster exists, a dry run says what it would install and a real run stops, naming where the
    kubeconfig comes from. Then the operator, its image pinned by digest, the age key, the settings and the
    FluxInstance, an exact Flux release from a pinned artifact, are applied once, and left alone; the setup's own
    path and settings; a FluxInstance moved to the other setup's path is refused unless the run allows it; a changed
    or removed setting is applied; an operator bump re-installs it, and a release whose install.yaml is not the
    pinned one is refused before anything is applied. The key never shows."""
    extra, env = flux_vars(tmp_path, operator_releases, nas=nas), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    write(state, {"nodes": {}, "mutations": 0})
    outputs = []

    result = run("flux.yml", extra, check=True, env=env)
    outputs.append(result[2])
    expect(result, "homeserver-k8s", changed=True, note="dry run before the cluster")
    assert "Would install the Flux Operator v0.61.0" in result[2], result[2][-3000:]
    rc, _, out = run("flux.yml", extra, env=env)
    assert rc != 0 and "roles/talos writes it into talos_config_dir" in out, out[-3000:]

    # The cluster is up: its kubeconfig exists and it answers.
    (Path(extra["talos_config_dir"]) / "kubeconfig").write_text("apiVersion: v1\n")
    s = read(state)
    s["bootstrapped"] = True
    write(state, s)
    result = run("flux.yml", extra, check=True, env=env)
    outputs.append(result[2])
    expect(result, "homeserver-k8s", changed=True, note="dry run before the operator")
    assert flux_objects(state) == {} or read(state)["mutations"] == 0

    result = run("flux.yml", extra, env=env)
    outputs.append(result[2])
    expect(result, "homeserver-k8s", changed=True, note="first run")
    objects = flux_objects(state)
    assert read(state)["operator_installs"] == [f"flux-operator:v0.61.0@{OPERATOR_DIGEST}"]
    instance = objects["FluxInstance/flux-system/flux"]["spec"]
    assert instance["distribution"]["version"] == "2.9.6"
    assert re.fullmatch(r"oci://\S+/flux-operator-manifests@sha256:[0-9a-f]{64}", instance["distribution"]["artifact"])
    assert instance["sync"] == {
        "kind": "GitRepository",
        "url": "https://example.invalid/homeserver.git",
        "ref": "refs/heads/main",
        "path": "k8s/clusters/nas" if nas else "k8s/clusters/local",
        "interval": "1m",
    }
    assert "kustomize" not in instance, "no feature gate is set: strict substitution is Flux 2.9's default"
    settings = objects["ConfigMap/flux-system/cluster-settings"]["data"]
    # Flux reconciles what reads the settings as soon as they change.
    labels = objects["ConfigMap/flux-system/cluster-settings"]["metadata"]["labels"]
    assert labels == {"reconcile.fluxcd.io/watch": "Enabled"}
    media = {"MEDIA_LOCAL_PATH": "/var/mnt/media/library", "FRIGATE_LOCAL_PATH": "/var/mnt/media/frigate"}
    assert settings["DOMAIN"] == "homeserver.internal" and settings["DATA_NODE"] == "homeserver-worker-1"
    assert all(isinstance(v, str) for v in settings.values())
    if nas:
        assert settings["NAS_ADDRESS"] == "127.0.0.10" and settings["MEDIA_DATASET"] == "/mnt/ephemeral/jellyfin"
        assert settings["EGRESS_DENIED_ADDRESS"] == "127.0.0.10" and not set(media) & set(settings)
    else:
        assert {k: settings[k] for k in media} == media and "NAS_ADDRESS" not in settings
        assert settings["EGRESS_DENIED_ADDRESS"] == PROXMOX_ADDRESS
    assert settings["PROXMOX_ADDRESS"] == PROXMOX_ADDRESS
    secret = objects["Secret/flux-system/sops-age"]["data"]["age.agekey"]
    assert AGE_KEY in base64.b64decode(secret).decode()

    for check in (False, True):
        result = run("flux.yml", extra, check=check, env=env)
        outputs.append(result[2])
        expect(result, "homeserver-k8s", changed=0, note=f"{'dry run' if check else 'second run'} after the bootstrap")

    # local.yml rewritten for the other setup would move Flux to the other path, and prune what this one applied:
    # refused, dry or real, before anything is applied, unless the run allows it.
    other = flux_vars(tmp_path, operator_releases, nas=not nas)
    mutations = read(state)["mutations"]
    for check in (True, False):
        rc, _, out = run("flux.yml", other, check=check, env=env)
        assert rc != 0 and f"The FluxInstance syncs {instance['sync']['path']}, and this run would make it" in out, out[
            -3000:
        ]
    assert read(state)["mutations"] == mutations
    for allowed in (other, extra):
        result = run("flux.yml", allowed | {"flux_bootstrap_allow_path_change": True}, env=env)
        outputs.append(result[2])
        expect(result, "homeserver-k8s", changed=1, note="a path change allowed")
    assert flux_objects(state)["FluxInstance/flux-system/flux"]["spec"]["sync"]["path"] == instance["sync"]["path"]

    # A setting changed, then one removed: each is applied, and a removed key disappears.
    result = run("flux.yml", extra | {"kubernetes_domain": "home.example.org"}, env=env)
    outputs.append(result[2])
    expect(result, "homeserver-k8s", changed=1, note="a changed setting")
    assert flux_objects(state)["ConfigMap/flux-system/cluster-settings"]["data"]["DOMAIN"] == "home.example.org"
    fewer = extra | {"flux_bootstrap_settings": {"DOMAIN": "home.example.org"}}
    expect(run("flux.yml", fewer, env=env), "homeserver-k8s", changed=1, note="a removed setting")
    assert flux_objects(state)["ConfigMap/flux-system/cluster-settings"]["data"] == {"DOMAIN": "home.example.org"}

    # A new operator release whose install.yaml is not the pinned one is refused, and nothing is applied.
    bump = extra | {"flux_bootstrap_operator_version": "v0.62.0"}
    mutations = read(state)["mutations"]
    rc, _, out = run("flux.yml", bump, env=env)
    assert rc != 0 and "did not match" in out, out[-3000:]
    assert read(state)["mutations"] == mutations and len(read(state)["operator_installs"]) == 1

    # Pinned, it is installed over the old one.
    bump |= {"flux_bootstrap_operator_sha256": hashlib.sha256(operator_release("v0.62.0")).hexdigest()}
    result = run("flux.yml", bump, env=env)
    outputs.append(result[2])
    expect(result, "homeserver-k8s", changed=True, note="an operator bump")
    assert read(state)["operator_installs"][1] == f"flux-operator:v0.62.0@{OPERATOR_DIGEST}"
    no_age_key(outputs)


GROUPS = ["apps-media", "apps-nextcloud", "apps-photoprism", "apps-frigate", "apps-homepage"]


def kustomizations_in(objects):
    return {k.rpartition("/")[2] for k in objects if k.startswith("Kustomization/")}


@pytest.mark.cluster
def test_flux_bootstrap_leaves_groups_out(tmp_path, operator_releases):
    """local.yml's kubernetes_apps reaches Flux through the FluxInstance alone: the groups it does not name are dropped
    from the sync by one patch, which prunes them, and the run waits until they are gone. A Deployment just suspend
    holds there is skipped by Flux's prune and stays at zero; a dry run changes nothing. Running every group again
    takes the patch away, and the groups come back."""
    extra, env = flux_vars(tmp_path, operator_releases), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    (Path(extra["talos_config_dir"]) / "kubeconfig").write_text("apiVersion: v1\n")
    write(state, {"nodes": {}, "mutations": 0, "bootstrapped": True, "synced": GROUPS})
    expect(run("flux.yml", extra, env=env), "homeserver-k8s", changed=True, note="every group")
    objects = flux_objects(state)
    assert "kustomize" not in objects["FluxInstance/flux-system/flux"]["spec"]
    assert "patches" not in objects["Kustomization/flux-system/flux-system"]["spec"]
    assert kustomizations_in(objects) == {"flux-system", *GROUPS}

    # The apps as Flux applied them, PhotoPrism held at zero by just suspend.
    s = read(state)
    for namespace, name, group, replicas in [
        ("media", "jellyfin", "apps-media", 1),
        ("photoprism", "photoprism", "apps-photoprism", 0),
        ("photoprism", "photoprism-db", "apps-photoprism", 1),
    ]:
        s["objects"][f"Deployment/{namespace}/{name}"] = deployment(namespace, name, group, replicas)
    s["objects"]["Deployment/photoprism/photoprism"]["metadata"]["annotations"] = {RECONCILE: "disabled", RECORDED: "1"}
    write(state, s)

    chosen = extra | {"kubernetes_apps": ["media", "nextcloud", "homepage"]}
    mutations = read(state)["mutations"]
    expect(run("flux.yml", chosen, check=True, env=env), "homeserver-k8s", changed=True, note="dry run leaving out")
    assert read(state)["mutations"] == mutations

    expect(run("flux.yml", chosen, env=env), "homeserver-k8s", changed=True, note="leaving groups out")
    objects = flux_objects(state)
    patches = objects["FluxInstance/flux-system/flux"]["spec"]["kustomize"]["patches"]
    assert [p["target"]["name"] for p in patches] == ["flux-system"]
    sync = objects["Kustomization/flux-system/flux-system"]["spec"]["patches"]
    assert [p["target"]["name"] for p in sync] == ["(apps-photoprism|apps-frigate)"]
    assert sync[0]["patch"].startswith("$patch: delete\n")
    assert kustomizations_in(objects) == {"flux-system", "apps-media", "apps-nextcloud", "apps-homepage"}
    assert "wait deleted kustomization/apps-photoprism kustomization/apps-frigate" in read(state)["events"]
    # Flux pruned PhotoPrism's database; PhotoPrism, held, stays at zero until just resume once its group is back.
    deployments = {k.partition("/")[2] for k in objects if k.startswith("Deployment/") and "flux-operator" not in k}
    assert deployments == {"media/jellyfin", "photoprism/photoprism"}
    assert objects["ConfigMap/flux-system/cluster-settings"]["data"]["HOMEPAGE_SERVICES"] == "media nextcloud"

    for check in (False, True):
        result = run("flux.yml", chosen, check=check, env=env)
        expect(result, "homeserver-k8s", changed=0, note=f"{'dry run' if check else 'second run'} leaving out")

    # Every group again: no patch anywhere, and every group back.
    expect(run("flux.yml", extra, env=env), "homeserver-k8s", changed=True, note="every group again")
    objects = flux_objects(state)
    assert "kustomize" not in objects["FluxInstance/flux-system/flux"]["spec"]
    assert "patches" not in objects["Kustomization/flux-system/flux-system"]["spec"]
    assert kustomizations_in(objects) == {"flux-system", *GROUPS}


@pytest.mark.cluster
def test_flux_bootstrap_refuses_a_shared_key_and_reports_a_stuck_operator(tmp_path, operator_releases):
    """An age key file holding two keys, one others can read, or none, is refused before anything is applied. An
    operator that never becomes available fails the run."""
    extra, env = flux_vars(tmp_path, operator_releases), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    (Path(extra["talos_config_dir"]) / "kubeconfig").write_text("apiVersion: v1\n")
    write(state, {"nodes": {}, "mutations": 0, "bootstrapped": True})
    key = Path(extra["sops_age_key_file"])
    for content, mode in ((f"{AGE_KEY}\n{AGE_KEY}2\n", 0o600), (f"{AGE_KEY}\n", 0o644), ("# no key\n", 0o600)):
        key.write_text(content)
        key.chmod(mode)
        rc, _, out = run("flux.yml", extra, check=True, env=env)
        assert rc != 0 and "must exist, be readable by you alone (mode 0600), and hold exactly one" in out, out[-3000:]
        assert AGE_KEY not in out
    assert read(state)["mutations"] == 0
    key.write_text(f"{AGE_KEY}\n")
    key.chmod(0o600)

    s = read(state)
    s["operator_never_available"] = True
    write(state, s)
    rc, _, out = run("flux.yml", extra, env=env)
    assert rc != 0 and "timed out waiting for the condition on deployment/flux-operator" in out, out[-3000:]

    # The FluxInstance ready, but a Kustomization below it failing: the run fails, and shows them.
    s = read(state)
    s.update(operator_never_available=False, kustomizations_not_ready=True)
    write(state, s)
    rc, _, out = run("flux.yml", extra, env=env)
    assert rc != 0 and "Flux's Kustomizations are not all ready after 0s: error: timed out waiting" in out, out[-3000:]
    assert "flux-system   flux-system" in out, out[-3000:]


# ------------------------------------------------------------------------------------------ Home Assistant ---

HAOS_PASSWORD = "HAOS-TEST-OWNER-PASSWORD"
HAOS_OVA = "local:import/haos_ova-18.3.ova"


@pytest.fixture
def home_assistant():
    """A Home Assistant's onboarding API, from its source (homeassistant/components/onboarding/views.py): GET
    /api/onboarding lists the steps, POST /api/onboarding/users creates the owner account while the user step is
    not done, and refuses it once done. `not_ready` counts requests answered 401 first, as Home Assistant OS's
    landing page answers every /api/ request while it prepares. Every request to the port's /api/ is redirected
    (307) to /ha/api/, as Home Assistant OS 18 redirects port 8123 to Home Assistant on port 80, which its first live
    run showed."""
    state = {"not_ready": 0, "users": [], "posted": 0}

    class Handler(http.server.BaseHTTPRequestHandler):
        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def redirected(self):
            if self.path.startswith("/api/"):
                self.send_response(307)
                self.send_header("Location", "/ha" + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True
            self.path = self.path.removeprefix("/ha")
            return False

        def do_GET(self):
            if self.redirected():
                return
            if state["not_ready"] > 0:
                state["not_ready"] -= 1
                self.reply(401, {"message": "Unauthorized"})
            elif self.path == "/api/onboarding":
                steps = ["user", "core_config", "analytics", "integration"]
                self.reply(200, [{"step": s, "done": s == "user" and bool(state["users"])} for s in steps])
            else:
                self.reply(404, {"message": "Not found"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if self.redirected():
                return
            if self.path != "/api/onboarding/users" or state["not_ready"] > 0:
                self.reply(401 if state["not_ready"] else 404, {"message": "Unauthorized"})
                return
            state["posted"] += 1
            if state["users"]:
                self.reply(403, {"message": "User step already done"})
            elif set(body) != {"name", "username", "password", "client_id", "language"}:
                self.reply(400, {"message": f"Message format incorrect: {sorted(body)}"})
            else:
                state["users"].append(body)
                self.reply(200, {"auth_code": "HAOS-TEST-AUTH-CODE"})

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["port"] = server.server_address[1]
    yield state
    server.shutdown()


def haos_vars(tmp_path, ha, nas=True, **more):
    """The homeassistant test playbook's variables: the VM's MAC, the owner's password, the fake's port, short waits."""
    extra = {
        "fake_pve_state": str(tmp_path / "pve.json"),
        "truenas_static_address": "127.0.0.10/24" if nas else "",
        "haos_mac": mac(9),
        "vault_haos_owner_password": HAOS_PASSWORD,
        "haos_onboard_port": ha["port"],
        "haos_onboard_delay": 0,
        "haos_onboard_retries": 5,
        "haos_onboard_agent_retries": 5,
    }
    return extra | more


def no_haos_secret(outputs):
    for out in outputs:
        assert HAOS_PASSWORD not in out and "HAOS-TEST-AUTH-CODE" not in out, "a secret reached Ansible's output"


@pytest.mark.cluster
def test_haos_with_a_nas(tmp_path, home_assistant):
    """With a NAS: a dry run on a fresh host says what it would download, create and onboard, changing nothing; the
    persistent storage must exist; then the OVA is downloaded with its checksum, the VM created from its disk, started,
    and Home Assistant, once past its landing page, given its owner account, once; later runs change nothing; USB
    devices come and go on the running VM; a larger disk is grown."""
    ha = home_assistant
    extra = haos_vars(tmp_path, ha)
    state = Path(extra["fake_pve_state"])
    vm_state(state)
    outputs = []

    result = run("homeassistant.yml", extra, check=True)
    expect(result, "homeserver", changed=True, note="dry run on a fresh host")
    outputs.append(result[2])
    assert "Would download: haos_ova-18.3.ova" in result[2] and re.search(r'homeassistant"?:\s*"?create', result[2])
    assert "Would wait for Home Assistant on VM 310 and create the owner account owner" in result[2], result[2][-3000:]
    assert read(state)["mutations"] == 0 and ha["posted"] == 0, "the dry run changed something"

    rc, _, out = run("homeassistant.yml", extra)
    assert rc != 0 and "homeserver-persistent: does not exist" in out and vms_of(state) == {}, out[-3000:]

    seed_storage(state, PERSISTENT_NFS, EPHEMERAL_NFS)
    ha["not_ready"] = 3
    result = run("homeassistant.yml", extra)
    expect(result, "homeserver", changed=True, note="first run")
    outputs.append(result[2])
    vm = vms_of(state)["310"]
    assert vm["status"] == "running" and vm["config"]["name"] == "homeassistant"
    assert (
        vm["config"]["scsi0"].startswith("homeserver-persistent:310/vm-310-disk-1.qcow2,")
        and "size=32G" in (vm["config"]["scsi0"])
    )
    efi = vm["config"]["efidisk0"]
    assert efi.startswith("homeserver-persistent:310/vm-310-disk-0.") and "pre-enrolled-keys=0" in efi, efi
    assert (vm["config"]["boot"], vm["config"]["tags"], vm["config"]["bios"]) == ("order=scsi0", "haos", "ovmf")
    assert vm["config"]["watchdog"] == "model=i6300esb,action=reset" and vm["config"]["startup"].startswith("order=3,")
    assert vm["config"]["net0"].lower().startswith(f"virtio={mac(9)},") and "ide2" not in vm["config"]
    assert [v["volid"] for v in read(state)["content"]["local"]] == [HAOS_OVA], "the OVA was not downloaded once"
    pinned = yaml.safe_load((HERE.parents[1] / "group_vars" / "proxmox" / "haos.yml").read_text())["haos_ova_sha256"]
    assert read(state)["checksums"] == {"haos_ova-18.3.ova": ["sha256", pinned]}, "the OVA was not checked"
    assert ha["users"] == [
        {
            "name": "Owner",
            "username": "owner",
            "password": HAOS_PASSWORD,
            "client_id": f"http://127.0.0.1:{ha['port']}/ha/",
            "language": "en",
        }
    ]
    assert f"answers at http://127.0.0.1:{ha['port']}/ha/ with its owner account owner" in result[2], result[2][-3000:]

    result = run("homeassistant.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run (changed: {changed_tasks(result[2])})")
    outputs.append(result[2])
    result = run("homeassistant.yml", extra, check=True)
    expect(result, "homeserver", changed=0, note=f"dry run after applying (changed: {changed_tasks(result[2])})")
    assert ha["posted"] == 1, "the owner account was asked for again"

    # A Zigbee stick passed through: it reaches the running VM at once, and goes again.
    stick = extra | {"haos_usb": [{"host": "10c4:ea60"}]}
    expect(run("homeassistant.yml", stick), "homeserver", changed=1, note="a USB stick added")
    assert vms_of(state)["310"]["config"]["usb0"] == "host=10c4:ea60"
    expect(run("homeassistant.yml", stick), "homeserver", changed=0, note="a USB stick added, again")
    expect(run("homeassistant.yml", extra), "homeserver", changed=1, note="the USB stick taken away")
    assert "usb0" not in vms_of(state)["310"]["config"]

    # A larger disk is grown; the image's own size is the least local.yml may give.
    grown = extra | {"haos_size": {"disk_gib": 64}}
    expect(run("homeassistant.yml", grown), "homeserver", changed=1, note="a larger disk")
    assert "size=64G" in vms_of(state)["310"]["config"]["scsi0"]
    rc, _, out = run("homeassistant.yml", extra | {"haos_size": {"disk_gib": 16}}, check=True)
    assert rc != 0 and "disk_gib must be at least 32" in out, out[-3000:]
    no_haos_secret(outputs)


@pytest.mark.cluster
def test_haos_without_a_nas(tmp_path, home_assistant):
    """Without a NAS: the disk on local-lvm, grown to its size in the run that creates it; the guest agent answers
    only once the VM has booted; a Home Assistant already set up (restored from a backup, say) is left alone."""
    ha = home_assistant
    ha["users"].append({"username": "someone"})
    extra = haos_vars(tmp_path, ha, nas=False, haos_size={"disk_gib": 40, "memory_mib": 3072})
    state = Path(extra["fake_pve_state"])
    vm_state(state, agent_down=2)

    # The installer's local holds no images, so Proxmox could not extract the OVA's disk there: refused, naming the
    # command that enables them, before anything is made.
    for check in (True, False):
        rc, _, out = run("homeassistant.yml", extra, check=check)
        assert rc != 0 and "pvesm set local --content backup,import,iso,vztmpl,images" in out, out[-3000:]
    assert vms_of(state) == {}
    s = read(state)
    next(e for e in s["storage"] if e["storage"] == "local")["content"] += ",images"
    write(state, s)

    result = run("homeassistant.yml", extra)
    expect(result, "homeserver", changed=True, note="first run")
    vm = vms_of(state)["310"]["config"]
    assert vm["scsi0"].startswith("local-lvm:vm-310-disk-1,") and "size=40G" in vm["scsi0"] and vm["memory"] == "3072"
    assert ha["posted"] == 0, "an onboarded Home Assistant was asked to onboard"
    result = run("homeassistant.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run (changed: {changed_tasks(result[2])})")

    # Without Home Assistant, nothing is read, created or downloaded.
    without = {k: v for k, v in extra.items() if not k.startswith("haos_")}
    result = run("homeassistant.yml", without)
    expect(result, "homeserver", changed=0, note="no Home Assistant")
    assert "this setup has no Home Assistant VM" in result[2] and "Read the node's VMs" not in result[2]


@pytest.mark.cluster
def test_haos_refusals(tmp_path, home_assistant):
    """A Home Assistant is never started without its owner's password; an OVA whose checksum does not match is never
    stored, and no VM is made without it; a Home Assistant that refuses the account stops the run, saying so."""
    ha = home_assistant
    extra = haos_vars(tmp_path, ha, nas=False)
    state = Path(extra["fake_pve_state"])
    vm_state(state)
    s = read(state)
    next(e for e in s["storage"] if e["storage"] == "local")["content"] += ",images"
    write(state, s)

    for check in (True, False):
        rc, _, out = run("homeassistant.yml", extra | {"vault_haos_owner_password": "short"}, check=check)
        assert rc != 0 and "no vault_haos_owner_password of at least 12 characters" in out, out[-3000:]
    assert read(state)["mutations"] == 0

    s = read(state)
    s["checksum_fails"] = True
    write(state, s)
    rc, _, out = run("homeassistant.yml", extra)
    assert rc != 0 and "checksum mismatch" in out and "938408c5" in out, out[-3000:]
    assert "local" not in read(state).get("content", {}) or not read(state)["content"]["local"]
    assert vms_of(state) == {}, "a VM was made without its image"

    s = read(state)
    s["checksum_fails"] = False
    write(state, s)
    ha["not_ready"] = 99
    rc, _, out = run("homeassistant.yml", extra)
    assert rc != 0 and "Wait for Home Assistant's onboarding API" in out, out[-3000:]
    assert ha["users"] == [], "an account was made on the landing page"
    no_haos_secret([out])


# -------------------------------------------------------------------------------------- app_scale ---

FLUX_LABELS = "kustomize.toolkit.fluxcd.io"
RECONCILE = "kustomize.toolkit.fluxcd.io/reconcile"
RECORDED = "homeserver.internal/replicas"


def kustomization(name, path):
    return {
        "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
        "kind": "Kustomization",
        "metadata": {"name": name, "namespace": "flux-system"},
        "spec": {"path": path},
    }


def deployment(namespace, name, owner, replicas=1, **labels):
    """A Deployment as Flux leaves it: labelled with the Kustomization that applied it (`owner`)."""
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": name,
                f"{FLUX_LABELS}/name": owner,
                f"{FLUX_LABELS}/namespace": "flux-system",
                **labels,
            },
        },
        "spec": {"replicas": replicas, "selector": {"matchLabels": {"app.kubernetes.io/name": name}}},
    }


DATABASE = {"app.kubernetes.io/component": "database"}
APP_OBJECTS = [
    {
        "apiVersion": "apiextensions.k8s.io/v1",
        "kind": "CustomResourceDefinition",
        "metadata": {"name": "kustomizations.kustomize.toolkit.fluxcd.io"},
        "spec": {"names": {"kind": "Kustomization"}, "scope": "Namespaced"},
    },
    kustomization("apps-media", "./k8s/apps/media"),
    kustomization("apps-nextcloud", "./k8s/apps/nextcloud"),
    kustomization("apps-homepage", "./k8s/apps/homepage"),
    kustomization("infra-controllers", "./k8s/infrastructure/controllers/nas"),
    deployment("media", "sonarr", "apps-media"),
    deployment("media", "jellyfin", "apps-media"),
    deployment("nextcloud", "nextcloud", "apps-nextcloud"),
    deployment("nextcloud", "nextcloud-db", "apps-nextcloud", **DATABASE),
    deployment("homepage", "homepage", "apps-homepage", replicas=2),
    # The platform's: one a Flux Kustomization outside k8s/apps applies, one a HelmRelease.
    deployment("local-path-storage", "local-path-provisioner", "infra-controllers"),
    {
        **deployment("traefik", "traefik", "-"),
        "metadata": {"name": "traefik", "namespace": "traefik", "labels": {"helm.toolkit.fluxcd.io/name": "traefik"}},
    },
]


def apps_vars(tmp_path, **more):
    """The apps test playbook's variables: the flux test's cluster, its kubeconfig naming the control plane."""
    # The apps' play never downloads the Flux Operator, so its release server is never asked.
    extra = flux_vars(tmp_path, "https://example.invalid/flux-operator")
    kubeconfig = Path(extra["talos_config_dir"]) / "kubeconfig"
    kubeconfig.write_text(yaml.safe_dump({"clusters": [{"cluster": {"server": "https://127.0.0.30:6443"}}]}))
    return extra | more


def apps_state(path, **more):
    objects = {f"{o['kind']}/{o['metadata'].get('namespace', '')}/{o['metadata']['name']}": o for o in APP_OBJECTS}
    write(path, {"nodes": {}, "mutations": 0, "bootstrapped": True, "objects": objects, **more})


def deployments_of(state):
    return {
        k.removeprefix("Deployment/"): (o["spec"]["replicas"], (o["metadata"].get("annotations") or {}))
        for k, o in read(state)["objects"].items()
        if k.startswith("Deployment/")
    }


UP = {
    "media/sonarr": (1, {}),
    "media/jellyfin": (1, {}),
    "nextcloud/nextcloud": (1, {}),
    "nextcloud/nextcloud-db": (1, {}),
    "homepage/homepage": (2, {}),
    "local-path-storage/local-path-provisioner": (1, {}),
    "traefik/traefik": (1, {}),
}


def held(replicas):
    return (0, {RECONCILE: "disabled", RECORDED: str(replicas)})


@pytest.mark.cluster
def test_app_scale_down_and_up(tmp_path):
    """Down stops every app, apps before their databases, and holds each against Flux, leaving the platform alone;
    up brings back what down stopped, databases first. Both change nothing a second time, and a dry run nothing
    at all. --only narrows either to apps or groups, and a name that is neither is refused before anything changes."""
    extra, env = apps_vars(tmp_path), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    apps_state(state)
    down, up = extra | {"apps_state": "down"}, extra | {"apps_state": "up"}

    result = run("apps.yml", down, check=True, env=env)
    expect(result, "homeserver-k8s", changed=1, note="dry run of down")
    assert (
        "Would stop, in this order: homepage/homepage, media/jellyfin, media/sonarr, nextcloud/nextcloud, "
        "then nextcloud/nextcloud-db." in result[2]
    ), result[2][-3000:]
    assert read(state)["mutations"] == 0

    expect(run("apps.yml", down, env=env), "homeserver-k8s", changed=True, note="down")
    assert deployments_of(state) == UP | {
        "media/sonarr": held(1),
        "media/jellyfin": held(1),
        "nextcloud/nextcloud": held(1),
        "nextcloud/nextcloud-db": held(1),
        "homepage/homepage": held(2),
    }
    events = read(state)["events"]
    # Nextcloud's pods are gone before its database is touched.
    assert events.index("wait nextcloud app.kubernetes.io/name=nextcloud") < events.index(
        "patch nextcloud/nextcloud-db 0"
    ), events
    for check in (False, True):
        result = run("apps.yml", down, check=check, env=env)
        expect(result, "homeserver-k8s", changed=0, note=f"{'dry run of' if check else 'second'} down")
        assert "Already down: homepage/homepage, media/jellyfin" in result[2], result[2][-3000:]

    result = run("apps.yml", up, check=True, env=env)
    expect(result, "homeserver-k8s", changed=1, note="dry run of up")
    assert "Would start, in this order: nextcloud/nextcloud-db, then homepage/homepage" in result[2], result[2][-3000:]
    write(state, read(state) | {"events": []})
    expect(run("apps.yml", up, env=env), "homeserver-k8s", changed=True, note="up")
    assert deployments_of(state) == UP
    events = read(state)["events"]
    assert events.index("rollout nextcloud/nextcloud-db") < events.index("patch nextcloud/nextcloud 1"), events
    for check in (False, True):
        result = run("apps.yml", up, check=check, env=env)
        expect(result, "homeserver-k8s", changed=0, note=f"{'dry run of' if check else 'second'} up")
        assert "No app is down." in result[2], result[2][-3000:]

    # An app and a group; up for one of them; then the rest.
    expect(run("apps.yml", down | {"only": "jellyfin, nextcloud"}, env=env), "homeserver-k8s", changed=True)
    assert deployments_of(state) == UP | {
        "media/jellyfin": held(1),
        "nextcloud/nextcloud": held(1),
        "nextcloud/nextcloud-db": held(1),
    }
    expect(run("apps.yml", up | {"only": "jellyfin"}, env=env), "homeserver-k8s", changed=True)
    assert deployments_of(state) == UP | {"nextcloud/nextcloud": held(1), "nextcloud/nextcloud-db": held(1)}
    expect(run("apps.yml", up, env=env), "homeserver-k8s", changed=True)
    assert deployments_of(state) == UP

    # Started by hand while held: down stops it again and keeps the replicas it recorded first.
    expect(run("apps.yml", down | {"only": "homepage"}, env=env), "homeserver-k8s", changed=True)
    s = read(state)
    s["objects"]["Deployment/homepage/homepage"]["spec"]["replicas"] = 1
    write(state, s)
    expect(run("apps.yml", down | {"only": "homepage"}, env=env), "homeserver-k8s", changed=True)
    assert deployments_of(state)["homepage/homepage"] == held(2)
    expect(run("apps.yml", up, env=env), "homeserver-k8s", changed=True)
    assert deployments_of(state)["homepage/homepage"] == (2, {})

    mutations = read(state)["mutations"]
    for check in (True, False):
        rc, _, out = run("apps.yml", down | {"only": "jellyfin,plex,traefik"}, check=check, env=env)
        assert rc != 0 and "No app or group is called plex, traefik. The apps: homepage, jellyfin," in out, out[-3000:]
    assert read(state)["mutations"] == mutations


@pytest.mark.cluster
def test_app_scale_refusals_and_timeouts(tmp_path):
    """No kubeconfig, or another cluster's, is refused before the cluster is read. Pods that never stop, or an app
    that never becomes available, fail the run, and the next run picks up where it stopped."""
    extra, env = apps_vars(tmp_path), talos_env(tmp_path)
    state = Path(extra["fake_talos_state"])
    apps_state(state)
    down, up = extra | {"apps_state": "down"}, extra | {"apps_state": "up"}
    kubeconfig = Path(extra["talos_config_dir"]) / "kubeconfig"

    kubeconfig.write_text(yaml.safe_dump({"clusters": [{"cluster": {"server": "https://127.0.0.99:6443"}}]}))
    rc, _, out = run("apps.yml", down, check=True, env=env)
    assert rc != 0 and "names the server https://127.0.0.99:6443, not https://127.0.0.30:6443" in out, out[-3000:]
    kubeconfig.unlink()
    rc, _, out = run("apps.yml", down, env=env)
    assert rc != 0 and "No kubeconfig at" in out, out[-3000:]
    assert read(state)["mutations"] == 0
    apps_vars(tmp_path)

    write(state, read(state) | {"pods_linger": ["nextcloud"]})
    rc, _, out = run("apps.yml", down | {"only": "nextcloud"}, env=env)
    assert rc != 0 and "timed out waiting for the condition on pods/nextcloud-" in out, out[-3000:]
    # Its database was never touched: the wave after it did not start.
    assert deployments_of(state)["nextcloud/nextcloud-db"] == (1, {})
    write(state, read(state) | {"pods_linger": []})
    expect(run("apps.yml", down | {"only": "nextcloud"}, env=env), "homeserver-k8s", changed=True)
    assert deployments_of(state)["nextcloud/nextcloud-db"] == held(1)

    write(state, read(state) | {"never_available": ["nextcloud-db"]})
    rc, _, out = run("apps.yml", up, env=env)
    assert rc != 0 and "timed out waiting for the condition" in out, out[-3000:]
    assert deployments_of(state)["nextcloud/nextcloud"] == held(1), "nextcloud waits for its database"


@pytest.mark.cluster
def test_haos_power(tmp_path):
    """just suspend shuts Home Assistant's VM down through its own shutdown, never a forced stop, and just resume starts
    it again; each leaves a VM already as asked alone, a dry run changes nothing, a VMID that does not exist is
    refused, and a shutdown that never finishes fails the run with the VM still running."""
    extra = {"fake_pve_state": str(tmp_path / "pve.json"), "haos_mac": mac(9)}
    state = Path(extra["fake_pve_state"])
    vm_state(state, vms={"310": {"config": {"name": "homeassistant"}, "pending": {}, "status": "running"}})
    down, up = extra | {"haos_power": "stopped"}, extra | {"haos_power": "running"}

    def status():
        return read(state)["vms"]["310"]["status"]

    result = run("haos_power.yml", down, check=True)
    expect(result, "homeserver", changed=1, note="dry run of down")
    assert "Would shut down VM 310." in result[2] and status() == "running", result[2][-3000:]
    expect(run("haos_power.yml", down), "homeserver", changed=True, note="down")
    assert status() == "stopped"
    for check in (False, True):
        result = run("haos_power.yml", down, check=check)
        expect(result, "homeserver", changed=0, note=f"{'dry run of' if check else 'second'} down")
        assert "VM 310 already stopped." in result[2], result[2][-3000:]

    result = run("haos_power.yml", up, check=True)
    expect(result, "homeserver", changed=1, note="dry run of up")
    assert "Would start VM 310." in result[2] and status() == "stopped", result[2][-3000:]
    expect(run("haos_power.yml", up), "homeserver", changed=True, note="up")
    assert status() == "running"
    expect(run("haos_power.yml", up), "homeserver", changed=0, note="second up")

    rc, _, out = run("haos_power.yml", down | {"haos_vm_vmid": 311}, check=True)
    assert rc != 0 and "VM 311 does not exist on this host" in out, out[-3000:]

    s = read(state)
    s["shutdown_hangs"] = True
    write(state, s)
    rc, _, out = run("haos_power.yml", down)
    assert rc != 0 and "VM quit/powerdown failed - got timeout" in out, out[-3000:]
    assert status() == "running"
