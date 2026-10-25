"""just status's stdout callback: the report, with one line on what was read above its exit line, and none of
Ansible's task output; on a terminal, each section as soon as the hosts it needs are read, in the report's order, the
sections still to come kept up to date in place below."""

DOCUMENTATION = r"""
name: homeserver_status
type: stdout
short_description: just status's report, without Ansible's task output
description:
  - Prints the text of the task that shows the report (status.yml, roles/status's report entry point), with one line
    above its last, the exit line, saying for each host the reading play ran on whether it answered and was read and
    how long it took.
  - With STATUS_LIVE set to 1, as just status sets it on a terminal, it prints the report itself instead, block by
    block, from the same filter as the report play (roles/status/filter_plugins/status.py, beside the playbook) and
    the settings status.yml's first play says. It prints the head at once, then each section in the report's order as
    soon as every source it is worked out from is read and every section before it is printed, then once every host
    is read the reads that failed, what needs attention and the exit line. Below what is printed, the sections still
    to come and the line on what is read are kept up to date in place ten times a second, a spinner beside each
    section still waiting with the hosts it waits for, a section already read with how many problems and things to do
    it has, until the report replaces them. Anything printed meanwhile, by Ansible or by this callback, goes above
    them. On a terminal too short for them, only the line on what is read is kept.
  - Prints nothing on standard output when STATUS_QUIET is set, for just status --json, which prints the report's
    file instead.
  - Prints every failure and every unreachable host on standard error, and nothing else of Ansible's.
requirements:
  - Chosen with ANSIBLE_STDOUT_CALLBACK=homeserver_status, as just status does; Ansible finds it beside status.yml.
"""

import atexit
import importlib.util
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from ansible.plugins.callback import CallbackBase

# What the report calls each inventory group's host.
LABELS = {"proxmox": "proxmox", "nas": "nas", "kubernetes": "cluster"}
# The task whose message is the report, the one that says what the report is of, and the play that reads the hosts.
REPORT_TASK = "Show the report"
SETTINGS_TASK = "Say what the report is of"
READING_PLAY = "Read the machines"
# The filter that writes the report, beside the playbook.
FILTERS = Path("roles") / "status" / "filter_plugins" / "status.py"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
FRAME_SECONDS = 0.1
# The colours of the live lines, as the report's own: dim, green, yellow, red, and a section's name.
STYLES = {"d": "90", "g": "32", "y": "33", "r": "31", "h": "1;34"}
# How the live lines name the hosts a section waits for.
HOSTS = {"proxmox": "the Proxmox host", "cluster": "the cluster", "nas": "the NAS"}


def _fit(parts, width, color):
    """A line of (text, style) parts, cut to `width` characters, in colour when `color`."""
    out, left = [], width
    for text, style in parts:
        text = text[:left]
        left -= len(text)
        out.append(f"\033[{STYLES[style]}m{text}\033[0m" if color and style and text.strip() else text)
    return "".join(out)


def _and(names):
    names = list(names)
    return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)


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
        size = shutil.get_terminal_size((100, 24))
        lines = [_fit(parts, max(size.columns - 1, 20), self._color) for parts in self._lines(size.lines)]
        self._drawn = len(lines)
        return "".join(line + "\n" for line in lines)

    def _write(self, text):
        sys.stdout.write(text)
        sys.stdout.flush()

    def _run(self):
        while not self._stop.wait(FRAME_SECONDS):
            try:
                self.redraw()
            except Exception:  # noqa: BLE001 - a frame that cannot be drawn ends the live lines, never the run
                self._stop.set()

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
        """Stops the thread and clears the lines, for good. It does not wait for the thread, which may be waiting for
        the lock the caller holds: once it has the lock it finds the lines closed, draws nothing, and ends."""
        self._stop.set()
        with self._lock:
            if self._open:
                self._open = False
                self._write(self._erase())
                self._display.display = self._show


