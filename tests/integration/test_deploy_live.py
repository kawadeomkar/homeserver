"""Every playbook run drawn live (callback_plugins/homeserver.py, filter_plugins/live_ansible.py), on a terminal.

The Proxmox host's play, tests/integration/proxmox.yml, which deploy's declaration matches by its file's name, runs on
a pseudo-terminal of its own against the fake Proxmox host, as the idempotence tests run it, with the repository's
stdout callback. What it wrote is read back through tests/vt.py's terminal: each frame redrawn in place, none
wrapping, nothing a terminal would not know, and once the run is over the summary where the region was. The log holds
the stream the default callback prints, PLAY RECAP and the summary. A refusal prints whole, a run of a playbook no
declaration names is drawn too, Ctrl-C leaves no frame behind, and -v is the stream.
"""

import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tests"))

import test_idempotence as idempotence  # noqa: E402
from vt import VT  # noqa: E402

PLAYBOOK = str(Path(sys.executable).parent / "ansible-playbook")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
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
