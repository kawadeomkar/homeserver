"""Runs each role repeatedly against stateful fakes and checks that it is idempotent.

For each role: a dry run changes nothing; the first real run applies the configuration; a second
run changes nothing and does not fail; a dry run afterwards reports nothing; drift introduced behind
the role's back is corrected by one run and left alone by the next. The Proxmox role is also told
the node's own storage to check, which must change nothing and must fail on a storage that cannot hold
VM disks.

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
import fake_truenas  # noqa: E402

RECAP = re.compile(r"^(\S+)\s+:\s+ok=(\d+)\s+changed=(\d+)\s+unreachable=(\d+)\s+failed=(\d+)", re.MULTILINE)
PLAYBOOK = str(Path(sys.executable).parent / "ansible-playbook")
NAS_ADDRESS = "127.0.0.1"
PROXMOX_ADDRESS = "127.0.0.20"


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
    assert sorted(p["name"] for p in s["pools"]) == ["ephemeral", "nvme_gen3"]
    assert all(p["autotrim"]["rawvalue"] == "on" for p in s["pools"])
    assert len(s["scrubs"]) == 2
    ds = s["datasets"]
    assert ds["nvme_gen3/proxmox/vm"]["sync"]["rawvalue"] == "standard"
    assert ds["ephemeral/proxmox/vm"]["sync"]["rawvalue"] == "disabled"
    assert ds["ephemeral/proxmox"]["quota"]["rawvalue"] == "1099511627776"
    assert ds["nvme_gen3/proxmox/vm"]["recordsize"]["rawvalue"] == "65536"
    assert ds["ephemeral/proxmox"]["acltype"]["rawvalue"] == "posix"
    assert ds["ephemeral/jellyfin"]["acltype"]["rawvalue"] == "nfsv4", "an undeclared dataset was touched"
    assert sorted(sh["path"] for sh in s["shares"]) == ["/mnt/ephemeral/proxmox/vm", "/mnt/nvme_gen3/proxmox/vm"]
    assert all(sh["hosts"] == [PROXMOX_ADDRESS] and sh["maproot_user"] == "root" for sh in s["shares"])
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

    for out in outputs:
        assert fake_truenas.PRIVATE_KEY not in out, "the UI certificate's private key reached Ansible output"
        assert fake_truenas.API_KEY not in out, "the API key reached Ansible output"


def test_truenas_role_with_defaults_changes_nothing(nas):
    """A user who sets only the connection: the role reports and leaves the NAS alone."""
    extra = {k: v for k, v in nas_vars(nas).items() if k.startswith("truenas_api")}
    before = semantic(read(nas["state"]))
    result = run("truenas_defaults.yml", extra)
    expect(result, "truenas", changed=0, note="role with only its defaults")
    assert semantic(read(nas["state"])) == before


# ---------------------------------------------------------------------------------- Proxmox ---


def pve_vars(state):
    return {"fake_pve_state": str(state), "truenas_static_address": "127.0.0.10/24"}


def test_proxmox_storage_role_is_idempotent(tmp_path):
    state = tmp_path / "pve.json"
    extra = pve_vars(state)

    result = run("proxmox.yml", extra, check=True)
    expect(result, "homeserver", note="dry run")
    for sid in ("truenas-persistent", "truenas-ephemeral"):
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
    assert sorted(nfs) == ["truenas-ephemeral", "truenas-persistent"]
    for entry in nfs.values():
        assert (entry["server"], entry["content"], entry["format"], entry["options"]) == (
            "127.0.0.10",
            "images",
            "qcow2",
            "vers=4.2",
        )
        assert "disable" not in entry
    assert nfs["truenas-ephemeral"]["export"] == "/mnt/ephemeral/proxmox/vm"
    assert s["node"]["startall-onboot-delay"] == 120

    # Drift: options, content and the enabled flag changed by hand.
    nfs["truenas-persistent"].update(options="vers=3", content="images,rootdir", disable=1)
    write(state, s)
    expect(run("proxmox.yml", extra), "homeserver", changed=True, note="run after drift")
    result = run("proxmox.yml", extra)
    expect(result, "homeserver", changed=0, note=f"second run after drift (changed: {changed_tasks(result[2])})")
    entry = next(e for e in read(state)["storage"] if e["storage"] == "truenas-persistent")
    assert (entry["options"], entry["content"], "disable" in entry) == ("vers=4.2", "images", False)

    # Two content types, which the fake hands back in a different order on every other read: a comparison
    # that did not sort both sides would see drift on one of the two runs after the correction.
    two = {**extra, "proxmox_storage_content": ["images", "rootdir"]}
    expect(run("proxmox.yml", two), "homeserver", changed=True, note="run with two content types")
    for n in (1, 2):
        result = run("proxmox.yml", two)
        note = f"run {n} after two content types (changed: {changed_tasks(result[2])})"
        expect(result, "homeserver", changed=0, note=note)
    entry = next(e for e in read(state)["storage"] if e["storage"] == "truenas-persistent")
    assert sorted(entry["content"].split(",")) == ["images", "rootdir"]

    # A storage id that points somewhere else is refused, not replaced.
    s = read(state)
    next(e for e in s["storage"] if e["storage"] == "truenas-ephemeral")["export"] = "/mnt/elsewhere"
    write(state, s)
    rc, _, out = run("proxmox.yml", extra)
    assert rc != 0 and "truenas-ephemeral" in out and "Remove or rename them by hand" in out, out[-3000:]
    assert next(e for e in read(state)["storage"] if e["storage"] == "truenas-ephemeral")["export"] == "/mnt/elsewhere"


def test_proxmox_storage_role_without_a_nas(tmp_path):
    """With no NAS address, this repo's configuration adds no NFS storage, sets no boot delay, and checks local-lvm."""
    state = tmp_path / "pve.json"
    extra = {"fake_pve_state": str(state), "truenas_static_address": "", "truenas_bootstrap_address": ""}

    result = run("proxmox.yml", extra, check=True)
    expect(result, "homeserver", changed=0, note="dry run without a NAS")
    assert re.search(r'local-lvm"?:\s*"?present', result[2]), result[2][-3000:]
    assert "truenas-" not in result[2], f"an NFS storage id appears without a NAS\n{result[2][-3000:]}"
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
    assert "truenas-" not in result[2] and re.search(r'local-lvm"?:\s*"?present', result[2]), result[2][-3000:]