class Progress:
    """Which blocks of the report can be printed yet, from what the hosts found so far. A section's turn comes once
    every section before it in the report's order has had its own (sections_expected: those this setup can have),
    and it is printed then if every source it is worked out from is read (section_sources: a source is read once
    every host whose findings say it has said them, SOURCE_HOSTS, or for the Proxmox host's Tailscale once it said it
    early). Once every host is read, what is left follows. A section printed early is the one the report play's own
    text has, since it depends on nothing that is not read yet (tests/unit/test_status_filters.py checks it). It
    prints nothing more after a host failed, which leaves the run without a report. `filters` is the filter's module,
    `settings` what status.yml's first play says (setup, types, read_at, now, admin, commands, hosts by label)."""

    def __init__(self, filters, settings, color=False):
        self.f, self.settings, self.color = filters, settings, color
        self.types = settings.get("types") or []
        self.expected = filters.sections_expected(settings.get("setup"), self.types)
        self.hosts = {label: list(names or []) for label, names in (settings.get("hosts") or {}).items()}
        self.found, self.early, self.failed = {}, {}, set()
        self.next, self.printed, self.finished = 0, set(), False
        self._blocks = None

    def update(self, host, found=None, early=None, failed=False):
        """What a host's task said: its findings, its early findings, or that it failed."""
        if found is not None:
            self.found[host] = found
        if early is not None:
            self.early[host] = early
        if failed:
            self.failed.add(host)
        self._blocks = None

    def _read(self, label):
        return all(h in self.found for h in self.hosts.get(label, []))

    def _source_read(self, source):
        early = any(source in e for e in self.early.values())
        return early or all(self._read(label) for label in self.f.SOURCE_HOSTS[source])

    def waiting(self, section):
        """The hosts a section still waits for, by label, in SOURCE_HOSTS' order."""
        labels = []
        for source in self.f.section_sources(section, self.types):
            if not self._source_read(source):
                labels += [label for label in self.f.SOURCE_HOSTS[source] if not self._read(label)]
        return list(dict.fromkeys(labels))

    def all_read(self):
        return all(h in self.found or h in self.failed for names in self.hosts.values() for h in names)

    def model(self):
        found = self.f.merge([*self.early.values(), *self.found.values()])
        settings = self.settings
        return self.f.report(
            found,
            settings.get("setup"),
            self.types,
            settings.get("read_at", ""),
            admin=settings.get("admin") or [],
            commands=settings.get("commands") or {},
            now=settings.get("now") or 0,
        )

    def blocks(self):
        if self._blocks is None:
            model = self.model()
            self._blocks = (model, self.f.blocks(model, self.color))
        return self._blocks

    def head(self):
        return dict(self.blocks()[1])["head"]

    def advance(self):
        """What is to be printed now, as blocks (lists of lines), and once every host is read, the end: (what comes
        before the line on what was read, the exit line's block); None for the end before then, or after a host
        failed."""
        out = []
        if self.finished or self.failed:
            return out, None
        while self.next < len(self.expected) and not self.waiting(self.expected[self.next]):
            section = self.expected[self.next]
            block = dict(self.blocks()[1]).get(section)
            if block:
                out.append(block)
                self.printed.add(section)
            self.next += 1
        if not self.all_read():
            return out, None
        self.finished = True
        rest = [(name, lines) for name, lines in self.blocks()[1] if name not in self.printed and name != "head"]
        out += [lines for name, lines in rest if name in self.f.TYPES]
        before = [line for name, lines in rest if name in ("errors", "attention") for line in lines]
        return out, (before, dict(rest)["exit"])

    def lines(self):
        """The sections still to come, each as (state, name, text): waiting (with the hosts it waits for), read (with
        what it found: its problems and things to do), or not read (with why); then what needs attention."""
        if self.finished:
            return []
        model = self.blocks()[0]
        unread = {a["subject"]: a["text"] for a in model["attention"] if a["level"] == "unread"}
        present = {name for name, _ in self.blocks()[1]}
        out = []
        for section in self.expected[self.next :]:
            labels = self.waiting(section)
            if labels:
                out.append(("waiting", section, "waiting for " + _and(HOSTS.get(label, label) for label in labels)))
                continue
            if section not in present:
                continue
            # A source not read is one of the report's sources not read: the Proxmox host, the cluster, the NAS.
            why = [unread[s] for s in self.f.section_sources(section, self.types) if s in unread]
            if why:
                out.append(("unread", section, why[0]))
                continue
            counts = model["counts"].get(section) or {}
            problems, todo = counts.get("problem", 0), counts.get("todo", 0)
            found = [
                f"{problems} problem{'s' if problems != 1 else ''}" if problems else "",
                f"{todo} to do" if todo else "",
            ]
            text = ", ".join(f for f in found if f) or "nothing to do"
            out.append(("problem" if problems else "todo" if todo else "read", section, f"read · {text}"))
        out.append(("waiting", "attention", "once every section is read"))
        return out


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "homeserver_status"

    def __init__(self):
        super().__init__()
        self._quiet = bool(os.environ.get("STATUS_QUIET"))
        self._live_wanted = os.environ.get("STATUS_LIVE") == "1" and not self._quiet and sys.stdout.isatty()
        self._color = os.environ.get("STATUS_COLOR") == "1"
        # Display's own lock, which the live lines' thread holds while it draws them from what the hosts found.
        self._lock = getattr(self._display, "_lock", None) or threading.RLock()
        self._basedir = Path.cwd()
        self._live = None
        self._progress = None
        self._printed = False
        self._reading = False
        self._started = 0.0
        self._hosts = {}

    def _label(self, host):
        groups = [g.get_name() for g in host.get_groups()]
        return next((LABELS[g] for g in groups if g in LABELS), host.get_name())

    def _entry(self, host):
        new = {"label": self._label(host), "state": "ok", "done": False, "since": self._started, "seconds": 0.0}
        return self._hosts.setdefault(host.get_name(), new)

    def _seen(self, result, state, done=False):
        # Nothing more once the report is printed: a host's play can still end with tasks of Ansible's own.
        if not self._reading or self._printed:
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
        if self._progress is not None and (found is not None or "status_early" in facts or done):
            with self._lock:
                host = result._host.get_name()
                self._progress.update(host, found, facts.get("status_early"), failed=state == "failed")
                self._advance()

    def _spinner(self):
        return SPINNER[int(time.monotonic() / FRAME_SECONDS) % len(SPINNER)]

    def _reading_parts(self, live=False):
        """The line on what was read: each host's state and seconds, or while it is still being read, a spinner and
        its seconds so far."""
        now = time.monotonic()
        parts = [("reading  ", "")]
        # A copy: the main thread adds hosts while the live lines' thread draws them.
        shown = [h for h in list(self._hosts.values()) if not h.get("nothing")]
        for i, h in enumerate(shown):
            if i:
                parts.append((" · ", "d"))
            if live and not h["done"]:
                parts += [(f"{h['label']} ", ""), (self._spinner(), "d"), (f" {now - h['since']:.1f} s", "d")]
            else:
                style = "g" if h["state"] == "ok" else "r"
                parts += [(f"{h['label']} ", ""), (h["state"], style), (f" {h['seconds']:.1f} s", "")]
        return parts if shown else []

    def _line(self):
        return _fit(self._reading_parts(), 10**6, False)

    def _live_lines(self, rows):
        """The live lines: the sections still to come, when the terminal has the rows for them, then the line on what
        is read."""
        reading = [p for p in [self._reading_parts(live=True)] if p]
        progress = self._progress
        sections = progress.lines() if progress is not None else []
        if len(sections) + len(reading) + 2 > rows:
            return reading
        marks = {"waiting": (self._spinner(), "d"), "read": ("✓", "g"), "todo": ("●", "y"), "problem": ("●", "r")}
        out = []
        seconds = time.monotonic() - self._started
        for state, section, text in sections:
            mark = marks.get(state, ("✗", "r"))
            extra = [(f" · {seconds:.1f} s", "d")] if state == "waiting" and section != "attention" else []
            out.append([(" ", ""), mark, (" ", ""), (f"{section.upper():<11}", "h"), (text, "d"), *extra])
        return out + reading

    def _start_live(self):
        if self._live_wanted and self._live is None:
            self._live = Live(self._display, self._live_lines, self._color)

    def _stop_live(self):
        if self._live is not None:
            self._live.close()
            self._live = None

    def _print(self, lines):
        # Ending in a newline of its own, so that Display keeps a block's closing blank line.
        self._display.display("\n".join(lines) + "\n")

    def _advance(self):
        """Prints whatever blocks of the report are ready, and once every host is read, the rest of it."""
        blocks, end = self._progress.advance()
        for lines in blocks:
            self._print(lines)
        if end is not None:
            before, last = end
            self._stop_live()
            self._print([*before, *([self._line()] if self._line() else []), *last])
            self._printed = True
            self._hosts = {}

    def _begin(self, settings):
        """Live, with the report's own filter: its head at once, then each section as the hosts are read."""
        try:
            spec = importlib.util.spec_from_file_location("homeserver_status_filters", self._basedir / FILTERS)
            filters = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(filters)
            self._progress = Progress(filters, settings, self._color)
            self._print(self._progress.head())
        except Exception as exc:  # noqa: BLE001 - without the filter, the line on what is read alone is kept
            self._progress = None
            self._display.display(f"just status: the sections are shown at the end ({exc})", stderr=True)

    def _flush(self):
        """The line on what was read, once, at the end of a run that made no report."""
        self._stop_live()
        if self._line() and not self._quiet:
            self._display.display(self._line())
        self._hosts = {}

    def v2_playbook_on_start(self, playbook):
        self._basedir = Path(playbook._basedir)

    def v2_playbook_on_play_start(self, play):
        self._reading = play.get_name() == READING_PLAY
        self._started = time.monotonic()
        if self._reading:
            self._start_live()

    def v2_runner_on_start(self, host, task):
        if self._reading and not self._printed:
            self._entry(host)

    def v2_runner_on_ok(self, result):
        name = result._task.get_name()
        if name.endswith(SETTINGS_TASK) and self._live_wanted:
            self._begin((result._result.get("ansible_facts") or {}).get("status_settings") or {})
        self._seen(result, "ok")
        if name.endswith(REPORT_TASK) and not self._quiet and not self._printed:
            self._stop_live()
            text = result._result.get("msg", "")
            # The line on what was read goes above the report's last, its exit line.
            body, _, last = text.rstrip("\n").rpartition("\n")
            self._display.display(f"{body}\n{self._line()}\n{last}" if self._line() else text.rstrip("\n"))
            self._hosts = {}

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
