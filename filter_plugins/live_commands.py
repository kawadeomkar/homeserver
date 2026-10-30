"""A run of commands drawn live: just lint's, fix's and test's types, one after another, each a step of one stage.

scripts/live.py hands a run here once it has decided to draw it. Each type's command (just lint-python) runs as a
child: its stdin /dev/null, its stdout and stderr one pipe, colour off and Python unbuffered, without the run's
ANSIBLE_LOG_PATH, in a session of its own, so that a prompt at /dev/tty fails at once rather than stopping the run.
Every line it prints goes to the run's log; the region (filter_plugins/live_region.py) shows the type running, with
the child's last line dim after it. A test run's step counts its tests instead (the pytest reader, below): several
running at once under xdist, a bar by those collected, each failure above as it fails. A type that fails otherwise
prints its last lines above, whole, with where the log has the rest, and the run goes on to the next, as the
justfile's _each does. The end is the summary: each type's time, a
failed one's exit code, and the exit line naming what failed.

Ctrl-C is sent on to the running child's process group, which the terminal's does not reach: the child says its last
and ends, and the run stops there, 130, the types it never reached said so. A second Ctrl-C ends the child's whole
tree (processes.py), what it still writes unread. SIGTERM or SIGHUP, a kill or the terminal closing, is sent on to the
tree, the region goes and the echo comes back, and the run dies of the same signal.

Adapted from claude-on-proxmox's filter_plugins/live_commands.py (commit 83c7bf6), without its runs in parts: one
process here runs every command of a run, so no state is kept between them, and without its Molecule reader. Loaded
by path as homeserver_live_commands; it has no filters of its own.
"""

from __future__ import annotations

import collections
import contextlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from ansible import constants as C
from ansible.utils.display import Display


def _sibling(stem):
    """filter_plugins/<stem>.py, loaded by path once as homeserver_<stem>."""
    name = f"homeserver_{stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().with_name(f"{stem}.py"))
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


text = _sibling("text")
screen = _sibling("screen")
view = _sibling("run_view")
live = _sibling("live_region")
processes = _sibling("processes")
engine = _sibling("live_ansible")

# What a child never inherits: the run's log is written here, by this process alone.
NOT_PASSED_ON = ("ANSIBLE_LOG_PATH",)
CHILD_ENV = {"NO_COLOR": "1", "PYTHONUNBUFFERED": "1", "PRE_COMMIT_COLOR": "never"}
# How much of a failed command's output is printed above: the end, where its error is.
FAILURE_LINES = 40
INTERRUPTED = view.INTERRUPTED
# What ends a run from outside: a kill or a timeout, and the terminal closing.
OUTSIDE = (signal.SIGTERM, signal.SIGHUP)
# Where tests/conftest.py writes a test run's events, for the pytest reader.
PYTEST_ENV = "HOMESERVER_LIVE_PYTEST"
OUTCOMES = {"passed": "ok", "failed": "failed", "skipped": "skipped"}


def named(nodeid):
    """A test as the screen names it: its file's name and the rest of its ID, whose parameters may hold a "/" of their
    own (test_x.py::test_path[../etc/passwd])."""
    path, separator, rest = nodeid.partition("::")
    return os.path.basename(path) + separator + rest


class Pytest:
    """The pytest reader: each test a task of the type's step, several running at once under xdist, from the events
    tests/conftest.py writes to a file beside the run's log; the count once collected, and each failure above as it
    fails. Read ten times a second while pytest runs, and once more when it has ended."""

    def __init__(self, commands, kind, task):
        self.commands, self.kind, self.task = commands, kind, task
        self.read = 0
        self.failed = False
        self.stop = threading.Event()
        logs = Path(os.environ.get("ANSIBLE_LOG_PATH") or tempfile.gettempdir()).parent
        descriptor, self.events = tempfile.mkstemp(dir=logs, prefix=f".{kind}-pytest-", suffix=".jsonl")
        os.close(descriptor)
        self.thread = threading.Thread(target=self._polling, daemon=True)

    def env(self):
        return {PYTEST_ENV: self.events}

    def start(self):
        self.thread.start()

    def _polling(self):
        while not self.stop.wait(0.1):
            self.poll()
            self.commands.out.redraw()

    def poll(self):
        """The events pytest has written since the last look: whole lines only."""
        try:
            with open(self.events, "rb") as handle:
                handle.seek(self.read)
                chunk = handle.read()
        except OSError:
            return
        whole = chunk[: chunk.rfind(b"\n") + 1]
        self.read += len(whole)
        for raw in whole.splitlines():
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if isinstance(event, dict):
                self._event(event)

    def _event(self, event):
        commands, run, now = self.commands, self.commands.run, self.commands._now()
        failure = None
        with commands.display._lock:
            if isinstance(event.get("count"), int):
                # The command's own task stands for it no more: its tests do, each counted, it with them.
                self.task.hidden = True
                run.expect(self.task.stage, self.task.step, event["count"] + 1)
            elif isinstance(event.get("start"), str):
                nodeid = text.one_line(event["start"], 300)
                run.task(f"test:{nodeid}", named(nodeid), None, now, parallel=True)
            elif isinstance(event.get("end"), str):
                nodeid = text.one_line(event["end"], 300)
                outcome = OUTCOMES.get(event.get("outcome"), "ok")
                run.result(f"test:{nodeid}", "localhost", outcome, now)
                if outcome == "failed":
                    self.failed = True
                    found = run.find(f"test:{nodeid}")
                    failure = (found, text.plain_text(str(event.get("said") or "")).splitlines())
        if failure is not None:
            found, said = failure
            took = f" ({screen.duration(found.seconds(now))})" if found is not None else ""
            head = f"✗ {self.kind}: {found.name if found is not None else self.kind}{took}"
            lines = [("", ""), (screen.render([(head, "r")], commands.color), head)]
            lines += [(f"    {line.rstrip()}", f"    {line.rstrip()}") for line in said[-FAILURE_LINES:]]
            commands._print([*lines, ("", "")])

    def ended(self, now):
        """pytest has ended: its last events read, a test still running stopped with it, and the file gone. Whether a
        test failure was said, so that the command's own last lines need not be."""
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join()
        self.poll()
        with contextlib.suppress(OSError):
            os.unlink(self.events)
        with self.commands.display._lock:
            for task in self.commands.run.order:
                if task.key.startswith("test:") and task.end is None and task.step == self.task.step:
                    task.result("localhost", "skipped", now)
        return self.failed


