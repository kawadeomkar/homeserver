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

Needs the project virtualenv and openssl. Run: make test-idempotence
"""

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fake_pve  # noqa: E402
import fake_truenas  # noqa: E402

RECAP = re.compile(r"^(\S+)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", re.MULTILINE)
PLAYBOOK = str(Path(sys.executable).parent / "ansible-playbook")
NAS_ADDRESS = "127.0.0.1"
PROXMOX_ADDRESS = "127.0.0.20"
# The cluster's nodes, with a NAS.
NODE_ADDRESSES = {"homeserver-cp-1": "127.0.0.30", "homeserver-worker-1": "127.0.0.31"}
# Both nodes at the least the settings check allows, for a small host without a NAS: 40 GiB of disks between them.
SMALL_SIZES = {
    "homeserver-cp-1": {"memory_mib": 2048, "system_disk_gib": 10},
    "homeserver-worker-1": {
        "cores": 2,
        "memory_mib": 4096,
        "system_disk_gib": 10,
        "data_disk_gib": 10,
        "media_disk_gib": 10,
    },
}
# Sentinels that must never reach Ansible's output.
MAIL_PASSWORD = "mail-password-sentinel-6c1f"
ROTATED_MAIL_PASSWORD = "mail-password-sentinel-rotated-91ad"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run(playbook, extra, check=False):
    """Run a test playbook; return (return code, {host: stats}, combined output)."""
    # ANSIBLE_FORCE_COLOR overrides ANSIBLE_NOCOLOR, and a coloured PLAY RECAP does not match RECAP.
    env = {k: v for k, v in os.environ.items() if k != "ANSIBLE_FORCE_COLOR"}
    env |= {"ANSIBLE_CONFIG": str(HERE / "ansible.cfg"), "ANSIBLE_NOCOLOR": "1"}
    cmd = [
        PLAYBOOK,
        str(HERE / playbook),
        "--diff",
        "-e",
        json.dumps({"ansible_python_interpreter": sys.executable, **extra}),
    ]
    if check:
        cmd.append("--check")
    proc = subprocess.run(cmd, env=env, cwd=HERE, capture_output=True, text=True, stdin=subprocess.DEVNULL)
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
    assert ds["ephemeral/jellyfin"]["acltype"]["rawvalue"] == "nfsv4", "an undeclared dataset was touched"
    assert sorted(sh["path"] for sh in s["shares"]) == [
        "/mnt/ephemeral/proxmox/claude-on-proxmox",
        "/mnt/ephemeral/proxmox/vm",
        "/mnt/persistent/proxmox/backup",
        "/mnt/persistent/proxmox/vm",
    ]
    assert all(sh["hosts"] == [PROXMOX_ADDRESS] and sh["maproot_user"] == "root" for sh in s["shares"])
    # Snapshot tasks: the persistent VMs' disks hourly and daily, nothing on the ephemeral pool.
    tasks = {(t["dataset"], t["naming_schema"].split("-")[0]): t for t in s["snapshot_tasks"]}
    assert sorted(tasks) == sorted([("persistent/proxmox/vm", "hourly"), ("persistent/proxmox/vm", "daily")])
    assert (
        tasks[("persistent/proxmox/vm", "daily")]["lifetime_value"],
        tasks[("persistent/proxmox/vm", "daily")]["lifetime_unit"],
    ) == (7, "DAY")
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
    assert all(sh["hosts"] == [PROXMOX_ADDRESS] for sh in s["shares"])
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
    """With a NAS: a dry run on a fresh host passes, the persistent storage pending; a real run without that storage
    is refused; then both VMs are created, started and left alone; drift is corrected; a shrink is refused."""
    extra = cluster_vars(tmp_path)
    state = Path(extra["fake_pve_state"])
    vm_state(state)

    result = run("kubernetes.yml", extra, check=True)
    expect(result, "homeserver", changed=True, note="dry run on a fresh host")
    out = result[2]
    assert re.search(r'storage pending: make proxmox adds it: run that first"?:\s*\[\s*"homeserver-persistent"', out), (
        out[-3000:]
    )
    assert re.search(r'homeserver-worker-1"?:\s*"?create', out) and "Would download: talos-v1.14.2" in out, out[-3000:]
    assert read(state)["mutations"] == 0, "the dry run changed the host"

    rc, _, out = run("kubernetes.yml", extra)
    assert rc != 0 and "homeserver-persistent: does not exist (make proxmox adds it: run that first)" in out, out[
        -3000:
    ]
    assert vms_of(state) == {}

    seed_storage(state, PERSISTENT_NFS)
    expect(run("kubernetes.yml", extra), "homeserver", changed=True, note="first run")
    result = run("kubernetes.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run (changed: {changed_tasks(result[2])})")
    expect(run("kubernetes.yml", extra, check=True), "homeserver", changed=0, note="dry run after applying")
    assert f'"homeserver-worker-1": "{mac(2).upper()}"' in result[2], "the MACs are not reported"

    vms = vms_of(state)
    assert sorted(vms) == ["300", "301"] and all(vm["status"] == "running" for vm in vms.values())
    worker = vms["301"]["config"]
    assert worker["scsi0"].startswith("local-lvm:vm-301-disk-1,") and "size=64G" in worker["scsi0"]
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
    assert worker["pending"]["boot"] == "order=scsi0;ide2" and worker["config"]["scsi0"].startswith("local-lvm:")
    assert re.search(r'"301":\s*\[[^\]]*"boot"', result[2]), result[2][-3000:]
    expect(run("kubernetes.yml", extra), "homeserver", changed=0, note="a system disk replaced, again")

    restart_all(state)

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
    # A 54 GiB pool holding another VM's 40 GiB disk: the 40 GiB more the two nodes need do not fit.
    vm_state(state, local_lvm_gib=54)
    other = {"volid": "local-lvm:vm-200-disk-0", "content": "images", "vmid": 200, "size": 40 * 2**30, "format": "raw"}
    s = read(state)
    s["content"] = {"local-lvm": [other]}
    write(state, s)
    for check in (True, False):
        rc, _, out = run("kubernetes.yml", extra, check=check)
        assert (
            rc != 0 and "local-lvm: its disks would be provisioned at 80 GiB (40 GiB there now, 40 GiB more" in out
        ), out[-3000:]
        assert "more than its 54 GiB" in out, out[-3000:]
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
    assert rc != 0 and "provisioned at 80 GiB (40 GiB there now, 40 GiB more" in out, out[-3000:]

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

    # Growing a disk counts against the pool as well: 40 GiB there, 15 GiB more does not fit in 54.
    worker = SMALL_SIZES["homeserver-worker-1"] | {"data_disk_gib": 25}
    grown = extra | {"kubernetes_node_sizes": SMALL_SIZES | {"homeserver-worker-1": worker}}
    mutations = read(state)["mutations"]
    for check in (True, False):
        rc, _, out = run("kubernetes.yml", grown, check=check)
        assert rc != 0 and "provisioned at 55 GiB (40 GiB there now, 15 GiB more" in out, out[-3000:]
    assert read(state)["mutations"] == mutations

    # The VMs' disk storage disabled: refused, naming it.
    s = read(state)
    next(e for e in s["storage"] if e["storage"] == "local-lvm")["disable"] = 1
    write(state, s)
    rc, _, out = run("kubernetes.yml", extra, check=True)
    assert rc != 0 and "local-lvm: is disabled (proxmox_vm_vms)" in out, out[-3000:]


@pytest.mark.cluster
@pytest.mark.parametrize("built_with_nas", [True, False], ids=["nas-to-local", "local-to-nas"])
def test_a_built_cluster_cannot_switch_setups(tmp_path, built_with_nas):
    """Both setups build the same two VMs, and only the worker's disks live elsewhere. With local.yml rewritten for
    the other setup, the cluster's play refuses before changing anything, in a dry run too, because the worker's data
    disk is on the other storage: which is why the worker always has a data disk. The backup job keeps both VMs
    either way. Put back, nothing changes."""
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
    seed_storage(state, PERSISTENT_NFS, BACKUP_NFS, usb_storage)
    usb = {"vm_backup_storage": "usb-backup"}
    expect(run("kubernetes.yml", built), "homeserver", changed=True, note="build")
    expect(run("proxmox_backup.yml", built | usb), "homeserver", changed=1, note="backup job")
    mutations = read(state)["mutations"]
    assert backup_job(state)["vmid"] == "300,301"

    on, wanted = ("homeserver-persistent", "local-lvm") if built_with_nas else ("local-lvm", "homeserver-persistent")
    for check in (True, False):
        rc, _, out = run("kubernetes.yml", switched | usb, check=check)
        assert rc != 0 and f"VM 301's scsi1 is on {on}, configured on {wanted}: moving it is by hand" in out, out[
            -3000:
        ]
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
    seed_storage(state, PERSISTENT_NFS)
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
        assert "scripts/runbooks/vm-restore.md" in out and "proxmox_vm_ignore_existing_volumes=true'" in out
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
    assert worker["efidisk0"].startswith("local-lvm:vm-301-disk-1,") and worker["scsi0"].startswith(
        "local-lvm:vm-301-disk-2,"
    )

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
    the boot disk, so starting them by hand fails (scripts/runbooks/vm-restore.md). make kubernetes downloads the ISO
    again and then starts them, creating nothing."""
    extra = cluster_vars(tmp_path)
    state = Path(extra["fake_pve_state"])
    vm_state(state)
    seed_storage(state, PERSISTENT_NFS)
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
    seed_storage(state, PERSISTENT_NFS)
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
