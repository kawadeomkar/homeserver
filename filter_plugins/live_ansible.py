"""A playbook run drawn live: Ansible's events turned into the region, the run's own words, and the summary.

The stdout callback (callback_plugins/homeserver.py) hands every event to an Engine, after the default callback's own
hook, whenever a run is at verbosity 0. Everything a drawn run does is here:

- live, at a terminal that can draw (filter_plugins/live_region.py): the run's model (filter_plugins/run_view.py)
  drawn in place at the bottom of the screen, with the reports, failures, warnings and a dry run's diffs printed
  above it for good;
- lines, any other output: a line as each step ends, and the same reports and failures.

Either way the default callback still renders every task, sent to the run's log alone (Display's log_only), so the
log is what it would be without the engine; the run's own words go to the screen in colour and to the log without;
and a summary replaces PLAY RECAP on the screen, which the log keeps, followed by NEXT STEPS. What a run is made of,
its stages and steps, is its playbook's declaration (filter_plugins/live_recipes.py); nothing here names a command.

A drawn run must have a log, since its stream goes there: a run that just did not start, without ANSIBLE_LOG_PATH,
gets one in logs/, named by scripts/run_log.py as just names its own.

Adapted from claude-on-proxmox's filter_plugins/live_ansible.py (commit 55e53c2), without what is that project's own:
VM fleets, runs in parts, browser waits, the person's answers and re-raised failures. Loaded by path as
homeserver_live_ansible; it has no filters of its own.
"""

from __future__ import annotations

import atexit
import importlib.util
import logging
import os
import re
import sys
import time
from pathlib import Path

from ansible import constants as C
from ansible import context
from ansible.utils import display as ansible_display

ROOT = Path(__file__).resolve().parents[1]


def _sibling(stem):
    """filter_plugins/<stem>.py, loaded by path once as homeserver_<stem>."""
    name = f"homeserver_{stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().with_name(f"{stem}.py"))
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


def _script(stem, path):
    """scripts/<path>, loaded by path once as homeserver_<stem>: vars_plugins/setup_links.py loads setups.py so too."""
    name = f"homeserver_{stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / path)
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


text = _sibling("text")
screen = _sibling("screen")
view = _sibling("run_view")
live = _sibling("live_region")
recipes = _sibling("live_recipes")

DEBUG_ACTIONS = frozenset({"debug", "ansible.builtin.debug", "ansible.legacy.debug"})
ASSERT_ACTIONS = frozenset({"assert", "ansible.builtin.assert", "ansible.legacy.assert"})
INCLUDE_TASKS = frozenset({"include_tasks", "ansible.builtin.include_tasks", "ansible.legacy.include_tasks"})
IMPORT_TASKS = frozenset({"import_tasks", "ansible.builtin.import_tasks", "ansible.legacy.import_tasks"})
INCLUDE_ROLE = frozenset({"include_role", "ansible.builtin.include_role", "ansible.legacy.include_role"})
ARGUMENT_CHECKS = frozenset({"validate_argument_spec", "ansible.builtin.validate_argument_spec"})
FAILURE_LINES = 20
# A run this long rings the bell when it ends: the person may have looked away. One that failed always does.
BELL_SECONDS = 60
# A task name's file prefix, as an imported file's tasks are named: "node | Report what the admin console must fix".
FILE_PREFIX = re.compile(r"^[a-z0-9_]+ \| ")
LOG_FORMAT = "%(asctime)s p=%(process)d u=%(user)s n=%(name)s %(levelname)s| %(message)s"


def from_callback(callback, playbook):
    """The engine for this run, started, or None for the stream: at verbosity 0, with a log to send the stream to."""
    if callback._display.verbosity != 0:
        return None
    if ansible_display.logger is None and not _own_log(playbook):
        return None
    engine = Engine(callback)
    try:
        engine.start(playbook)
    except Exception as error:  # noqa: BLE001 - a run that cannot be drawn is the stream, Display let go
        engine.release(error)
        return None
    return engine


