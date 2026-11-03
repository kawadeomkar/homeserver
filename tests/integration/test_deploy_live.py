"""Every playbook run drawn live (callback_plugins/homeserver.py, filter_plugins/live_ansible.py), on a terminal.

The Proxmox host's play, tests/integration/proxmox.yml, which deploy's declaration matches by its file's name, runs on
a pseudo-terminal of its own against the fake Proxmox host, as the idempotence tests run it, with the repository's
stdout callback. What it wrote is read back through tests/vt.py's terminal: each frame redrawn in place, none
wrapping, nothing a terminal would not know, and once the run is over the summary where the region was. The log holds
the stream the default callback prints, PLAY RECAP and the summary. A refusal prints whole, a run of a playbook no
declaration names is drawn too, Ctrl-C leaves no frame behind, and -v is the stream.

just suspend and just resume run whole, the recipes and the real playbooks, in a scratch tree against the fake
Proxmox host and the fake cluster, set up as test_status.py sets them up: drawn by their declaration, the apps' waves
each a step, the report naming each app with its replicas in the summary, and the command as it was typed.
"""

import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "policy"))

import test_idempotence as idempotence  # noqa: E402
import test_status as status  # noqa: E402
from helpers import PROXMOX_ONLY_CLUSTER_LOCAL_YML, example_tree  # noqa: E402
from vt import VT  # noqa: E402

PLAYBOOK = str(Path(sys.executable).parent / "ansible-playbook")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DASH = "\u2013"
COLUMNS, ROWS = 100, 30


def environment(log, **more):
    """The idempotence tests' configuration, with the repository's stdout callback and a log of the test's own."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANSIBLE_", "NO_COLOR"))}
    return env | {
        "ANSIBLE_CONFIG": str(HERE / "ansible.cfg"),
        "ANSIBLE_STDOUT_CALLBACK": "homeserver",
        "ANSIBLE_CALLBACK_PLUGINS": str(ROOT / "callback_plugins"),
        "ANSIBLE_LOG_PATH": str(log),
        "ANSIBLE_NOCOLOR": "1",
        "TERM": "xterm-256color",
        **more,
    }


def on_a_terminal(command, env, cwd=HERE, interrupt_after=None):
    """command on a terminal of its own, COLUMNS by ROWS: its exit code and what it wrote, every control sequence
    included. interrupt_after: seconds after which the terminal's Ctrl-C reaches the run."""
    master, terminal = pty.openpty()
    fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
    proc = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=terminal, stdout=terminal, stderr=terminal, start_new_session=True
    )
    os.close(terminal)
    chunks, started, sent = [], time.monotonic(), False
    while select.select([master], [], [], 120)[0]:
        try:
            data = os.read(master, 65536)
        except OSError:  # EIO: every process that wrote to the terminal has ended
            break
        if not data:
            break
        chunks.append(data)
        if interrupt_after is not None and not sent and time.monotonic() - started >= interrupt_after:
            os.killpg(proc.pid, signal.SIGINT)
            sent = True
    os.close(master)
    return proc.wait(timeout=60), b"".join(chunks)


def proxmox(tmp_path, *args, **given):
    """The fake Proxmox host's play, on a terminal: its code, the terminal it leaves, and its log."""
    extra = idempotence.pve_vars(tmp_path) | given | {"ansible_python_interpreter": sys.executable}
    log = tmp_path / "run.log"
    command = [PLAYBOOK, str(HERE / "proxmox.yml"), "--diff", "-e", json.dumps(extra), *args]
    rc, stream = on_a_terminal(command, environment(log))
    terminal = VT(COLUMNS, ROWS)
    terminal.feed(stream)
    return rc, terminal, stream, log.read_text() if log.exists() else ""


def drawn(terminal):
    """Redrawn in place, each frame whole on its lines, and nothing written a terminal would not know."""
    assert terminal.ups, "never redrawn in place"
    assert terminal.frame_wraps == 0, f"{terminal.frame_wraps} lines of a frame wrapped"
    assert not terminal.unknown, terminal.unknown


