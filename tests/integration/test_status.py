"""Runs make status, the real status.yml with its callback and make's exit codes, against the fake Proxmox host.

The playbook, roles, group_vars and Makefile are copied into a scratch tree with a local.yml of a Proxmox-only setup
with a cluster and Home Assistant, as the policy tests do, and the Proxmox host is reached locally with the fake's
commands first on PATH. Checked: the report's file and text, the callback's output (one line on what was read, then
the report, and no task banners), that nothing on the host changed, the exit codes (make's own, 0 or 2 as for any
failed recipe, and the report's, 1 for a problem, 2 for a host that could not be read, 3 for a refused run, on the
report's last line and in make's "Error N"), JSON=1, and a host that does not answer at all.

Run: make test-idempotence
"""

import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tests" / "policy"))
import fake_pve  # noqa: E402
from helpers import CLUSTER_VARS, PROXMOX_ONLY_CLUSTER_LOCAL_YML, example_tree  # noqa: E402

GIB = 2**30
BIN = Path(sys.executable).parent
HAOS_FSINFO = [
    {"mountpoint": "/mnt/data", "type": "ext4", "used-bytes": 5 * GIB, "total-bytes": 30 * GIB, "disk": []},
    {"mountpoint": "/", "type": "erofs", "used-bytes": GIB // 4, "total-bytes": GIB // 4, "disk": []},
]


def vm(name, cores, memory_mib, status="running", **more):
    return {"config": {"name": name, "cores": cores, "memory": memory_mib, **more}, "pending": {}, "status": status}


def seed(path, **more):
    """A Proxmox-only host as this setup leaves it: the two nodes and Home Assistant running, another project's VM
    and a template beside them, a restart and an upgrade waiting, and Home Assistant's guest agent answering."""
    state = fake_pve.fresh_state()
    state["storage_total"] = {"local-lvm": 200 * GIB}
    state["vms"] = {
        "300": vm("homeserver-cp-1", 2, 4096),
        "301": vm("homeserver-worker-1", 4, 12288),
        "310": vm("homeassistant", 2, 4096),
        "9000": vm("ubuntu-template", 2, 2048, status="stopped", template=1),
        "9001": vm("another-project", 2, 4096),
    }
    state["vms"]["301"]["pending"] = {"memory": 16384}
    state["content"] = {
        "local-lvm": [
            {"volid": f"local-lvm:vm-{vmid}-disk-{n}", "vmid": vmid, "content": "images", "size": size * GIB}
            for vmid, n, size in [(300, 0, 32), (301, 0, 32), (301, 1, 20), (301, 2, 100), (310, 0, 32)]
        ]
    }
    state["guest_fsinfo"] = {"310": HAOS_FSINFO}
    state["guest_osinfo"] = {"310": {"id": "haos", "pretty-name": "Home Assistant OS 18.3", "version-id": "18.3"}}
    state["usage"] = {"301": {"cpu": 0.42, "mem": 9 * GIB, "uptime": 5 * 86400}}
    state["packages"]["lists"] = dict(state["packages"]["repository"])
    state.update(more)
    path.write_text(json.dumps(state))
    return state


@pytest.fixture
def tree(tmp_path):
    """The repository's playbooks, roles, group_vars, callback and Makefile, with a Proxmox-only cluster's local.yml."""
    root = example_tree(tmp_path / "tree", PROXMOX_ONLY_CLUSTER_LOCAL_YML)
    shutil.copytree(ROOT / "callback_plugins", root / "callback_plugins")
    shutil.copy(ROOT / "Makefile", root)
    (root / "run").mkdir()
    return root


def environment(tree, state, fake=True, **more):
    path = f"{HERE / 'fake_pve_bin'}:{BIN}:{os.environ['PATH']}" if fake else f"{BIN}:{os.environ['PATH']}"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "STATUS_", "NO_COLOR"))}
    return env | {
        "ANSIBLE_CONFIG": str(tree / "ansible.cfg"),
        "ANSIBLE_STDOUT_CALLBACK": "homeserver_status",
        "PATH": path,
        "FAKE_PVE_STATE": str(state),
        "FAKE_PVE_PYTHON": sys.executable,
        **more,
    }


def extra_vars(tree, local=True, **more):
    """The variables a run needs here: the local connection, unless the inventory gives one, this Python, the
    placeholders' values, and the reboot flag in the tree."""
    return json.dumps(
        {
            **({"ansible_connection": "local"} if local else {}),
            "ansible_python_interpreter": sys.executable,
            "status_reboot_required_file": str(tree / "run" / "reboot-required"),
            **CLUSTER_VARS,
            **more,
        }
    )


