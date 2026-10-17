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
    }
    return {**extra, "truenas_static_address": "127.0.0.10/24"} if nas else extra


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
    extra = {"fake_pve_state": str(state), "proxmox_storage_local": ["local-lvm"]}

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
    """With a NAS: one nightly job of the VMs local.yml names to the NAS's backup storage. A dry run on a fresh
    host passes, the storage pending; a real run without the storage is refused; drift is corrected."""
    state = tmp_path / "pve.json"
    extra = {
        "fake_pve_state": str(state),
        "truenas_static_address": "127.0.0.10/24",
        "vm_backup_extra_vmids": [300, 301],
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

    # Other VMs in local.yml: the same job, corrected to them.
    other = extra | {"vm_backup_extra_vmids": [310]}
    expect(run("proxmox_backup.yml", other), "homeserver", changed=1, note="other VMs")
    assert backup_job(state)["vmid"] == "310"
    expect(run("proxmox_backup.yml", other), "homeserver", changed=0, note="other VMs, second run")


@pytest.mark.cluster
def test_proxmox_backup_without_a_nas(tmp_path):
    """Without a NAS: no job and no read without vm_backup_storage; a directory storage that is not a checked
    mount point is refused, naming the fix; then one job, for the VMs local.yml names."""
    state = tmp_path / "pve.json"
    extra = {
        "fake_pve_state": str(state),
        "truenas_static_address": "",
        "vm_backup_extra_vmids": [300],
    }
    for check in (True, False):
        expect(run("proxmox_backup.yml", extra, check=check), "homeserver", changed=0, note="no backup storage")
    assert not state.exists() or read(state)["mutations"] == 0

    usb = {"storage": "usb-backup", "type": "dir", "path": "/mnt/usb", "content": "backup"}
    seed_storage(state, usb)
    with_usb = {**extra, "vm_backup_storage": "usb-backup", "vm_backup_extra_vmids": [300, 310]}
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
    assert (job["storage"], job["vmid"], job["notes-template"]) == ("usb-backup", "300,310", "{{guestname}}")

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