def left_behind(lines):
    """Lines a frame would leave if it were not erased: a spinner's, or the footer's."""
    return [line for line in lines if any(g in line for g in SPINNER) or re.search(r" · (stage|step) \d+ of \d+", line)]


def test_a_deploy_of_the_proxmox_host_is_drawn_and_ends_in_its_summary(tmp_path):
    rc, terminal, stream, log = proxmox(tmp_path)
    assert rc == 0, stream.decode(errors="replace")[-4000:]
    drawn(terminal)
    screen = terminal.transcript()
    assert not left_behind(screen), screen
    assert not any(line.startswith(("TASK [", "PLAY [", "ok: [", "changed: [")) for line in screen), screen
    head = next(line for line in screen if line.startswith("homeserver  deploy proxmox"))
    assert " · done in " in head, head
    # The run's own words above: the reports, after their step.
    assert any(line.startswith("  packages ") for line in screen), screen
    assert any(line.startswith("  storage ") for line in screen), screen
    assert screen[-1] == "exit 0 · for the whole picture: just status", screen
    assert any(line.startswith("STEPS") for line in screen) and any(line.startswith(" ✓ packages") for line in screen)
    # A real run's diffs go to the log alone.
    assert not any(line.startswith("+++ after") for line in screen), screen
    assert terminal.titles and terminal.titles[-1] == "deploy proxmox · done", terminal.titles[-3:]
    # The log: the stream, the recap and the summary.
    assert "TASK [proxmox_apt : " in log and "PLAY RECAP" in log and "+++ after" in log, log[-3000:]
    assert log.index("PLAY RECAP") < log.index("exit 0 · for the whole picture: just status")
    assert "\x1b" not in log, "the log is plain"


def test_a_dry_run_prints_its_diffs_above(tmp_path):
    rc, terminal, stream, _log = proxmox(tmp_path, "--check")
    assert rc == 0, stream.decode(errors="replace")[-4000:]
    drawn(terminal)
    screen = terminal.transcript()
    assert "dry run, nothing is changed" in stream.decode(errors="replace")
    head = next(line for line in screen if line.startswith("homeserver  check proxmox"))
    assert " · dry run · done in " in head, head
    assert any(line.startswith("+++ after") for line in screen), "a dry run's diffs print above"
    assert any("would change" in line for line in screen)


def test_a_refusal_prints_whole_and_the_run_ends_failed(tmp_path):
    rc, terminal, stream, _log = proxmox(tmp_path, proxmox_apt_disabled=["pve-enterprise", ""])
    assert rc == 2, stream.decode(errors="replace")[-4000:]
    drawn(terminal)
    screen = terminal.transcript()
    assert not left_behind(screen), screen
    head = next(line for line in screen if line.startswith("✗ "))
    assert head.startswith("✗ packages · homeserver: ") and head.endswith("refused"), head
    # The message whole, wrapped to the width: one paragraph across the lines it takes.
    assert "none may be empty, and none may contain a slash." in " ".join(" ".join(screen).split()), screen
    assert any(re.search(r"roles/proxmox_apt/tasks/\w+\.yml:\d+ · log line \d+$", line) for line in screen), screen
    summary = next(line for line in screen if line.startswith("homeserver  deploy proxmox"))
    assert "failed after" in summary and "at packages" in summary, summary
    assert screen[-1] == "exit 2 · for the whole picture: just status"
    assert terminal.bells, "a failed run rings the bell"