def _own_log(playbook):
    """A log for a run started without one, as just names its own, created 0600 in a 0700 logs/ (logs hold
    addresses), set up as Ansible sets up its own; False when it cannot be made."""
    try:
        cli = context.CLIARGS or {}
        path = _script("run_log", "run_log.py").create(ROOT, *_command(playbook, cli))
        logging.basicConfig(filename=str(path), level=logging.INFO, format=LOG_FORMAT)
        for handler in logging.root.handlers:
            handler.addFilter(ansible_display.FilterDenyList(getattr(C, "DEFAULT_LOG_FILTER", [])))
            handler.addFilter(ansible_display.FilterUserInjector())
        ansible_display.logger = logging.getLogger("ansible")
        os.environ["ANSIBLE_LOG_PATH"] = str(path)
        return True
    except Exception:  # noqa: BLE001 - without a log, the run is the stream, as before
        return False


def _command(playbook, cli):
    """What a run is, as a log's name says it: (recipe, playbook, check, tags), as just's _playbook passes them."""
    file = Path(str(playbook._file_name)).name
    stem = file.rsplit(".", 1)[0]
    recipe = recipes.recipe_for(file)
    check = bool(cli.get("check"))
    name = (recipe.check_name if check else recipe.name) if recipe else stem
    return name, stem, check, ",".join(t for t in cli.get("tags") or () if t != "all")


def title_of(playbook, cli):
    """The command a run is: deploy proxmox, check under --check, deploy truenas --tags truenas_pools; for a
    playbook no declaration names, its file's name."""
    file = Path(str(playbook._file_name)).name
    stem = file.rsplit(".", 1)[0]
    recipe = recipes.recipe_for(file)
    check = bool(cli.get("check"))
    if recipe:
        words = [recipe.check_name if check else recipe.name] + ([] if stem == "site" else [stem])
    else:
        words = [stem] + (["--check"] if check else [])
    tags = [t for t in cli.get("tags") or () if t != "all"]
    if tags:
        words.append("--tags " + ",".join(tags))
    skip = list(cli.get("skip_tags") or ())
    if skip:
        words.append("--skip-tags " + ",".join(skip))
    return " ".join(words)


def setup_name():
    """The named setup this checkout links to, or "" for its own files, as the log's name says it."""
    try:
        return _script("run_log", "run_log.py").setup_name(ROOT)
    except Exception:  # noqa: BLE001 - the header names the setup when it can; vars_plugins/setup_links.py refuses
        return ""


# --- what a play compiled to -------------------------------------------------------------------------------------
def tasks_of(blocks, out, play=None, search=()):
    """Each task a play compiled to, as (its ID, its role's name or None, meta), blocks' rescue and always included,
    and only those --tags and --skip-tags leave, as the play's iterator filters them. meta: file, the task file it
    comes from; include, for an include whose file is literal, how many tasks it will announce."""
    for block in blocks:
        for part in (block.block, block.rescue, block.always):
            for item in part or []:
                if hasattr(item, "block"):
                    tasks_of([item], out, play, search)
                elif not getattr(item, "implicit", False) and tagged_in(item, play):
                    out.append((item._uuid, role_of(item), {**_include_meta(item, search), "file": task_file(item)}))
    return out


def _include_meta(task, search):
    """For an include whose file a playbook names literally: the count of the tasks it will announce."""
    args = getattr(task, "args", None) or {}
    try:
        if task.action in INCLUDE_TASKS:
            target = args.get("file") or args.get("_raw_params")
            if not isinstance(target, str) or "{{" in target:
                return {}
            base = os.path.dirname(str(task.get_path()).split(":", 1)[0])
            path = target if os.path.isabs(target) else os.path.join(base, target)
            return {"include": view.count_tasks(path)} if os.path.isfile(path) else {}
        if task.action in INCLUDE_ROLE:
            name, tasks_from = args.get("name"), args.get("tasks_from")
            if not isinstance(name, str) or "{{" in name or "{{" in str(tasks_from or ""):
                return {}
            path = view.role_tasks_file(name.split(".")[-1], tasks_from, search)
            return {"include": view.count_tasks(path)} if path else {}
    except Exception:  # noqa: BLE001 - a count is a nicety: an include whose file cannot be read is counted as it runs
        return {}
    return {}