class Abandoned(Exception):
    """A second Ctrl-C: the run stops reading what its child's group still writes."""


class Ended(BaseException):
    """SIGTERM or SIGHUP: the run is ended from outside, by that signal. Nothing here catches it but run()."""

    def __init__(self, signum):
        super().__init__(signum)
        self.signum = signum


class Commands:
    """One drawn run: commands, each (its type, its argv), run in order, each the one task of its type's step."""

    def __init__(self, title, recipe, commands, log=""):
        self.recipe = recipe
        self.commands = list(commands)
        self.display = Display()
        self.out = live.Output(
            self.display,
            frame=self._frame,
            live=True,
            color=not C.ANSIBLE_NOCOLOR,
            to_log=True,
            on_fault=self._fault,
        )
        self.color = self.out.color
        self.run = view.Run(title, self._now(), log=log, recipe=recipe)
        tasks = [(f"type:{kind}", None, {"name": kind}) for kind, _argv in self.commands]
        self.run.plays([(recipe.name, recipe.name, tasks, recipe.name)])
        self.child = None
        self.under = {}
        self.blind = []
        self.interrupted = 0
        self.reading = False
        self.drawn = True
        self.blank = False  # whether the last line printed above was blank, so that parts are one blank line apart

    @staticmethod
    def _now():
        return time.monotonic()

    @staticmethod
    def _width():
        try:
            return os.get_terminal_size(sys.stdout.fileno()).columns
        except (OSError, ValueError, AttributeError):
            return 100

    def _frame(self, cols, rows, glyph):
        with self.display._lock:
            now = self._now()
            self.out.title(self.run.title_text(now))
            # A column short, as the playbook engine draws: a full line would leave the cursor on its last column.
            return self.run.frame(now, cols - 1, rows, glyph, self.color)

    def _fault(self, error):
        """A frame that cannot be drawn: the region is gone, and the commands' lines go to the screen from here."""
        self.drawn = False
        with contextlib.suppress(Exception):
            self.out.release()
        self.display.display(f"[homeserver] the live output stopped ({type(error).__name__}: {error})", stderr=True)

    def _print(self, lines):
        """Lines above the region, (coloured, plain) each, a blank one first left out after a blank one."""
        if lines and self.blank and not lines[0][1].strip():
            lines = lines[1:]
        if lines:
            self.out.print(lines)
            self.blank = not lines[-1][1].strip()

    def _child_env(self):
        env = {name: value for name, value in os.environ.items() if name not in NOT_PASSED_ON}
        return env | CHILD_ENV

    # --- signals -------------------------------------------------------------------------------------------
    def interrupt(self, *_):
        # The child runs in a session of its own, which the terminal's Ctrl-C does not reach: it is sent there, and the
        # child says its last and ends. A second one ends everything the child started, and what it writes goes unread.
        self.interrupted += 1
        if self.child is None:
            return
        if self.interrupted > 1:
            processes.end(self.child.pid, remembered=self.under, signum=signal.SIGKILL, blind=self.blind.append)
        else:
            # What the child has started, remembered before it is signalled: a child that ends at the Ctrl-C leaves
            # them under PID 1, where a second Ctrl-C could no longer find them from it.
            try:
                self.under = processes.snapshot(self.child.pid)
            except processes.Blind as error:
                self.blind.append(str(error))
            with contextlib.suppress(OSError):
                os.killpg(self.child.pid, signal.SIGINT)
        if self.interrupted > 1 and self.reading:
            raise Abandoned

    def outside(self, signum, _frame):
        # The child's session has no terminal to signal it: it gets the signal from here, with everything it started.
        if self.child is not None:
            processes.end(self.child.pid, signum=signum, blind=self.blind.append)
        raise Ended(signum)

    def abandon(self):
        """The run ended from outside: the region gone and the echo back, as far as a terminal that may be gone
        allows."""
        with contextlib.suppress(Exception):
            self.out.finish()

    # --- the run -------------------------------------------------------------------------------------------
    def work(self):
        """Every command in turn, then the summary: 0, 1 when a command failed, 130 after Ctrl-C."""
        self.out.start()
        self.out.redraw()
        self.out.spin()
        failed = []
        for kind, argv in self.commands:
            if self.interrupted:
                break
            if self._one(kind, argv):
                failed.append(kind)
        now = self._now()
        code = INTERRUPTED if self.interrupted else 1 if failed else 0
        with self.display._lock:
            self.run.end(now, code)
            self.out.title(self.run.title_text(now))
        self.out.finish()
        for said in self.blind:
            self.display.display(f"scripts/live.py: {said}", log_only=True)
        summary = self.run.summary(now, self._width() - 1, (), self.color)
        self._print([("", ""), *summary])
        if code or now - self.run.started >= engine.BELL_SECONDS:
            self.out.bell()
        return code

    def _one(self, kind, argv):
        """One command, its every line to the log: its exit code, 128 and the number of a signal that ended it."""
        key = f"type:{kind}"
        with self.display._lock:
            task = self.run.task(key, f"{self.recipe.name}-{kind}", None, self._now())
        reader = Pytest(self, kind, task) if self.recipe.reader == "pytest" else None
        self.out.redraw()
        last = collections.deque(maxlen=FAILURE_LINES)
        try:
            self.child = child = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self._child_env() | (reader.env() if reader else {}),
                # A session of its own, with no terminal: a prompt at /dev/tty fails rather than stopping the child,
                # and the session is its process group too, which Ctrl-C is sent on to.
                start_new_session=True,
            )
        except OSError as error:
            last.append(f"{argv[0]}: {error.strerror}")
            self.display.display(last[-1], log_only=True)
            rc = 127 if isinstance(error, FileNotFoundError) else 126
        else:
            if reader is not None:
                reader.start()
            try:
                self.reading = True
                for raw in iter(child.stdout.readline, b""):
                    line = raw.decode(errors="replace").rstrip("\r\n")
                    self.display.display(line, log_only=True)
                    if not self.drawn:
                        self.display.display(line, screen_only=True)
                    last.append(line)
                    said = text.one_line(line, 200)
                    if said.strip():
                        with self.display._lock:
                            task.detail = said
                self.reading = False
                rc = child.wait()
            except Abandoned:
                self.reading = False
                rc = INTERRUPTED
                with contextlib.suppress(Exception):
                    child.wait(timeout=5)
            # Ended by a signal, as a shell says it: 128 and the signal's number.
            rc = 128 - rc if rc < 0 else rc
        self.child = None
        now = self._now()
        said = reader.ended(now) if reader is not None else False
        with self.display._lock:
            task.detail = ""
            task.rc = rc
            # One Ctrl-C stopped is no failure: it is drawn as not run to its end.
            outcome = "ok" if rc == 0 else "skipped" if self.interrupted else "failed"
            self.run.result(key, "localhost", outcome, now)
        # A test run whose failures were said as they failed says no more; one that failed otherwise (an error before
        # any test ran) says its last lines, as any command does.
        if rc and not self.interrupted and not said:
            self._failure(task, kind, rc, last, now)
        self.out.redraw()
        return rc

    def _failure(self, task, kind, rc, last, now):
        """A failed command, whole above: its type, what it ran, its last lines and where the log has the rest."""
        head = f"✗ {kind}: {task.name} ({screen.duration(task.seconds(now))}) · exit {rc}"
        lines = [("", ""), (screen.render([(head, "r")], self.color), head)]
        lines += [(f"    {line}", f"    {line}") for line in (text.plain_text(line).rstrip() for line in last)]
        if self.run.log:
            said = f"    the whole output: {self.run.log}"
            lines.append((screen.render([(said, "d")], self.color), said))
        self._print([*lines, ("", "")])


def run(title, recipe, commands, log=""):
    """A run of commands, drawn: its exit code."""
    commands = Commands(title, recipe, commands, log)
    # Ctrl-C from the first moment: a handler, never SIG_IGN, which the children would inherit. SIGTERM and SIGHUP end
    # the run, its child's tree with it.
    previous = {signal.SIGINT: signal.signal(signal.SIGINT, commands.interrupt)}
    previous.update((signum, signal.signal(signum, commands.outside)) for signum in OUTSIDE)
    try:
        return commands.work()
    except Ended as ended:
        commands.abandon()
        # Ended as the signal ends a process, which is what the recipe's shell expects to see.
        signal.signal(ended.signum, signal.SIG_DFL)
        os.kill(os.getpid(), ended.signum)
        return 128 + ended.signum
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


class FilterModule:
    """No filters: scripts/live.py loads this module by path. Here so the filter loader takes the file."""

    def filters(self):
        return {}
