"""just status's stdout callback: the report, with one line on what was read above its exit line, and none of
Ansible's task output; on a terminal, that line is drawn at once and kept up to date while the machines are read."""

DOCUMENTATION = r"""
name: homeserver_status
type: stdout
short_description: just status's report, without Ansible's task output
description:
  - Prints the text of the task that shows the report (status.yml, roles/status's report entry point), with one line
    above its last, the exit line, saying for each host the reading play ran on whether it answered and was read and
    how long it took, and how long asking the apps' addresses took.
  - With STATUS_LIVE set to 1, as just status sets it on a terminal, that line is drawn as soon as the hosts are being
    read and redrawn in place ten times a second, each host still being read with a spinner and its seconds so far,
    then cleared before the report. Anything printed meanwhile, by Ansible or by this callback, goes above it.
  - Prints nothing on standard output when STATUS_QUIET is set, for just status --json, which prints the report's
    file instead.
  - Prints every failure and every unreachable host on standard error, and nothing else of Ansible's.
requirements:
  - Chosen with ANSIBLE_STDOUT_CALLBACK=homeserver_status, as just status does; Ansible finds it beside status.yml.
"""

import atexit
import os
import shutil
import sys
import threading
import time

from ansible.plugins.callback import CallbackBase

# What the report calls each inventory group's host.
LABELS = {"proxmox": "proxmox", "nas": "nas", "kubernetes": "cluster"}
# The task whose message is the report, the one that asks the apps' addresses, and the play that reads the hosts.
REPORT_TASK = "Show the report"
PROBE_TASK = "Ask the apps' addresses"
READING_PLAY = "Read the machines"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
FRAME_SECONDS = 0.1
# The colours of the live lines' marks, as the report's own (roles/status's filter): dim, green, red.
STYLES = {"d": "90", "g": "32", "r": "31"}


def _fit(parts, width, color):
    """A line of (text, style) parts, cut to `width` characters, in colour when `color`."""
    out, left = [], width
    for text, style in parts:
        text = text[:left]
        left -= len(text)
        out.append(f"\033[{STYLES[style]}m{text}\033[0m" if color and style and text.strip() else text)
    return "".join(out)