def test_ctrl_c_leaves_no_frame_behind(tmp_path):
    extra = idempotence.pve_vars(tmp_path) | {"ansible_python_interpreter": sys.executable}
    log = tmp_path / "run.log"
    command = [PLAYBOOK, str(HERE / "proxmox.yml"), "--diff", "-e", json.dumps(extra)]
    rc, stream = on_a_terminal(command, environment(log), interrupt_after=3)
    assert rc != 0, stream.decode(errors="replace")[-3000:]
    terminal = VT(COLUMNS, ROWS)
    terminal.feed(stream)
    screen = terminal.transcript()
    assert not left_behind(screen), screen
    assert terminal.titles and terminal.titles[-1] == "deploy proxmox · stopped", terminal.titles[-3:]


def test_a_playbook_no_declaration_names_is_drawn_a_stage_per_play(tmp_path):
    (tmp_path / "chores.yml").write_text(
        "- name: First chores\n  hosts: localhost\n  gather_facts: false\n  tasks:\n"
        "    - name: Say hello\n      ansible.builtin.debug:\n        msg: hello from the first play\n"
        "- name: Second chores\n  hosts: localhost\n  gather_facts: false\n  tasks:\n"
        "    - name: Wait a moment\n      ansible.builtin.command: sleep 1\n      changed_when: false\n"
    )
    log = tmp_path / "run.log"
    command = [PLAYBOOK, "-i", "localhost,", "-c", "local", "chores.yml"]
    command += ["-e", json.dumps({"ansible_python_interpreter": sys.executable})]
    rc, stream = on_a_terminal(command, environment(log), cwd=tmp_path)
    assert rc == 0, stream.decode(errors="replace")[-3000:]
    terminal = VT(COLUMNS, ROWS)
    terminal.feed(stream)
    drawn(terminal)
    screen = terminal.transcript()
    assert not left_behind(screen), screen
    assert "  First chores   hello from the first play" in screen, screen
    assert any(line.startswith(" ✓ First chores") for line in screen) and any(
        line.startswith(" ✓ Second chores") for line in screen
    ), screen
    assert screen[-1] == "exit 0 · for the whole picture: just status"


def test_at_v_the_run_is_the_stream(tmp_path):
    rc, terminal, stream, _log = proxmox(tmp_path, "-v")
    assert rc == 0, stream.decode(errors="replace")[-3000:]
    assert b"\x1b[?2026h" not in stream and not terminal.ups, "drawn at -v"
    screen = terminal.transcript()
    assert any(line.startswith("TASK [proxmox_apt : ") for line in screen) and any(
        line.startswith("PLAY RECAP") for line in screen
    )


def scale_tree(tmp_path):
    """The repository's playbooks, roles, plugins, justfile and the scripts just's playbook recipes run, with
    test_status.py's Proxmox-only cluster and Home Assistant: the host's state, the cluster's and its credentials."""
    root = example_tree(tmp_path / "tree", PROXMOX_ONLY_CLUSTER_LOCAL_YML)
    shutil.copytree(ROOT / "callback_plugins", root / "callback_plugins")
    shutil.copy(ROOT / "justfile", root)
    (root / "scripts" / "init").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "run_log.py", root / "scripts")
    shutil.copy(ROOT / "scripts" / "init" / "setups.py", root / "scripts" / "init")
    (tmp_path / "home").mkdir()
    status.credentials(status.talos_dir(tmp_path))
    status.seed_cluster(tmp_path / "talos.json")
    status.seed(tmp_path / "pve.json")
    return root


def just_on_a_terminal(root, *args):
    """just with args in the tree, on a terminal, with the repository's stdout callback, this test's inventory and
    variables after --: its code, the terminal it leaves, and what it wrote."""
    state = root.parent / "pve.json"
    env = status.environment(root, state) | {
        "ANSIBLE_STDOUT_CALLBACK": "homeserver",
        "ANSIBLE_CALLBACK_PLUGINS": str(root / "callback_plugins"),
        "ANSIBLE_NOCOLOR": "1",
        "TERM": "xterm-256color",
        "VENV": sys.prefix,
    }
    command = ["just", *args, "--", "-i", "inventory.example", "-e", status.extra_vars(root)]
    rc, stream = on_a_terminal(command, env, cwd=root)
    terminal = VT(COLUMNS, ROWS)
    terminal.feed(stream)
    return rc, terminal, stream.decode(errors="replace")