def tagged_in(task, play):
    if play is None:
        return True
    try:
        return bool(task.evaluate_tags(play.only_tags, play.skip_tags, all_vars={}))
    except Exception:  # noqa: BLE001 - tags that need a variable to say: counted, as a run may reach them
        return True


def role_of(task):
    return task._role.get_name(include_role_fqcn=False) if getattr(task, "_role", None) else None


def drawn_name(task):
    """A task's name as drawn: without its role's prefix, which its step says, nor its file's ("node | ")."""
    name = task.get_name().strip()
    role = role_of(task)
    if role and name.startswith(role + " : "):
        name = name[len(role) + 3 :]
    return FILE_PREFIX.sub("", name)


def origin(task):
    """Where a task is written, file:line, as Ansible's Origin says it."""
    path = str(task.get_path() or "")
    file, sep, line = path.rpartition(":")
    return f"{relative(file)}:{line}" if sep and line.isdigit() else relative(path)


def task_file(task):
    """The task file a task comes from as its play reaches it: the file the import or include nearest the play names,
    so a file another file imports stays its importer's; else the task's own file."""
    own = str(task.get_path() or "").split(":", 1)[0]
    outer, node = None, getattr(task, "_parent", None)
    while node is not None:
        if getattr(node, "action", None) in INCLUDE_TASKS | IMPORT_TASKS:
            outer = node
        node = getattr(node, "_parent", None)
    if outer is None:
        return own
    args = getattr(outer, "args", None) or {}
    target = args.get("file") or args.get("_raw_params")
    if not isinstance(target, str) or "{{" in target:
        return own
    base = os.path.dirname(str(outer.get_path() or "").split(":", 1)[0])
    return os.path.normpath(os.path.join(base, target))


def from_include(task):
    """Whether a task came from a dynamic include, whose tasks the include's count covers."""
    node = getattr(task, "_parent", None)
    while node is not None:
        if getattr(node, "action", None) in INCLUDE_TASKS | INCLUDE_ROLE:
            return True
        node = getattr(node, "_parent", None)
    return False


def rescued(task, play=None):
    """Whether a block's rescue takes the task's failure: the task is among that block's own tasks, at any depth, and
    the rescue has a task the play's tags leave. Any link between the task and the block may be the one the block
    lists: a static import puts the file's own block there, under a TaskInclude that no block lists."""
    below, node = [], task
    while node is not None:
        below.append(getattr(node, "_uuid", None))
        parent = getattr(node, "_parent", None)
        if parent is not None and getattr(parent, "rescue", None) and _keeps(parent.rescue, play):
            ids = set(below)
            if not ids & (_uuids(parent.rescue) | _uuids(parent.always)) and ids & _uuids(parent.block):
                return True
        node = parent
    return False


def _uuids(items):
    return {getattr(item, "_uuid", None) for item in items or []}


def _keeps(items, play):
    for item in items or []:
        if hasattr(item, "block"):
            if any(_keeps(part, play) for part in (item.block, item.rescue, item.always)):
                return True
        elif tagged_in(item, play):
            return True
    return False


def relative(path):
    """A path as a person reads it here: relative to the working directory when under it."""
    try:
        shown = os.path.relpath(path)
    except ValueError:
        return str(path)
    return str(path) if shown.startswith("..") else shown


def log_line(path, host):
    """The line of the log where a host's last failure starts (1 for the first), or 0 when not found."""
    marks = (f"fatal: [{host}]".encode(), f"failed: [{host}]".encode(), f"[{host}]: UNREACHABLE!".encode())
    try:
        lines = Path(path).read_bytes().splitlines()
    except OSError:
        return 0
    for number in range(len(lines) - 1, -1, -1):
        if any(mark in lines[number] for mark in marks):
            return number + 1
    return 0