class Live:
    """Lines kept at the bottom of the terminal and redrawn in place by a thread of their own, from `lines`, a function
    that returns them as lists of (text, style) parts. Whatever else is printed meanwhile goes above them: Display's
    display, which every message of Ansible's and of this callback goes through, is wrapped to clear them first and
    draw them again after. Every write holds Display's own lock, which Ansible already takes for each write from any
    of its threads and lets go of in the processes it forks, so that a frame never mixes with another write."""

    def __init__(self, display, lines, color):
        self._display = display
        self._lines = lines
        self._color = color
        self._lock = getattr(display, "_lock", None) or threading.RLock()
        self._drawn = 0
        self._open = True
        self._stop = threading.Event()
        self._show = display.display
        display.display = self._above
        self._thread = threading.Thread(target=self._run, name="homeserver_status", daemon=True)
        self._thread.start()
        # A run cut short (Ctrl-C) leaves no half-drawn line behind.
        atexit.register(self.close)

    def _erase(self):
        """What takes the cursor back to the first line drawn and clears from there down."""
        erase, self._drawn = (f"\033[{self._drawn}F\033[J" if self._drawn else ""), 0
        return erase

    def _frame(self):
        """The lines as they are now, each cut to the terminal's width so that none wraps onto a second row."""
        width = max(shutil.get_terminal_size((100, 24)).columns - 1, 20)
        lines = [_fit(parts, width, self._color) for parts in self._lines()]
        self._drawn = len(lines)
        return "".join(line + "\n" for line in lines)

    def _write(self, text):
        sys.stdout.write(text)
        sys.stdout.flush()

    def _run(self):
        while not self._stop.wait(FRAME_SECONDS):
            self.redraw()

    def redraw(self):
        with self._lock:
            if self._open:
                self._write(self._erase() + self._frame())

    def _above(self, msg, *args, **kwargs):
        if kwargs.get("log_only"):
            return self._show(msg, *args, **kwargs)
        with self._lock:
            if self._open:
                self._write(self._erase())
            self._show(msg, *args, **kwargs)
            sys.stdout.flush()
            sys.stderr.flush()
            if self._open:
                self._write(self._frame())
        return None

    def close(self):
        """Stops the thread and clears the lines, for good."""
        self._stop.set()
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join()
        with self._lock:
            if self._open:
                self._open = False
                self._write(self._erase())
                self._display.display = self._show


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "homeserver_status"

    def __init__(self):
        super().__init__()
        self._quiet = bool(os.environ.get("STATUS_QUIET"))
        self._live_wanted = os.environ.get("STATUS_LIVE") == "1" and not self._quiet and sys.stdout.isatty()
        self._color = os.environ.get("STATUS_COLOR") == "1"
        self._live = None
        self._reading = False
        self._started = 0.0
        self._task_started = 0.0
        self._hosts = {}

    def _label(self, host):
        groups = [g.get_name() for g in host.get_groups()]
        return next((LABELS[g] for g in groups if g in LABELS), host.get_name())

    def _entry(self, host):
        new = {"label": self._label(host), "state": "ok", "done": False, "since": self._started, "seconds": 0.0}
        return self._hosts.setdefault(host.get_name(), new)

    def _seen(self, result, state, done=False):
        if not self._reading:
            return
        # A host that answered but whose source could not be read, such as a NAS that refused the API key: its
        # findings say why, and the report too.
        facts = result._result.get("ansible_facts") or {}
        found = facts.get("status_found")
        if state == "ok" and any(isinstance(f, dict) and f.get("unread") for f in (found or {}).values()):
            state = "not read"
        entry = self._entry(result._host)
        if entry["done"]:
            return
        # A host with nothing to read in this setup, such as the NAS's without one, is left out of the line.
        if found == {}:
            entry["nothing"] = True
        entry["seconds"] = time.monotonic() - self._started
        entry["done"] = done or found is not None
        if entry["state"] == "ok":
            entry["state"] = state

    def _reading_parts(self, live=False):
        """The line on what was read: each host's state and seconds, or while it is still being read, a spinner and
        its seconds so far."""
        now = time.monotonic()
        parts = [("reading  ", "")]
        # A copy: the main thread adds hosts while the live line's thread draws them.
        shown = [h for h in list(self._hosts.values()) if not h.get("nothing")]
        for i, h in enumerate(shown):
            if i:
                parts.append((" · ", "d"))
            if live and not h["done"]:
                spinner = SPINNER[int(now / FRAME_SECONDS) % len(SPINNER)]
                parts += [(f"{h['label']} ", ""), (spinner, "d"), (f" {now - h['since']:.1f} s", "d")]
            else:
                style = "g" if h["state"] == "ok" else "r"
                parts += [(f"{h['label']} ", ""), (h["state"], style), (f" {h['seconds']:.1f} s", "")]
        return parts if shown else []

    def _line(self):
        return _fit(self._reading_parts(), 10**6, False)

    def _start_live(self):
        if self._live_wanted and self._live is None:
            self._live = Live(self._display, lambda: [p for p in [self._reading_parts(live=True)] if p], self._color)

    def _stop_live(self):
        if self._live is not None:
            self._live.close()
            self._live = None

    def _flush(self):
        """The line on what was read, once, at the end of a run that made no report."""
        self._stop_live()
        if self._line() and not self._quiet:
            self._display.display(self._line())
        self._hosts = {}

    def v2_playbook_on_play_start(self, play):
        self._reading = play.get_name() == READING_PLAY
        self._started = time.monotonic()
        if self._reading:
            self._start_live()

    def v2_playbook_on_task_start(self, task, is_conditional):
        self._task_started = time.monotonic()
        if task.get_name().endswith(PROBE_TASK):
            self._hosts["probes"] = {
                "label": "probes",
                "state": "ok",
                "done": False,
                "since": self._task_started,
                "seconds": 0.0,
            }

    def v2_runner_on_start(self, host, task):
        if self._reading:
            self._entry(host)

    def v2_runner_on_ok(self, result):
        self._seen(result, "ok")
        name = result._task.get_name()
        if name.endswith(PROBE_TASK):
            self._hosts["probes"] |= {"done": True, "seconds": time.monotonic() - self._task_started}
        if name.endswith(REPORT_TASK) and not self._quiet:
            self._stop_live()
            text = result._result.get("msg", "")
            # The line on what was read goes above the report's last, its exit line.
            body, _, last = text.rstrip("\n").rpartition("\n")
            self._display.display(f"{body}\n{self._line()}\n{last}" if self._line() else text.rstrip("\n"))
            self._hosts = {}

    def v2_runner_on_skipped(self, result):
        if result._task.get_name().endswith(PROBE_TASK):
            self._hosts.pop("probes", None)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if ignore_errors:
            self._seen(result, "ok")
            return
        self._seen(result, "failed", done=True)
        message = result._result.get("msg") or result._result.get("stderr") or "failed"
        self._display.display(f"{result._host.get_name()}: {result._task.get_name()}: {message}", stderr=True)

    def v2_runner_on_unreachable(self, result):
        self._seen(result, "did not answer")
        message = result._result.get("msg", "unreachable")
        self._display.display(f"{result._host.get_name()} did not answer: {message}", stderr=True)

    def v2_playbook_on_stats(self, stats):
        self._flush()
