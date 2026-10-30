"""just lint and just fix drawn live (scripts/live.py, filter_plugins/live_commands.py): each type's command a step of
one run, on a pseudo-terminal, read back through tests/vt.py's terminal.

scripts/live.py runs in a scratch tree of its own, so its logs go there, with a fake just whose recipes print and exit
as each test needs: every frame redrawn in place, none wrapping, a failed type's last lines whole above with where the
log has the rest, the summary, every line the commands printed in the log and none of the run's log in their
environment, Ctrl-C sent on to the command, and, off a terminal, every type printing as it runs. Then the justfile's
own lint and fix, in a scratch tree with real files: drawn at a terminal, the stream anywhere else.
"""

import fcntl
import os
import pty
import re
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from vt import VT  # noqa: E402

COLUMNS, ROWS = 100, 30
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# A just whose recipes each print and exit as a test needs: just --justfile <file> <recipe>.
FAKE_JUST = """\
import os, signal, sys, time
recipe = sys.argv[-1]
if recipe == "lint-good":
    for n in range(1, 4):
        print(f"good {n}")
elif recipe == "lint-bad":
    for n in range(1, 51):
        print(f"bad line {n}")
    sys.exit(3)
elif recipe == "lint-env":
    print("log", os.environ.get("ANSIBLE_LOG_PATH", "none"), "colour", os.environ.get("NO_COLOR", "on"))
    print("stdin", os.path.realpath("/proc/self/fd/0") if os.path.exists("/proc/self/fd/0") else "unknown")
elif recipe == "lint-slow":
    def stop(*_):
        print("slow got INT", flush=True)
        sys.exit(130)
    signal.signal(signal.SIGINT, stop)
    print("slow started", flush=True)
    time.sleep(60)
"""


def environment(**more):
    """This environment without what the just running the tests exports, or CI's switch, which keeps a run plain."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ANSIBLE_", "JUST_", "NO_COLOR")) and k not in ("VENV", "VIRTUAL_ENV", "CI")
    }
    return env | {"ANSIBLE_NOCOLOR": "1", "TERM": "xterm-256color", **more}


def on_a_terminal(command, env, cwd, interrupt_when=None):
    """command on a terminal of its own: its exit code and what it wrote. interrupt_when: the text after which the
    terminal's Ctrl-C reaches the run."""
    master, terminal = pty.openpty()
    fcntl.ioctl(terminal, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLUMNS, 0, 0))
    proc = subprocess.Popen(
        command, cwd=cwd, env=env, stdin=terminal, stdout=terminal, stderr=terminal, start_new_session=True
    )
    os.close(terminal)
    chunks, sent = [], False
    while select.select([master], [], [], 120)[0]:
        try:
            data = os.read(master, 65536)
        except OSError:  # EIO: every process that wrote to the terminal has ended
            break
        if not data:
            break
        chunks.append(data)
        if interrupt_when and not sent and interrupt_when(b"".join(chunks)):
            time.sleep(0.5)
            os.killpg(proc.pid, signal.SIGINT)
            sent = True
    os.close(master)
    return proc.wait(timeout=60), b"".join(chunks)