class Engine:
    """One drawn run: the callback's events in; the region, the run's own words and the summary out."""

    def __init__(self, callback):
        self.cb = callback
        self.display = callback._display
        self.mode = "live" if live.can_draw() else "lines"
        self.run = None
        self.out = live.Output(
            self.display,
            frame=self._frame,
            live=self.mode == "live",
            color=not C.ANSIBLE_NOCOLOR,
            to_log=True,
            on_fault=self.release,
        )
        self.released = False  # let go after a fault: the callback prints classically from then on
        self.search = []  # where a role's tasks are looked for: beside the playbook, then Ansible's roles path
        self.color = self.out.color
        self.log = ""  # the log, as the summary names it
        self.log_path = ""
        self._failed_items = {}  # (task ID, host): the results of a loop's items that failed, said with the task
        self._blank = False  # whether the last line the run printed of its own was blank

    # --- what the run says, and where it goes --------------------------------------------------------------
    def say(self):
        return self.out.say()

    def print(self, lines):
        """Lines of the run's own, (coloured, plain) each: the colour to the screen, the plain text to the log."""
        if lines:
            self.out.print(lines)
            self._blank = not lines[-1][1].strip()

    def _width(self):
        try:
            return os.get_terminal_size(sys.stdout.fileno()).columns
        except (OSError, ValueError, AttributeError):
            return 100

    @staticmethod
    def _now():
        return time.monotonic()

    def _wrapped(self, head, lines, style=""):
        """head, then lines wrapped under where head ends, whole, never cut: (coloured, plain) pairs for print. A line
        keeps its own indent, its wrapped pieces under where its words start."""
        column = screen.width(head)
        room = max(20, self._width() - 1 - column)
        out = []
        for number, line in enumerate(lines):
            indent = len(line) - len(line.lstrip(" "))
            pieces = screen.fill(line.strip(), max(10, room - indent)) if line.strip() else [""]
            for piece_number, piece in enumerate(pieces):
                lead = head if number == 0 and piece_number == 0 else [(" " * column, "")]
                runs = [*lead, (" " * indent + piece, style)]
                out.append((screen.render(runs, self.color), screen.render(runs, False)))
        return out

    def _frame(self, cols, rows, glyph):
        with self.display._lock:
            if self.run is None:
                return []
            now = self._now()
            self.out.title(self.run.title_text(now))
            # A column short: a line as wide as the terminal leaves the cursor on its last column, which the
            # region's erase to the end of the line would take with it.
            return self.run.frame(now, cols - 1, rows, glyph, self.color)

    def _after(self):
        """After each event: off a terminal, a line for each step that ended; live, a redraw now."""
        if self.mode == "lines":
            ended = self.run.ended_lines(self._now(), self._width())
            if ended:
                self.print([(line, line) for line in ended])
        else:
            self.out.redraw()

    def release(self, error):
        """A fault in the drawing: the region erased, the stream back on the screen for the rest of the run, and one
        line on stderr. Every event so far is in the log, which the default callback's hook wrote first."""
        if self.released:
            return
        self.released = True
        try:
            self.out.release()
        finally:
            self.display.display(
                f"[homeserver] the live output stopped ({type(error).__name__}: {error}); the rest of the run prints "
                f"as Ansible's own{f', and all of it is in {self.log}' if self.log else ''}",
                stderr=True,
            )

    # --- the playbook --------------------------------------------------------------------------------------
    def start(self, playbook):
        cli = context.CLIARGS or {}
        self.log_path = os.environ.get("ANSIBLE_LOG_PATH") or C.DEFAULT_LOG_PATH or ""
        self.log = relative(self.log_path) if self.log_path else ""
        file = Path(str(playbook._file_name)).name
        self.run = view.Run(
            title_of(playbook, cli),
            self._now(),
            check=bool(cli.get("check")),
            log=self.log,
            setup=setup_name(),
            recipe=recipes.recipe_for(file),
        )
        self.search = [os.path.join(os.path.dirname(os.path.abspath(str(playbook._file_name))), "roles")]
        self.search += list(C.DEFAULT_ROLES_PATH)
        search = self.search
        plays = []
        for play in playbook.get_plays():
            try:
                tasks = tasks_of(play.compile(), [], play, search)
            except Exception:  # noqa: BLE001 - a count is a nicety: a play that will not compile here still runs
                tasks = []
            plays.append((play._uuid, play.get_path(), tasks, play.get_name()))
        self.run.plays(plays)
        self.out.start()
        self.out.redraw()
        self.out.spin()
        if self.mode == "live":
            # Registered after the region's own, so it runs first: a run that ends without its stats, at Ctrl-C,
            # does not leave the tab saying it is still at work.
            atexit.register(self._stopped_title)

    def _stopped_title(self):
        if self.run is not None and self.run.finished is None:
            self.out.title(f"{self.run.title} · stopped")

    def play_start(self, play):
        with self.display._lock:
            self.run.play_start(play._uuid)
            self._after()

    def task_start(self, task):
        with self.display._lock:
            play = getattr(self.cb, "_play", None)
            counted = task._uuid in self.run.plan or from_include(task)
            self.run.task(
                task._uuid,
                drawn_name(task),
                role_of(task),
                self._now(),
                play=play._uuid if play is not None else None,
                hidden=bool(getattr(task, "implicit", False)) or task.action in ARGUMENT_CHECKS,
                file=task_file(task),
                path=origin(task),
                counted=counted,
            )
            self._after()

    def include(self, included_file):
        with self.display._lock:
            task = included_file._task
            if getattr(included_file, "_is_role", False):
                role = getattr(task, "_role_name", None) or str(included_file._filename)
                tasks_from = (getattr(task, "_from_files", None) or {}).get("tasks", "main")
                path = view.role_tasks_file(role, tasks_from, self.search)
            else:
                path = str(included_file._filename)
            self.run.include(task._uuid, view.count_tasks(path) if path else 0)

    # --- results -------------------------------------------------------------------------------------------
    def _result(self, result, outcome):
        with self.display._lock:
            self.run.result(result.task._uuid, result.host.get_name(), outcome, self._now())
            self._after()

    def ok(self, result, changed=False):
        """A task's result on a host. changed: as it came, before the default callback's hook cleaned a debug's."""
        self._result(result, "changed" if changed or result.result.get("changed") else "ok")
        # A stage this setup lacks: its absent task has run, which it does only then. Its start says nothing, since
        # Ansible announces a task before it checks its when.
        with self.display._lock:
            task = self.run.find(result.task._uuid)
            if task is not None and task.name == self.run.stages[task.stage].declared.absent:
                self.run.absent(task.key)
        # A looped report's words are its items'; its own msg is the loop's "All items completed".
        if result.task.action in DEBUG_ACTIONS and "results" not in result.result:
            self._report(result)

    def item_ok(self, result):
        with self.display._lock:
            self.run.item(result.task._uuid)
        if result.task.action in DEBUG_ACTIONS:
            self._report(result)

    def skipped(self, result):
        self._result(result, "skipped")

    def _report(self, result):
        """A debug message, a report: whole above the region, each of its lines kept as a line and wrapped under its
        own indent, after its stage (its step, in a run of one stage); its first line is the summary's."""
        msg = result.result.get("msg")
        if msg in (None, ""):
            return
        parts = msg if isinstance(msg, list) else str(msg).splitlines()
        # Each line as it reads, indent and all: no escape, control or format character is left.
        lines = [text.visible_text(str(part)).rstrip().expandtabs(4) for part in parts]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return
        with self.display._lock:
            task = self.run.find(result.task._uuid)
            self.run.note(task, text.one_line(lines[0], 200))
            if task is None:
                label = ""
            elif self.run.flat() and task.step is not None:
                label = self.run.stages[task.stage].steps[task.step].label
            else:
                label = self.run.stages[task.stage].label
        self.print(self._wrapped([("  ", ""), (label.ljust(view.LABEL), "b")], lines))

    def failed(self, result, ignore_errors=False):
        outcome = "ignored" if ignore_errors else "rescued" if rescued(result.task, self.cb._play) else "failed"
        self._result(result, outcome)
        if outcome == "failed":
            self._failure(result, "")
        else:
            with self.display._lock:
                self._failed_items.pop((result.task._uuid, result.host.get_name()), None)

    def item_failed(self, result):
        with self.display._lock:
            self._failed_items.setdefault((result.task._uuid, result.host.get_name()), []).append(result.result)

    def unreachable(self, result):
        ignored = bool(getattr(result.task, "ignore_unreachable", False))
        self._result(result, "ignored" if ignored else "unreachable")
        if not ignored:
            self._failure(result, " · could not be reached")

    def _failure(self, result, how):
        """A failed task, for good above the region: where, on which host, the task, what it said, and where its
        file and the log have it. A failed assert is a refusal: its message alone, never the assertion."""
        host = result.host.get_name()
        with self.display._lock:
            task = self.run.find(result.task._uuid)
            where = self.run.where(task) if task is not None else ""
            took = f" ({screen.duration(task.seconds(self._now()))})" if task is not None else ""
            name = task.name if task is not None else drawn_name(result.task)
            origin = task.path if task is not None else ""
        refusal = result.task.action in ASSERT_ACTIONS
        said = self._said(result.result, refusal)
        # A loop's own msg is "One or more items failed": what each failed item said is the failure.
        for item in self._failed_items.pop((result.task._uuid, host), []):
            said.append(f"{text.one_line(self.cb._get_item_label(item))}:")
            said += [f"  {line}" for line in self._said(item, refusal)]
        head = [("✗ ", "r"), (f"{where} · {host}: {name}{took}{how}" if where else f"{host}: {name}{took}{how}", "r")]
        if refusal:
            head.append(("  refused", "d"))
        out = [("", "")]
        out.append((screen.render(head, self.color), screen.render(head, False)))
        out += self._wrapped([("    ", "")], [line for line in said if line.strip()][: FAILURE_LINES * 2])
        found = log_line(self.log_path, host) if self.log_path else 0
        place = " · ".join(part for part in (origin, f"log line {found}" if found else "") if part)
        if place:
            out += self._wrapped([("    ", "")], [place], "d")
        self.print([*out, ("", "")])

    @staticmethod
    def _said(res, refusal=False):
        """What a failed result says: its message, then the last lines of its output (a module's own, should it have
        crashed), never the command it ran."""
        said = []
        for key in ("msg", "reason"):
            if res.get(key):
                # A message is prose, often templated over several lines of YAML: each paragraph one line, wrapped
                # where it is printed.
                for paragraph in re.split(r"\n\s*\n", text.plain_text(str(res[key]))):
                    if paragraph.strip():
                        said.append(" ".join(paragraph.split()))
        if refusal:
            return said
        for key in ("stderr_lines", "module_stderr", "stdout_lines", "module_stdout"):
            value = res.get(key)
            if value:
                lines = value if isinstance(value, list) else str(value).splitlines()
                said += [text.plain_text(str(line)) for line in lines][-FAILURE_LINES:]
                break
        return said

    def retry(self, result):
        """A retried task: its line says which try of how many, and how long it may wait; its retries go to the log."""
        try:
            delay = int(result.task.delay)
        except (TypeError, ValueError):
            delay = 0
        with self.display._lock:
            attempt, total = result.result.get("attempts", 0), result.result.get("retries", 0)
            self.run.tried(result.task._uuid, self._now(), attempt + 1, total, delay)
            self._after()

    # --- the end -------------------------------------------------------------------------------------------
    def stats(self, stats, next_lines):
        """The end: the region gone, the summary and NEXT STEPS (next_lines, given a width) on the screen and in the
        log, after PLAY RECAP, which the default callback's hook sent to the log alone. The exit code is worked out
        as ansible-playbook works out its own: 4 when a host could not be reached, else 2 when one failed."""
        now = self._now()
        code = 4 if stats.dark else 2 if stats.failures else 0
        with self.display._lock:
            self.run.end(now, code)
            if self.mode == "live":
                self.out.title(self.run.title_text(now))
        if self.mode == "lines":
            self._after()
        self.out.finish()
        width = self._width() - 1
        summary = self.run.summary(now, width, next_lines(width), self.color)
        self.print([*([] if self._blank else [("", "")]), *summary])
        if self.mode == "live" and (code or now - self.run.started >= BELL_SECONDS):
            self.out.bell()


class FilterModule:
    """No filters: the stdout callback loads this module by path. Here so the filter loader takes the file."""

    def filters(self):
        return {}
