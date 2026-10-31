"""The processes under one, and their end: what a kill must reach beyond the process group it was sent to.

A command scripts/live.py runs is in a session of its own, so that a prompt at /dev/tty fails rather than stops it,
and what it starts may be again: a playbook's task runs in a worker ansible-core forks and detaches (2.21's
WorkerProcess._detach: setsid, its stdio on /dev/null). ansible-core sends that worker a SIGINT or SIGTERM of its own
on, but a SIGKILL, or a SIGHUP, which it does not handle, ends the playbook and leaves the task in flight behind,
reparented and idle for good. `descendants` reads the table `ps` prints, which macOS and Linux both have, and must be
read before anything is signalled: a dead parent's children move to PID 1, or to a subreaper, where nothing leads to
them. `snapshot` keeps a tree with each process's start, for an `end` after part of it has gone: a pid remembered is
signalled only while the process of that start is still at it, never once another has it. `end` signals a process,
every process under it and each of their groups but this process's own, so a child started in this group is ended
alone, never this process with it; the root's group is signalled whether or not it can still be looked up, since the
caller starts it as a group leader, a child that has exited, a zombie, no longer answers for its group on macOS, and
its children moved on the moment it exited. The interrupts are held off while the table is read and the tree
signalled, since a second one, a hangup moments after a kill, would otherwise end the end itself.

Ported from claude-on-proxmox's filter_plugins/processes.py (commit 83c7bf6), unchanged but for its names: the projects
stay independent, so a fix there is compared by hand. Loaded by path as homeserver_processes (live_commands.py); it
has no filters of its own.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess

# Held off while a tree is read and signalled: one of them arriving then would abandon the end half way.
HELD = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class Blind(OSError):
    """The process table could not be read: ps is missing, or failed."""


def _table() -> dict[int, tuple[int, str]]:
    """Every process by pid, with its parent's and its start, as ps lists them now."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,lstart="], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        raise Blind(f"the process table could not be read ({error}): the group ended, what left it not") from error
    table = {}
    for line in out.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            table[int(fields[0])] = (int(fields[1]), fields[2].strip())
    return table


def descendants(pid: int, table: dict[int, tuple[int, str]] | None = None) -> list[int]:
    """Every process under pid as ps lists them now, each before its own children; Blind when ps cannot be read."""
    table = _table() if table is None else table
    children: dict[int, list[int]] = {}
    for child, (parent, _) in table.items():
        children.setdefault(parent, []).append(child)
    found: list[int] = []
    queue = [pid]
    while queue:
        for child in children.get(queue.pop(0), []):
            if child not in found and child != pid:
                found.append(child)
                queue.append(child)
    return found


def snapshot(pid: int) -> dict[int, str]:
    """Every process under pid with its start, to remember a tree by before part of it ends."""
    table = _table()
    return {each: table[each][1] for each in descendants(pid, table)}


def end(pid: int, *, remembered: dict[int, str] | None = None, signum: int = signal.SIGKILL, blind=None) -> list[int]:
    """Signal a process, every process under it and their groups other than this one's; the pids signalled.

    remembered, a snapshot from before part of the tree ended: each pid in it is signalled, with what is under it,
    only while a process of that start is still at it. blind, called with the message, is how a table that cannot be
    read is said: the process and its group are then ended as before, and what left the group is not.
    """
    held = signal.pthread_sigmask(signal.SIG_BLOCK, HELD)
    try:
        try:
            table = _table()
        except Blind as error:
            table = {}
            if blind is not None:
                blind(str(error))
        pids = [pid, *descendants(pid, table)]
        for each, started in (remembered or {}).items():
            if each not in pids and table.get(each, (None, None))[1] == started:
                pids.extend(found for found in (each, *descendants(each, table)) if found not in pids)
        own = os.getpgrp()
        groups = {pid}
        for each in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                groups.add(os.getpgid(each))
        for group in groups - {own}:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signum)
        for each in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(each, signum)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)
    return pids


class FilterModule:
    """No filters: the command engine loads this module by path. Here so the filter loader takes the file."""

    def filters(self):
        return {}