def tree(tmp_path):
    """scripts/live.py and what it loads, copied into a tree of their own, with a fake just beside them."""
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    shutil.copytree(ROOT / "filter_plugins", root / "filter_plugins", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("live.py", "run_log.py"):
        shutil.copy(ROOT / "scripts" / name, root / "scripts" / name)
    (root / "ansible.cfg").write_text("[defaults]\n")
    (root / "just.py").write_text(FAKE_JUST)
    return root


def live(root, *types, interrupt_when=None, terminal=True):
    command = [sys.executable, "scripts/live.py", "--just", str(root / "fake-just")]
    command += ["--justfile", "justfile", "--title", "lint " + " ".join(types), "lint", *types]
    (root / "fake-just").write_text(f'#!/bin/sh\nexec {sys.executable} {root / "just.py"} "$@"\n')
    (root / "fake-just").chmod(0o755)
    if not terminal:
        proc = subprocess.run(command, cwd=root, env=environment(), capture_output=True, text=True)
        return proc.returncode, proc.stdout, proc.stderr
    rc, stream = on_a_terminal(command, environment(), root, interrupt_when)
    screen = VT(COLUMNS, ROWS)
    screen.feed(stream)
    return rc, screen, stream.decode(errors="replace")


def drawn(screen):
    assert screen.ups, "never redrawn in place"
    assert screen.frame_wraps == 0, f"{screen.frame_wraps} lines of a frame wrapped"
    assert not screen.unknown, screen.unknown
    left = [
        line
        for line in screen.transcript()
        if any(g in line for g in SPINNER) or re.search(r" · (check|fix) \d+ of \d+", line)
    ]
    assert not left, left


def the_log(root):
    (log,) = (root / "logs").glob("*-lint.log")
    return log


def test_each_type_is_a_step_a_failure_is_whole_above_and_the_summary_names_it(tmp_path):
    root = tree(tmp_path)
    rc, screen, stream = live(root, "good", "bad", "env")
    assert rc == 1, stream[-3000:]
    drawn(screen)
    lines = screen.transcript()
    # The failure, whole above: its type, its command, its last 40 lines, where the rest is.
    head = next(line for line in lines if line.startswith("✗ "))
    assert head.startswith("✗ bad: lint-bad (") and head.endswith(" · exit 3"), head
    assert "    bad line 50" in lines and "    bad line 11" in lines and "    bad line 10" not in lines, lines
    assert any(line.startswith("    the whole output: logs/") for line in lines), lines
    # The summary: each type's time, the failed one's exit code, then how many passed and the exit line.
    assert any(line.startswith("homeserver  lint good bad env · failed after ") for line in lines), lines
    table = lines.index(next(line for line in lines if line.startswith("CHECKS")))
    rows = lines[table + 1 : table + 4]
    assert [row.split()[:2] for row in rows] == [["✓", "good"], ["✗", "bad"], ["✓", "env"]], rows
    assert rows[1].endswith("exit 3"), rows
    assert lines[-2].startswith("3 checks · 2 passed · 1 failed · log logs/"), lines
    assert lines[-1] == "exit 1 · failed: bad", lines
    assert screen.titles[-1] == "lint good bad env · failed", screen.titles[-3:]
    # Every line the commands printed is in the log, 0600, and the summary after them; none saw the run's log.
    log = the_log(root)
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
    text = log.read_text()
    assert "good 3" in text and "bad line 1\n" in text and "bad line 50" in text and "3 checks · 2 passed" in text
    assert "log none colour 1" in text, text[-2000:]
    assert "\x1b" not in text, "the log is plain"


def test_a_running_command_shows_its_last_line(tmp_path):
    root = tree(tmp_path)
    rc, _screen, stream = live(root, "slow", interrupt_when=lambda seen: b"slow started" in seen)
    assert rc == 130, stream[-3000:]
    assert "lint-slow  slow started" in stream, "the command's last line, dim after it"


def test_ctrl_c_is_sent_on_to_the_command_and_the_run_stops_there(tmp_path):
    root = tree(tmp_path)
    rc, screen, stream = live(root, "good", "slow", "bad", interrupt_when=lambda seen: b"slow started" in seen)
    assert rc == 130, stream[-3000:]
    drawn(screen)
    lines = screen.transcript()
    assert any(line.startswith("homeserver  lint good slow bad · stopped after ") for line in lines), lines
    assert any(line.startswith(" ○ bad") and line.endswith("not reached") for line in lines), lines
    assert not any(line.startswith("✗ ") for line in lines), "a command Ctrl-C stopped is no failure said above"
    assert "slow got INT" in the_log(root).read_text(), "the command had the Ctrl-C and said its last"
    assert screen.titles[-1] == "lint good slow bad · stopped", screen.titles[-3:]


def test_off_a_terminal_each_type_prints_as_it_runs(tmp_path):
    root = tree(tmp_path)
    rc, out, err = live(root, "good", "bad", terminal=False)
    assert rc == 1 and "good 3" in out and "bad line 1\n" in out and "bad line 50" in out, out + err
    assert err.strip() == "just lint failed: bad", err
    assert not (root / "logs").exists(), "nothing drawn, nothing logged"


def test_the_justfile_draws_lint_and_fix_at_a_terminal_and_streams_them_anywhere_else(tmp_path):
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    shutil.copytree(ROOT / "filter_plugins", root / "filter_plugins", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("live.py", "run_log.py"):
        shutil.copy(ROOT / "scripts" / name, root / "scripts" / name)
    for name in ("justfile", ".yamllint", "ruff.toml"):
        shutil.copy(ROOT / name, root / name)
    (root / "ansible.cfg").write_text("[defaults]\n")
    (root / "settings.yml").write_text("---\nkey: value\n")
    (root / "tool.py").write_text("print( 'unformatted' )\n")
    env = environment(VENV=sys.prefix)

    def just(*args):
        rc, stream = on_a_terminal(["just", *args], env, root)
        screen = VT(COLUMNS, ROWS)
        screen.feed(stream)
        return rc, screen, stream.decode(errors="replace")

    rc, screen, stream = just("lint", "yaml")
    assert rc == 0, stream[-3000:]
    drawn(screen)
    lines = screen.transcript()
    assert any(line.startswith("homeserver  lint yaml · done in ") for line in lines), lines
    assert any(line.startswith(" ✓ yaml") for line in lines) and lines[-1] == "exit 0", lines

    rc, screen, stream = just("fix", "python")
    assert rc == 0, stream[-3000:]
    lines = screen.transcript()
    assert any(line.startswith("FIXES") for line in lines) and any(line.startswith(" ✓ python") for line in lines)
    assert (root / "tool.py").read_text() == 'print("unformatted")\n', "the fix ran"

    (root / "settings.yml").write_text("---\nkey: value\nkey: again\n")
    rc, screen, stream = just("lint", "yaml")
    assert rc == 1, stream[-3000:]
    lines = screen.transcript()
    assert any(line.startswith("✗ yaml: lint-yaml (") for line in lines), lines
    assert any("duplication of key" in line for line in lines), lines

    # Anywhere else, the stream, as before: yamllint's own lines, and the failure named at the end.
    proc = subprocess.run(["just", "lint", "yaml"], cwd=root, env=env, capture_output=True, text=True)
    assert proc.returncode == 1 and "duplication of key" in proc.stdout, proc.stdout + proc.stderr
    assert "CHECKS" not in proc.stdout and proc.stderr.strip().endswith("just lint failed: yaml"), proc.stderr