def replicas(root):
    """Each app's replicas in the fake cluster, by namespace/name."""
    objects = json.loads((root.parent / "talos.json").read_text())["objects"]
    return {
        k.removeprefix("Deployment/"): o["spec"].get("replicas", 1)
        for k, o in objects.items()
        if k.startswith("Deployment/")
    }


@pytest.mark.cluster
def test_suspend_and_resume_are_drawn_by_their_declaration(tmp_path):
    root = scale_tree(tmp_path)
    rc, terminal, stream = just_on_a_terminal(root, "suspend")
    assert rc == 0, stream[-4000:]
    drawn(terminal)
    screen = terminal.transcript()
    assert not left_behind(screen), screen
    assert "Setup: this checkout's own files" in screen and any(line.startswith("Logging to logs/") for line in screen)
    assert replicas(root)["homepage/homepage"] == 0
    head = next(line for line in screen if line.startswith("homeserver  suspend"))
    assert " · done in " in head, head
    # The apps' waves were drawn as steps, the databases after the apps.
    assert "○ databases" in stream and re.search(f"[{SPINNER}] databases", stream), stream[-4000:]
    assert re.search(f"[{SPINNER}] plan", stream), stream[-4000:]
    # The summary names each wave, after the plan when that took long enough to name.
    said = " ".join(" ".join(screen).split())
    assert re.search(r"apps [\d.]+ s, 1 changed · databases [\d.]+ s, 1 changed", said), screen
    # The report whole in the summary: each app with the replicas it comes back with, the database last.
    at = next(i for i, line in enumerate(screen) if line.startswith("APPS "))
    report = " ".join(" ".join(screen[at : at + 6]).split())
    assert "Stopped 4 apps, to come back with the replicas in brackets" in report, screen[at:]
    assert "homepage/homepage (2)" in report and "then nextcloud/nextcloud-db (1)" in report, screen[at:]
    assert "Already down: media/sonarr." in report, screen[at:]
    assert any(line.startswith(" ✓ Home Assistant") for line in screen), screen
    assert not any("cluster VMs" in line for line in screen), "drawn only with --all"
    assert screen[-1] == "exit 0 · for the whole picture: just status", screen
    assert terminal.titles and terminal.titles[-1] == "suspend · done", terminal.titles[-3:]
    log = next((root / "logs").glob("*-suspend.log")).read_text()
    assert "TASK [app_scale : " in log and "PLAY RECAP" in log and "\x1b" not in log, log[-3000:]

    rc, terminal, stream = just_on_a_terminal(root, "resume")
    assert rc == 0, stream[-4000:]
    drawn(terminal)
    screen = terminal.transcript()
    assert replicas(root)["homepage/homepage"] == 2
    at = next(i for i, line in enumerate(screen) if line.startswith("APPS "))
    report = " ".join(" ".join(screen[at : at + 6]).split())
    assert "Started 5 apps with the replicas in brackets" in report and "nextcloud/nextcloud-db (1)" in report
    assert report.index("nextcloud/nextcloud-db (1)") < report.index("then homepage/homepage (2)"), report
    assert terminal.titles[-1] == "resume · done", terminal.titles[-3:]

    # A dry run of some apps: the command as typed, and Home Assistant, which --only leaves alone, not in this run.
    rc, terminal, stream = just_on_a_terminal(root, "suspend", "--check", "--only", "jellyfin")
    assert rc == 0, stream[-4000:]
    drawn(terminal)
    screen = terminal.transcript()
    head = next(line for line in screen if line.startswith("homeserver  suspend"))
    assert head.startswith("homeserver  suspend --check --only jellyfin · dry run · done in "), head
    assert f" {DASH} Home Assistant not in this run" in screen, screen
    assert replicas(root)["media/jellyfin"] == 1, "a dry run changes nothing"
