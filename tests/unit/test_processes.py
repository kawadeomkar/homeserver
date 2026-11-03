"""filter_plugins/processes.py: what ends a drawn run's command whole, beyond the process group it was sent to.

A command scripts/live.py runs is in a session of its own, and what it starts may be again (ansible-core's task worker
detaches: setsid, its stdio on /dev/null), so a kill of its group alone would leave such a grandchild behind,
reparented to PID 1 and idle for good. A kill must find the tree before it signals anything. Ported from
claude-on-proxmox's tests/unit/test_processes.py with the module, its harness's tests aside.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("test_processes_module", ROOT / "filter_plugins" / "processes.py")
processes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(processes)

# A child in a group of its own, with a grandchild in a session of its own: the shape of a playbook and its task worker.
# Each pid to the file named, then both wait.
HOLDS = (
    "import os, subprocess, sys, time\n"
    "grandchild = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)\n"
    "open(sys.argv[1], 'w').write(f'{os.getpid()} {grandchild.pid}')\n"
    "time.sleep(120)\n"
)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _until(condition, what: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"not within {timeout} s: {what}")
        time.sleep(0.1)


def _child_and_grandchild(tmp_path: Path) -> tuple[subprocess.Popen, int]:
    pids = tmp_path / "pids"
    child = subprocess.Popen([sys.executable, "-c", HOLDS, str(pids)], process_group=0)
    _until(lambda: pids.exists() and len(pids.read_text().split()) == 2, "the grandchild's pid written")
    return child, int(pids.read_text().split()[1])


def test_descendants_lists_a_grandchild_in_a_session_of_its_own(tmp_path):
    child, grandchild = _child_and_grandchild(tmp_path)
    try:
        assert processes.descendants(child.pid) == [grandchild]
        assert os.getpgid(grandchild) == grandchild != os.getpgid(child.pid)
    finally:
        processes.end(child.pid)
        child.wait()
    _until(lambda: not _alive(grandchild), "the grandchild gone")


def test_a_kill_of_the_group_leaves_the_grandchild_behind_and_end_does_not(tmp_path):
    # A kill of the group alone: the child's group ends, the grandchild lives on under PID 1.
    child, grandchild = _child_and_grandchild(tmp_path)
    os.killpg(child.pid, signal.SIGKILL)
    assert child.wait() == -signal.SIGKILL
    try:
        time.sleep(0.5)
        assert _alive(grandchild)
        # Reparented: to PID 1, or to a subreaper where one stands between (systemd --user); the child no more.
        parent = subprocess.run(["ps", "-o", "ppid=", "-p", str(grandchild)], capture_output=True, text=True).stdout
        assert parent.strip() not in ("", str(child.pid)), parent
    finally:
        processes.end(grandchild)
    _until(lambda: not _alive(grandchild), "the grandchild gone")
    # And the same tree, ended whole: nothing of it is left.
    (tmp_path / "again").mkdir()
    child, grandchild = _child_and_grandchild(tmp_path / "again")
    signalled = processes.end(child.pid)
    child.wait()
    assert signalled == [child.pid, grandchild]
    _until(lambda: not _alive(grandchild), "the grandchild gone with the child")


def test_end_signals_a_child_of_this_group_alone_and_never_the_group(tmp_path):
    # A child in this process's own group: ending it must not end this process.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    assert os.getpgid(child.pid) == os.getpgrp()
    assert processes.end(child.pid) == [child.pid]
    assert child.wait() == -signal.SIGKILL


def test_end_of_a_process_that_is_gone_signals_nothing_and_raises_nothing(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert processes.end(child.pid) == [child.pid]


def test_a_remembered_pid_is_signalled_only_while_the_process_of_that_start_is_at_it(tmp_path):
    # What the run remembered of its command's tree, by each process's start: the grandchild, orphaned since, is ended;
    # a pid another process has by now - this process's own child standing in for a reused one - is left alone.
    child, grandchild = _child_and_grandchild(tmp_path)
    remembered = processes.snapshot(child.pid)
    assert set(remembered) == {grandchild}
    os.killpg(child.pid, signal.SIGKILL)
    child.wait()
    bystander = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        remembered[bystander.pid] = "Thu Jan  1 00:00:00 1970"
        signalled = processes.end(child.pid, remembered=remembered)
        assert grandchild in signalled and bystander.pid not in signalled
        _until(lambda: not _alive(grandchild), "the orphaned grandchild gone")
        assert bystander.poll() is None
    finally:
        bystander.kill()
        bystander.wait()


LEAVES = (  # a child that starts a grandchild in its own group and exits, leaving it there
    "import os, subprocess, sys\n"
    "kept = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
    "open(sys.argv[1], 'w').write(str(kept.pid))\n"
)


def _child_that_left(tmp_path: Path) -> tuple[subprocess.Popen, int]:
    pids = tmp_path / "pids"
    child = subprocess.Popen([sys.executable, "-c", LEAVES, str(pids)], process_group=0)
    _until(lambda: pids.exists() and pids.read_text().isdigit(), "the grandchild's pid written")
    kept = int(pids.read_text())
    # Exited, and not reaped: a command between its exit and the run's wait for it.
    _until(lambda: os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT) is not None, "the child exited")
    assert child.returncode is None and _alive(kept) and os.getpgid(kept) == child.pid
    return child, kept


@pytest.mark.parametrize("reaped", [False, True], ids=["a zombie", "reaped"])
def test_a_root_that_exited_still_has_its_group_ended(tmp_path, reaped):
    # A child gone at the first Ctrl-C, and what it started after the snapshot still in its group. Its children moved
    # on to PID 1 or a subreaper the moment it exited, reaped or not, so no walk finds them under it, and a zombie no
    # longer answers for its group on macOS: the group is signalled all the same, unasked, as it was before this module.
    child, kept = _child_that_left(tmp_path)
    if reaped:
        child.wait()
        time.sleep(0.3)
    assert processes.end(child.pid) == [child.pid]
    child.wait()
    _until(lambda: not _alive(kept), "the grandchild in the exited child's group gone")


def test_a_table_that_cannot_be_read_is_said_and_the_group_still_ended(tmp_path, monkeypatch):
    child, grandchild = _child_and_grandchild(tmp_path)
    said = []
    try:
        monkeypatch.setattr(processes.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no ps")))
        assert processes.end(child.pid, blind=said.append) == [child.pid]
        assert len(said) == 1 and "no ps" in said[0] and "the group ended" in said[0], said
        assert child.wait() == -signal.SIGKILL
    finally:
        monkeypatch.undo()
        processes.end(grandchild)
    _until(lambda: not _alive(grandchild), "the grandchild gone")