def playbook(tree, state, *args, **env):
    cmd = [str(BIN / "ansible-playbook"), "-i", "inventory.example", "status.yml", "--check", *args]
    proc = subprocess.run(
        cmd, cwd=tree, env=environment(tree, state, **env), capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    return proc.returncode, proc.stdout, proc.stderr


def make_status(tree, state, *args, fake=True, inventory="inventory.example", local=True):
    """make status in the tree, with this test's inventory and variables passed through ANSIBLE_ARGS."""
    ansible_args = f"-i {inventory} -e '{extra_vars(tree, local)}'"
    cmd = ["make", "--no-print-directory", "status", f"VENV={sys.prefix}", f"ANSIBLE_ARGS={ansible_args}", *args]
    env = environment(tree, state, fake=fake)
    env.pop("ANSIBLE_STDOUT_CALLBACK")
    proc = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout, proc.stderr


def report_of(tree):
    return json.loads((tree / ".cache" / "status" / "report.json").read_text())


def make_failed(err, code):
    """Whether make said its status recipe failed with `code`: "[status] Error 2" from GNU make 3.81 (macOS's),
    "[Makefile:415: status] Error 2" from 4.x (Linux's)."""
    return re.search(rf"\[(?:[^]\s]+:\d+: )?status\] Error {code}\b", err) is not None


def test_the_report_of_a_proxmox_only_host(tree, tmp_path):
    state = tmp_path / "pve.json"
    before = seed(state)
    (tree / "run" / "reboot-required").write_text("*** System restart required ***\n")
    rc, out, err = playbook(tree, state, "-e", extra_vars(tree))
    assert rc == 0, out + err

    # Nothing on the host changed: every command was a read.
    after = json.loads(state.read_text())
    assert after["mutations"] == before["mutations"] and after["vms"] == before["vms"]

    report = report_of(tree)
    assert stat.S_IMODE((tree / ".cache" / "status" / "report.json").stat().st_mode) == 0o600
    assert report["setup"] == {"nas": False, "cluster": True, "home_assistant": True}
    assert report["exit"] == 0, report["attention"]
    assert [(a["subject"], a["command"]) for a in report["attention"]] == [
        ("reboot", "reboot it at a quiet time"),
        ("upgrades", "make proxmox"),
        ("restart", "qm reboot 301"),
        # The disks provisioned on local-lvm add up to more than it holds, as on a host run with overcommit allowed.
        ("local-lvm", "keep its use well below 100% (pvesm status)"),
    ]
    vms = {v["vmid"]: v for v in report["proxmox"]["vms"]}
    assert sorted(vms) == [300, 301, 310]
    assert vms[301]["pending"] == ["memory"] and vms[301]["cpu"] == 0.42
    assert vms[310]["filesystems"] == [{"mount": "/mnt/data", "used": 5 * GIB, "total": 30 * GIB}]
    assert vms[310]["os"] == "Home Assistant OS 18.3" and vms[300]["filesystems"] == []
    others = {"vmids": [9001], "templates": [9000], "running": 1, "cores": 2, "memory": 4 * GIB}
    assert report["proxmox"]["others"] == others
    budget = next(s for s in report["proxmox"]["storages"] if s["id"] == "local-lvm")["budget"]
    assert budget == {"provisioned": 216 * GIB, "limit": 200 * GIB}

    # The callback: what was read, then the report, and nothing of Ansible's own.
    lines = out.splitlines()
    assert lines[0].startswith("reading  proxmox ok "), out
    assert "TASK [" not in out and "PLAY RECAP" not in out and "\033[" not in out, out
    assert "VMS  pve · Proxmox VE 9.2.2-1 · kernel 7.0.2-6-pve" in out
    assert "+ 1 other VM (9001), 2 cores, 4.0 GiB, 1 running and 1 template (9000), not this repo's" in out
    assert "disks 216 of 200 GiB budgeted" in out
    assert out.rstrip().endswith("make status TYPE=vms|storage · JSON=1"), out
    assert max(len(line) for line in lines) <= 100, out


def test_make_status_exits_with_the_reports_code(tree, tmp_path):
    """make exits 0 when nothing is wrong, and otherwise 2, as for any failed recipe; the report's own code ends its
    last line and is the "Error N" make names."""
    state = tmp_path / "pve.json"
    seed(state)
    rc, out, err = make_status(tree, state)
    assert rc == 0, out + err
    assert out.startswith("reading  proxmox ok ") and "exit 0 · nothing is wrong" in out, out

    # A storage that is not available is a problem: exit 1.
    seed(state, storage_total={"local-lvm": 200 * GIB, "local": 64 * GIB}, inactive=["local"])
    rc, out, err = make_status(tree, state)
    assert rc == 2 and "exit 1 · 1 problem" in out and make_failed(err, 1), out + err
    assert "the storage is unknown" in out

    # A host whose commands are not there could not be read: exit 2.
    rc, out, err = make_status(tree, state, fake=False)
    assert rc == 2 and "exit 2 · 1 source not read" in out and make_failed(err, 2), out + err
    assert "could not be read: pvesh get /cluster/resources" in out

    # A section that does not exist ends the run with no report: exit 3, and why.
    rc, out, err = make_status(tree, state, "TYPE=vms,apps")
    assert rc == 2 and make_failed(err, 3), out + err
    assert "make status has no section apps" in out + err and "the run failed" in err
    assert "ATTENTION" not in out and not (tree / ".cache" / "status" / "report.json").exists()


def test_json_prints_the_report_alone(tree, tmp_path):
    state = tmp_path / "pve.json"
    seed(state)
    rc, out, err = make_status(tree, state, "JSON=1", "TYPE=storage")
    assert rc == 0, out + err
    report = json.loads(out)
    assert report["types"] == ["storage"] and report == report_of(tree)
    assert "vms" in report["proxmox"] and [s["id"] for s in report["proxmox"]["storages"]] == ["local", "local-lvm"]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_a_host_that_does_not_answer(tree, tmp_path):
    """A Proxmox host that refuses the connection is a report that says so, exit 2, not a failed run."""
    state = tmp_path / "pve.json"
    seed(state)
    # An inventory of its own: given with -e, the connection would apply to the report's own play on localhost too.
    (tree / "unreachable.ini").write_text(
        f"[proxmox]\nhomeserver ansible_connection=ssh ansible_host=127.0.0.1 ansible_port={free_port()}\n"
        "[nas]\ntruenas\n[kubernetes]\nhomeserver-k8s\n"
    )
    rc, out, err = make_status(tree, state, inventory="unreachable.ini", local=False)
    assert rc == 2 and make_failed(err, 2), out + err
    assert "reading  proxmox did not answer" in out
    assert "the Proxmox host could not be read: " in out and "Connection refused" in out and "VMS" not in out
    assert report_of(tree)["exit"] == 2