def test_proxmox_storage_role_needs_the_static_address(tmp_path):
    """A NAS known only by its bootstrap address counts as a NAS, but Proxmox mounts it at its static address, so
    the run is refused, naming the variable the server comes from."""
    extra = {"fake_pve_state": str(tmp_path / "pve.json"), "truenas_static_address": ""}
    rc, _, out = run("proxmox.yml", {**extra, "truenas_bootstrap_address": "127.0.0.10"}, check=True)
    expected = "No NFS server for truenas-persistent, truenas-ephemeral: set proxmox_storage_nfs_server"
    assert rc != 0 and expected in out, out[-3000:]


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
    # id, a string for the list, an id in both lists, and an NFS storage with no server.
    nfs = [{"id": "truenas-persistent", "export": "/mnt/tank/vm"}]
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
        ({"proxmox_storage_nfs": nfs}, "No NFS server for truenas-persistent: set proxmox_storage_nfs_server"),
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


def test_truenas_role_refuses_an_unpinned_certificate(nas):
    """With the wrong certificate fingerprint, the API key is never sent."""
    extra = {k: v for k, v in nas_vars(nas).items() if k.startswith("truenas_api")}
    extra["truenas_api_cert_sha256"] = "00" * 32
    extra["truenas_api_connect_wait"] = 0
    logins_before = Path(nas["calls"]).read_text().count("auth.login_with_api_key")
    rc, _, out = run("truenas_defaults.yml", extra)
    assert rc != 0 and "not the pinned one" in out and "the API key was not sent" in out, out[-3000:]
    assert Path(nas["calls"]).read_text().count("auth.login_with_api_key") == logins_before, "a login was attempted"
