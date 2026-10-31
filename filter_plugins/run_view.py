"""The model of a playbook run drawn live: its stages, steps and tasks, its frame, its lines piped, its summary.

filter_plugins/live_ansible.py feeds a Run from the stdout callback's events; this module only keeps what they said
and draws it. A run is its declaration's (filter_plugins/live_recipes.py) stages and steps, or, for a playbook no
declaration names, a stage per play and its roles as steps (generic). Every task Ansible compiled is counted at the
start, so each row knows how far it is; a row that has ended reads whole, and no bar or percentage ever moves back.

Drawn three ways, each from the model alone, given the time and the width: the frame the live region redraws at the
bottom of a terminal (frame), a line as each step ends off a terminal (ended_lines), and the summary that replaces
PLAY RECAP on the screen (summary). Every line is built with screen.py and cut to the width, so none wraps.

Adapted from claude-on-proxmox's filter_plugins/run_view.py (commit 55e53c2), without what is that project's own:
VM fleets, runs in parts, sign-ins, checklists and the person's answers. Pure, nothing read but the task files an
include names. Loaded by path as homeserver_run_view; it has no filters of its own.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import time
from pathlib import Path

import yaml


def _sibling(stem):
    """filter_plugins/<stem>.py, loaded by path once as homeserver_<stem>."""
    name = f"homeserver_{stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().with_name(f"{stem}.py"))
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


screen = _sibling("screen")
text = _sibling("text")
recipes = _sibling("live_recipes")

Line = screen.Line
# A skipped row's or task's mark, an en dash.
DASH = "\u2013"
# A task's outcome as the window draws it, and how bad it is: a host's worst outcome is the task's.
GLYPHS = {
    "ok": ("✓", "g"),
    "changed": ("●", "y"),
    "skipped": (DASH, "d"),
    "failed": ("✗", "r"),
    "unreachable": ("✗", "r"),
    "ignored": ("↺", "d"),
    "rescued": ("↺", "d"),
}
SEVERITY = ("skipped", "ok", "changed", "ignored", "rescued", "failed", "unreachable")
# A row's state as its glyph: a spinner for the running one, filled in by the frame.
ROW_GLYPHS = {
    "done": ("✓", "g"),
    "skipped": (DASH, "d"),
    "absent": (DASH, "d"),
    "failed": ("✗", "r"),
    "todo": ("○", "d"),
    "unrun": ("○", "d"),
    "none": (DASH, "d"),
}
# The summary names the tasks that changed something, but not a report's: a report says its change itself.
REPORT_NAME = re.compile(r"^(Show|Say|Report|Record) ")
LABEL = 15
# How much of each report's first line the summary keeps.
NOTE = 72
NARROW = 90
FIRST_SENTENCE = re.compile(r"(.+?[.!?:])(\s+\S)")


def first_sentence(said):
    """said up to the end of its first sentence or clause, or whole when it has none."""
    found = FIRST_SENTENCE.match(said)
    return found.group(1).rstrip(":") if found else said


# --- what a play compiled to -----------------------------------------------------------------------------------
def count_tasks(path, depth=0):
    """The tasks a task file runs, as Ansible will start them: a block's children, an import's file's tasks, an
    include as one task (its own file is counted when it is included). 0 for a file that cannot be read."""
    if depth > 8:
        return 0
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or []
    except (OSError, yaml.YAMLError):
        return 0
    return _count(data, os.path.dirname(path), depth)


def _count(items, base, depth):
    total = 0
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if "block" in item:
            total += sum(_count(item.get(key) or [], base, depth) for key in ("block", "rescue", "always"))
            continue
        imported = item.get("ansible.builtin.import_tasks") or item.get("import_tasks")
        if isinstance(imported, str) and "{{" not in imported:
            total += count_tasks(os.path.join(base, imported), depth + 1)
            continue
        total += 1
    return total


def role_tasks_file(name, tasks_from, search):
    """Where a role's tasks are: tasks/<tasks_from>.yml of the first role directory that has them."""
    for directory in search:
        for suffix in (".yml", ".yaml", ""):
            path = os.path.join(directory, name, "tasks", f"{tasks_from or 'main'}{suffix}")
            if os.path.isfile(path):
                return path
    return ""


def generic(plays):
    """The declaration of a playbook no declaration names: a stage per play, labelled with its name, and its roles,
    in the order its tasks reach them, as its steps; a play without roles has one step, its tasks."""
    stages = []
    for _key, path, tasks, name in plays:
        file = Path(str(path).split(":", 1)[0]).name
        roles = []
        for _task, role, _meta in tasks:
            if role and role not in roles:
                roles.append(role)
        steps = [recipes.Step(role, roles=(role,)) for role in roles] or [
            recipes.Step("tasks", plays=(f"{file}:{name}",))
        ]
        stages.append(recipes.Stage(name or file, plays=(f"{file}:{name}",), steps=steps))
    return recipes.Recipe("", stages=stages)


# --- the run's parts ---------------------------------------------------------------------------------------------
class Task:
    """A task as it ran: when, its outcome over every host, its retries and loop items. number: its place among the
    run's tasks, as they started. part: the row of its include's item, for a step whose items are steps (each)."""

    def __init__(self, key, name, stage, step, start, hidden=False, path="", number=0, part=None, ends=False):
        self.key = key
        self.name = name
        self.stage = stage
        self.step = step
        self.part = part
        self.number = number
        self.ends = ends  # a play's own end_host or end_play, which says nothing when it ends its host
        self.start = start
        self.end = None
        self.hidden = hidden
        self.path = path  # file:line, as Ansible's Origin says it
        self.hosts = {}
        self.outcome = None
        self.retry = None  # (attempt, total, delay, since)
        self.items = 0

    def result(self, host, outcome, now):
        self.hosts[host] = outcome
        self.outcome = max(self.hosts.values(), key=SEVERITY.index)
        self.end = now

    def close(self, now):
        if self.end is None:
            self.end = now
        if self.outcome is None:
            self.outcome = "ok"

    def running(self):
        return self.end is None

    def seconds(self, now):
        return (self.end if self.end is not None else now) - self.start


class Row:
    """A stage, or a step of one, or an item of a step whose items are steps: its tasks, how many it expects, and
    whether it has ended."""

    def __init__(self, label, each=False, names=None):
        self.label = label
        self.tasks = []
        self.expected = 0
        self.ended = False
        self.absent = None  # what the stage's absent task said, once it has run
        self.left = False  # the stage's play ended its host before its work: not in this run
        self.reason = ""  # why, as the report its play printed just before said it
        self.waiting = False  # the stage has run, and a play of its comes later: drawn as to come
        self.shown = 0.0  # the largest fraction drawn, so a bar never moves back
        self.each = each
        self.names = dict(names or {})
        self.parts = []  # an each step's items, each a row, as its include brings them

    @property
    def reached(self):
        return len(self.tasks)

    def changed(self):
        return sum(1 for t in self.tasks if t.outcome == "changed")

    def failed(self):
        return sum(1 for t in self.tasks if t.outcome in ("failed", "unreachable"))

    def ran(self):
        return sum(1 for t in self.tasks if t.outcome not in (None, "skipped"))

    def seconds(self, now):
        """From its first task's start to its last's end, less the time between when another row's tasks ran: a
        stage whose plays are apart, or a step its stage comes back to."""
        total, first, last = 0.0, None, None
        for task in self.tasks:
            if first is None or task.number != last.number + 1:
                if first is not None:
                    total += _ended(last, now) - first.start
                first = task
            last = task
        return total + (_ended(last, now) - first.start if first is not None else 0.0)

    def state(self, over):
        if self.absent is not None:
            return "absent"
        if self.left:
            return "none"
        if self.failed():
            return "failed"
        if not self.tasks:
            # A step none of whose tasks this run has, as --tags leaves it, never runs.
            return "none" if self.expected == 0 else "unrun" if over else "todo"
        if self.ended or over:
            return "done" if self.ran() else "skipped"
        return "todo" if self.waiting else "running"

    def fraction(self):
        """How far the row is, from 0 to 1, never less than it was drawn."""
        now = 1.0 if self.ended or self.absent is not None else self.reached / max(self.expected, self.reached, 1)
        self.shown = max(self.shown, now)
        return self.shown


class Stage(Row):
    def __init__(self, declared):
        super().__init__(declared.label)
        self.declared = declared
        self.steps = [Row(step.label, step.each, step.names) for step in declared.steps]
        self.whole = None  # the declared report's lines, for the summary
        self.said = None  # the place in the run's notes of the last report its running play printed

    def rows(self):
        """Its steps as drawn: each step, or the items of a step whose items are steps once its include has run."""
        return [row for step in self.steps for row in (step.parts or [step])]


def _ended(task, now):
    return task.end if task.end is not None else now


# --- the run -----------------------------------------------------------------------------------------------------
class Run:
    """One run. title: the command it is (deploy proxmox); setup: the named setup's name, or ""; log: the run log's
    path as shown; options: the recipe's options given, by variable, as -e gave them."""

    def __init__(self, title, now, check=False, log="", setup="", started_at=None, recipe=None, options=None):
        self.title = title
        self.options = dict(options or {})
        self.started = now
        self.started_at = time.localtime() if started_at is None else started_at
        self.check = check
        self.log = log
        self.setup = setup
        self.recipe = recipe
        self.stages = []
        self.tasks = {}  # key: Task
        self.order = []  # every Task, as they started
        self.plan = {}  # task key: (stage, step), from what the plays compiled to
        self.play_stage = {}  # play key: stage index, None for a play of a stage not drawn
        self.play_path = {}  # play key: "file:play name"
        self.started_plays = set()
        self.reserved = {}  # include key: the tasks it was counted with at the start, until it is included
        self.include_into = {}  # include key: (stage, step) its file's tasks are in
        self.current = None
        self.finished = None
        self.failure = None  # (where, seconds) of the first failure
        self.code = 0
        self.notes = []  # (stage index, first line of a report)
        self._said = set()  # the rows ended_lines has said

    # --- what the plays compiled to -------------------------------------------------------------------------
    def plays(self, plays, recipe=None):
        """Every play of the run, as (key, path, tasks, name), each task (key, role, meta): its stages, and what each
        step expects. meta: file (the task file it comes from), include (an include's file's count, when literal)."""
        given = recipe or self.recipe or generic(plays)
        # A copy, since a play the declaration does not name adds a stage to this run's alone.
        declared = recipes.Recipe(given.name, given.playbooks, given.stages, given.check_name, given.window)
        self.recipe = declared
        wanted = []
        for key, path, tasks, name in plays:
            file = Path(str(path).split(":", 1)[0]).name
            self.play_path[key] = f"{file}:{name}"
            index = self._declared_stage(declared, file, name)
            if index is None:
                # A play the declaration does not name: a stage of its own, as a playbook no declaration names.
                extra = generic([(key, path, tasks, name)]).stages[0]
                declared.stages = (*declared.stages, extra)
                index = len(declared.stages) - 1
            when = declared.stages[index].when
            if when and not truthy(self.options.get(when)):
                # A stage only an option draws: its plays' tasks are the log's alone.
                self.play_stage[key] = None
                continue
            self.play_stage[key] = index
            if index not in wanted:
                wanted.append(index)
        # Only the stages this run's plays reach, in the declaration's order.
        order = sorted(wanted)
        self.stages = [Stage(declared.stages[i]) for i in order]
        position = {declared_index: i for i, declared_index in enumerate(order)}
        self.play_stage = {key: None if i is None else position[i] for key, i in self.play_stage.items()}
        for key, _path, tasks, _name in plays:
            stage = self.play_stage[key]
            if stage is None:
                continue
            steps = [self._step(stage, self.play_path[key], role, meta.get("file", "")) for _t, role, meta in tasks]
            steps = _neighbours(steps)
            for (task, role, meta), step in zip(tasks, steps, strict=True):
                self.plan[task] = (stage, step)
                self._expect(stage, step, 1)
                if meta.get("include"):
                    # Its file's tasks, in the step that file is: an include in one step may bring another's tasks.
                    into = self._step(stage, self.play_path[key], role, meta.get("included", ""))
                    self.include_into[task] = (stage, step if into is None else into)
                    self.reserved[task] = int(meta["include"])
                    self._expect(*self.include_into[task], int(meta["include"]))

    def _expect(self, stage, step, count):
        self.stages[stage].expected += count
        if step is not None:
            self.stages[stage].steps[step].expected += count

    @staticmethod
    def _declared_stage(declared, file, name):
        for index, stage in enumerate(declared.stages):
            if f"{file}:{name}" in stage.plays or file in stage.plays:
                return index
            if any(f"{file}:{name}" in step.plays for step in stage.steps):
                return index
        return None

    def _step(self, stage, play, role, file):
        """The step of a stage a task is in: by its play, its role or its task file; None for none."""
        steps = self.stages[stage].declared.steps
        for index, step in enumerate(steps):
            if play in step.plays:
                return index
        for index, step in enumerate(steps):
            if role and role in step.roles:
                return index
        for index, step in enumerate(steps):
            if file and any(file.endswith(os.sep + end) or file == end for end in step.files):
                return index
        return None

    def _part(self, stage, step, item, count=None):
        """The row of an include's item in a step whose items are steps: the first of that label still to run, or a
        new one, expecting count tasks, when the include brings it."""
        row = self.stages[stage].steps[step]
        label = row.names.get(item, item)
        if count is None:
            found = [part for part in row.parts if part.label == label]
            for part in found:
                if part.reached < part.expected:
                    return part
            if found:
                return found[-1]
        part = Row(label)
        part.expected = count or 0
        row.parts.append(part)
        return part

    # --- the events -----------------------------------------------------------------------------------------
    def play_start(self, key, now=None):
        self._close(now)
        self.started_plays.add(key)
        stage = self.play_stage.get(key)
        if stage is not None:
            # A play of a stage another of its plays left: whether this one leaves it too is still to be seen.
            self.stages[stage].left = False
            self.stages[stage].said = None
        self._move(stage)

    def _close(self, now):
        """The running task ends with the next event. One that ended its host said nothing: its stage is left out of
        this run, unless its absent task said why, with the reason the report its play printed last gave."""
        task = self.current
        if task is None or not task.running() or now is None:
            return
        fired = task.outcome is None
        task.close(now)
        stage = self.stages[task.stage]
        if task.ends and fired and stage.absent is None:
            stage.left = True
            if stage.said is not None:
                # The play's last words: the notes' last, since its play is the one running.
                stage.reason = first_sentence(self.notes.pop(stage.said)[1])
                stage.said = None

    def task(
        self, key, name, role, now, play=None, hidden=False, file="", path="", counted=True, item=None, ends=False
    ):
        """A task starts. counted: whether the start counted it, as a compiled task or an include's; else it is
        expected now. A task the start did not see is put in a step by its role or file, else the running step. item:
        the label of the loop item its include brought it with. ends: a play's own end_host or end_play."""
        self._close(now)
        stage, step = self.plan.get(key, (None, None))
        if stage is None:
            stage = self.play_stage.get(play, self._running_stage())
            if stage is None:
                return None
            step = self._step(stage, self.play_path.get(play, ""), role, file)
            if step is None:
                step = self._running_step(stage)
            if not counted:
                self._expect(stage, step, 1)
        part = None
        if step is not None and item is not None and self.stages[stage].steps[step].each:
            part = self._part(stage, step, str(item))
        self._move(stage, step, part)
        task = Task(key, name, stage, step, now, hidden=hidden, path=path, number=len(self.order), part=part, ends=ends)
        # A task that runs again, a handler or an include's task in a loop, is a task more.
        self.tasks[key] = task
        self.order.append(task)
        self.stages[stage].tasks.append(task)
        if step is not None:
            self.stages[stage].steps[step].tasks.append(task)
        if part is not None:
            part.tasks.append(task)
        self.current = task
        return task

    def include(self, key, count, item=None):
        """An include's tasks are coming: counted at the start once, when its file is literal; counted now after
        that, as each loop item or host includes it again. item: the loop item's label, which is a step of its own in
        a step whose items are steps."""
        task = self.tasks.get(key)
        into = self.include_into.get(key) or ((task.stage, task.step) if task is not None else None)
        if into is None:
            return
        if self.reserved.pop(key, None) is None:
            self._expect(*into, count)
        stage, step = into
        if item is not None and step is not None and self.stages[stage].steps[step].each:
            self._part(stage, step, str(item), count)

    def result(self, key, host, outcome, now):
        task = self.tasks.get(key)
        if task is not None:
            task.result(host, outcome, now)
            if outcome in ("failed", "unreachable") and self.failure is None:
                self.failure = (self.where(task), now - self.started)
        reserved = self.reserved.get(key) if outcome == "skipped" else None
        if reserved is not None and key in self.include_into:
            # A skipped include brings nothing: what it was counted with goes.
            del self.reserved[key]
            self._expect(*self.include_into[key], -reserved)

    def item(self, key):
        task = self.tasks.get(key)
        if task is not None:
            task.items += 1

    def tried(self, key, now, attempt, total, delay):
        """A retried task: its next try, how many there are, and how long each waits."""
        task = self.tasks.get(key)
        if task is not None:
            task.retry = (attempt, total, delay, task.start)

    def absent(self, key):
        """The stage's absent task ran: this setup has no such stage."""
        task = self.tasks.get(key)
        if task is not None and self.stages[task.stage].absent is None:
            self.stages[task.stage].absent = ""

    def note(self, task, first, lines=()):
        """A report's first line, for the summary; a stage's absent task's is its reason, and the declared report's
        lines are kept whole."""
        if task is None:
            return
        stage = self.stages[task.stage]
        if stage.absent == "":
            stage.absent = first_sentence(first)
            return
        if stage.declared.report and task.name == stage.declared.report and lines:
            stage.whole = list(lines)
            return
        self.notes.append((task.stage, first))
        stage.said = len(self.notes) - 1

    def end(self, now, code=0):
        self._close(now)
        for stage in self.stages:
            stage.waiting = False
            if stage.tasks:
                stage.ended = True
            for row in [*stage.steps, *stage.rows()]:
                if row.tasks:
                    row.ended = True
        self.finished = now
        self.code = code

    def find(self, key):
        return self.tasks.get(key)

    def _move(self, stage, step=None, part=None):
        """The run moves to a stage, a step of it and an item of that step: whatever ran before ends, but a stage a
        play of which is still to come waits for it."""
        if stage is None:
            return
        for index, other in enumerate(self.stages):
            if index != stage and other.tasks:
                if self._plays_to_come(index):
                    other.waiting = True
                else:
                    other.ended = True
                for row in [*other.steps, *other.rows()]:
                    row.ended = row.ended or bool(row.tasks)
        if step is not None:
            current = self.stages[stage].steps[step]
            for row in [*self.stages[stage].steps, *self.stages[stage].rows()]:
                if row is not current and row is not part and row.tasks:
                    row.ended = True
            current.ended = False
            if part is not None:
                part.ended = False
        self.stages[stage].ended = False
        self.stages[stage].waiting = False

    def _plays_to_come(self, stage):
        return any(at == stage and key not in self.started_plays for key, at in self.play_stage.items())

    def _running_stage(self):
        return self.current.stage if self.current is not None else None

    def _running_step(self, stage):
        if self.current is not None and self.current.stage == stage:
            return self.current.step
        return 0 if self.stages[stage].steps else None

    # --- reading the model ----------------------------------------------------------------------------------
    def flat(self):
        """A run of one stage is drawn as its steps."""
        return len(self.stages) == 1

    def over(self):
        return self.finished is not None

    def row_of(self, task):
        """The row a task is drawn in: its include's item, or its step; None for a stage's own task."""
        if task.part is not None:
            return task.part
        return self.stages[task.stage].steps[task.step] if task.step is not None else None

    def where(self, task):
        """A task's stage and step, as a failure says where it was."""
        stage = self.stages[task.stage]
        row = self.row_of(task)
        step = row.label if row is not None else ""
        if self.flat():
            return step or stage.label
        return f"{stage.label} · {step}" if step else stage.label

    def _changed_said(self, count):
        return f"{count} would change" if self.check else f"{count} changed"

    def _running_index(self):
        for index, stage in enumerate(self.stages):
            if stage.state(self.over()) == "running":
                return index
        return None

    # --- the frame ------------------------------------------------------------------------------------------
    def frame(self, now, cols, rows, glyph, color=True):
        """The region's lines for a width and a height, rendered: the header, the stages and the running one's
        steps and last tasks, and a footer. Folded to fit: the steps to come on one line, then fewer tasks."""
        for fold, window in ((False, self.recipe.window), (True, self.recipe.window), (True, 3), (True, 1)):
            lines = self._frame(now, cols, glyph, fold, window)
            if len(lines) <= rows:
                break
        return [screen.render(screen.cut(line, cols), color) for line in lines]

    def _frame(self, now, cols, glyph, fold, window):
        narrow = cols < NARROW
        out = [self._header(now), []]
        flat = self.flat()
        for stage in self.stages:
            state = stage.state(self.over())
            if not flat or state in ("absent", "none"):
                out.append(self._row(stage, 1, state, glyph, now, cols, narrow, stage_row=True))
            if state in ("absent", "none"):
                continue
            if flat or state == "running" or (state == "failed" and self.over()):
                indent = 1 if flat else 5
                todo = [step for step in stage.rows() if step.state(self.over()) == "todo"]
                for step in stage.rows():
                    step_state = step.state(self.over())
                    if fold and step_state == "none":
                        continue
                    if fold and step_state == "todo":
                        if step is todo[0]:
                            labels = " · ".join(s.label for s in todo)
                            out.append([(" " * indent, ""), ("○", "d"), (" " + labels, "d")])
                        continue
                    out.append(self._row(step, indent, step_state, glyph, now, cols, narrow))
                    if step_state in ("running", "failed"):
                        out += self._window(step, now, glyph, cols, indent + 4, window)
        out.append([])
        out.append(self._footer(now, glyph))
        return out

    def _header(self, now, done=False):
        line = [("homeserver", "b"), ("  " + self.title, "")]
        said = [self.setup] if self.setup else []
        if self.check:
            said.append("dry run, nothing is changed")
        if not done:
            said.append("started " + time.strftime("%H:%M", self.started_at))
        if said:
            line.append((" · " + " · ".join(said), "d"))
        return line

    def _row(self, row, indent, state, glyph, now, cols, narrow, stage_row=False):
        mark, style = (glyph, "c") if state == "running" else ROW_GLYPHS[state]
        quiet = state in ("todo", "unrun", "absent", "none")
        line = [
            (" " * indent, ""),
            (mark, style),
            (" " + row.label.ljust(LABEL), "d" if quiet else "b" if stage_row else ""),
        ]
        if state == "absent":
            return [*line, (row.absent or "not in this setup", "d")]
        if state == "none":
            return [*line, (row.reason or "not in this run", "d")]
        if state in ("todo", "unrun"):
            said = " · ".join(step.label for step in row.rows()) if stage_row else screen.plural(row.expected, "task")
            return [*line, (said + (", not reached" if state == "unrun" else ""), "d")]
        cells = (12 if stage_row else 8) if narrow else (20 if stage_row else 12)
        full = min(cells, round(row.fraction() * cells))
        line += [("█" * full, "c" if state == "running" else "g"), ("░" * (cells - full), "d")]
        line.append(("  " + f"{row.reached}/{max(row.expected, row.reached)}".rjust(7), ""))
        if not narrow:
            changed = row.changed()
            line += [("   ", ""), (self._changed_said(changed), "y") if changed else ("—", "d")]
        seconds = row.seconds(now)
        tail = [(screen.clock(seconds), "c")] if state == "running" else [(screen.duration(seconds), "")]
        return screen.right(line, tail, cols)

    def _window(self, row, now, glyph, cols, indent, size):
        out = []
        for task in [t for t in row.tasks if not t.hidden][-size:]:
            running = task.running()
            mark = (glyph, "c") if running else GLYPHS.get(task.outcome, GLYPHS["ok"])
            line = [
                (" " * indent, ""),
                mark,
                (" " + task.name, "d" if task.outcome == "skipped" and not running else ""),
            ]
            tail = []
            if task.items:
                tail.append((f"{screen.plural(task.items, 'item')}  ", "d"))
            if running:
                if task.retry:
                    attempt, total, delay, _since = task.retry
                    tail.append((f"try {attempt} of {total} · up to {screen.clock(total * delay)}  ", "d"))
                tail.append((screen.clock(task.seconds(now)), "c"))
            elif task.outcome == "skipped":
                tail.append(("skipped", "d"))
            else:
                tail.append((screen.duration(task.seconds(now)), "d" if task.outcome in ("ignored", "rescued") else ""))
            out.append(screen.right(screen.cut(line, cols - screen.width(tail) - 2), tail, cols))
        return out

    def _footer(self, now, glyph):
        line = [(glyph + " ", "c"), (screen.clock(now - self.started), "")]
        said = []
        if self.flat():
            steps = self.stages[0].rows() if self.stages else []
            at = next((i for i, s in enumerate(steps) if s.state(self.over()) == "running"), None)
            if steps:
                said.append(f"step {(at or 0) + 1} of {len(steps)}")
        else:
            at = self._running_index()
            if self.stages:
                said.append(f"stage {(at or 0) + 1} of {len(self.stages)}")
            if at is not None:
                stage = self.stages[at]
                said.append(f"{stage.label} {int(100 * stage.fraction())}%")
        if self.log:
            said.append(self.log)
        return [*line, (" · " + " · ".join(said), "d")] if said else line

    def title_text(self, now):
        """The terminal's title: the run, and where it is."""
        if self.finished is not None:
            return f"{self.title} · {'failed' if self.code else 'done'}"
        if self.flat() and self.stages:
            return f"{self.title} · {int(100 * self.stages[0].fraction())}%"
        at = self._running_index()
        if at is None:
            return self.title
        stage = self.stages[at]
        return f"{self.title} · {stage.label} {int(100 * stage.fraction())}%"

    # --- off a terminal ---------------------------------------------------------------------------------------
    def ended_lines(self, now, cols=100, said=None):
        """A line for each step that has ended since the last call, and for a stage this setup lacks."""
        said = self._said if said is None else said
        out = []
        for index, stage in enumerate(self.stages):
            if stage.absent is not None or (stage.left and not self._plays_to_come(index)):
                # Said once its reason is known, and its steps never: what its plays read first is no step of it.
                reason = stage.absent if stage.absent is not None else stage.reason or "not in this run"
                if reason and stage not in said:
                    said.add(stage)
                    line = [(DASH + " ", "d"), (stage.label.ljust(30), ""), (reason, "d")]
                    out.append(screen.render(line, False))
                continue
            for step in stage.rows():
                if step in said or not step.tasks or not (step.ended or self.over()) or stage.left:
                    continue
                said.add(step)
                label = step.label if self.flat() else f"{stage.label} · {step.label}"
                state = step.state(True)
                mark = {"failed": "✗", "skipped": DASH}.get(state, "✓")
                counts = [screen.plural(step.reached, "task")]
                if step.changed():
                    counts.append(self._changed_said(step.changed()))
                if step.failed():
                    counts.append(f"{step.failed()} failed")
                line = [(mark + " ", ""), (label.ljust(30), ""), (" · ".join(counts), "")]
                out.append(screen.render(screen.right(line, [(screen.duration(step.seconds(now)), "")], cols), False))
        return out

    # --- the summary ------------------------------------------------------------------------------------------
    def summary(self, now, cols, next_lines=(), color=True):
        """The run's end, as (coloured, plain) lines: how it ended, each stage or step, the reports' first lines,
        what changed, the slowest tasks, NEXT STEPS (next_lines, each a screen.Line) and the exit line, each part
        one blank line from the next."""
        flat = self.flat()
        as_steps = flat and self.stages[0].absent is None and not self.stages[0].left
        rows = self.stages[0].rows() if as_steps else self.stages
        changed_head = "WOULD" if self.check else "CHANGED"
        table = [
            [
                (("STEPS" if as_steps else "STAGES").ljust(18), "h"),
                ("TIME".rjust(7) + "TASKS".rjust(7) + changed_head.rjust(9), "h"),
            ]
        ]
        for row in rows:
            table += self._summary_row(row, now, cols, flat)
        parts = [[self._summary_head(now)], table]
        reports = []
        for index, stage in enumerate(self.stages):
            # Each report's first line, cut short: the report itself is above, whole. The declared report, whole: a
            # line each, wrapped.
            said = [text.one_line(note, NOTE) for at, note in self.notes if at == index]
            paragraphs = [" · ".join(said)] if said else []
            paragraphs += [line.strip() for line in stage.whole or [] if line.strip()]
            pieces = [piece for paragraph in paragraphs for piece in screen.fill(paragraph, max(20, cols - 16))]
            for number, piece in enumerate(pieces):
                head = stage.label.upper().ljust(16) if number == 0 else " " * 16
                reports.append([(head, "h"), (piece, "")])
        parts.append(reports)
        changed = []
        for task in self.order:
            if task.outcome == "changed" and not REPORT_NAME.match(task.name) and task.name not in changed:
                changed.append(task.name)
        named = [*changed[:5], f"and {len(changed) - 5} more"] if len(changed) > 6 else changed
        parts.append(self._labelled("WOULD CHANGE" if self.check else "CHANGED", " · ".join(named), "", cols))
        slow = sorted((t for t in self.order if t.seconds(now) >= 1), key=lambda t: -t.seconds(now))[:5]
        said = " · ".join(f"{t.name} {screen.duration(t.seconds(now))}" for t in slow)
        parts.append(self._labelled("SLOWEST", said, "d", cols))
        out = []
        for part in parts:
            if part:
                out += [
                    (screen.render(screen.cut(line, cols), color), screen.render(screen.cut(line, cols), False))
                    for line in part
                ]
                out.append(("", ""))
        if next_lines:
            out += [(line.text(color), line.text(False)) for line in next_lines]
            out.append(("", ""))
        tasks = len(self.order)
        failed = sum(1 for t in self.order if t.outcome in ("failed", "unreachable"))
        changed_count = sum(1 for t in self.order if t.outcome == "changed")
        total = f"{screen.plural(tasks, 'task')} · {self._changed_said(changed_count)} · {failed} failed"
        tail = [
            [(total + (f" · log {self.log}" if self.log else ""), "d")],
            [(f"exit {self.code}", "r" if self.code else "g"), (" · for the whole picture: just status", "d")],
        ]
        out += [(screen.render(line, color), screen.render(line, False)) for line in tail]
        return out

    @staticmethod
    def _labelled(label, said, style, cols):
        """said under a heading of 14 columns, wrapped; nothing when there is nothing to say."""
        if not said:
            return []
        lines = screen.fill(said, max(20, cols - 14))
        return [[(label.ljust(14), "h"), (lines[0], style)], *[[(" " * 14 + line, style)] for line in lines[1:]]]

    def _summary_head(self, now):
        line = [("homeserver", "b"), ("  " + self.title, "")]
        said = [self.setup] if self.setup else []
        if self.check:
            said.append("dry run")
        line.append((" · " + " · ".join([*said, ""]), "d"))
        took = screen.long_duration((self.finished or now) - self.started)
        if self.code:
            where = self.failure[0] if self.failure else ""
            seconds = self.failure[1] if self.failure else (self.finished or now) - self.started
            line.append((f"failed after {screen.long_duration(seconds)}" + (f" at {where}" if where else ""), "r"))
        else:
            line.append((f"done in {took}", "g"))
        line.append((" · " + time.strftime("%H:%M", time.localtime()), "d"))
        return line

    def _summary_row(self, row, now, cols, flat):
        state = row.state(True)
        mark, style = ROW_GLYPHS["todo"] if state == "unrun" else ROW_GLYPHS[state]
        quiet = state in ("unrun", "absent", "none")
        line = [(" ", ""), (mark, style), (" " + row.label.ljust(LABEL), "d" if quiet else "")]
        if state == "unrun":
            return [[*line, ("not reached", "d")]]
        if state == "none":
            return [[*line, (row.reason or "not in this run", "d")]]
        if state == "absent":
            return [[*line, (row.absent or "not in this setup", "d")]]
        changed = row.changed()
        line += [(screen.duration(row.seconds(now)).rjust(7) + str(row.reached).rjust(7), "")]
        line.append((str(changed).rjust(9) if changed else "—".rjust(9), "y" if changed else "d"))
        if flat or not isinstance(row, Stage):
            return [line]
        # A stage names its steps worth naming: those that took 2 s or more, or changed something.
        info = []
        for step in row.rows():
            if step.tasks and (step.seconds(now) >= 2 or step.changed()):
                said = f"{step.label} {screen.duration(step.seconds(now))}"
                if step.changed():
                    said += ", " + self._changed_said(step.changed())
                info.append(said)
        if not info:
            return [line]
        parts = screen.fill(" · ".join(info), max(20, cols - 45))
        out = [[*line, ("   " + parts[0], "d")]]
        out += [[(" " * 44 + part, "d")] for part in parts[1:]]
        return out


def truthy(value):
    """An option's value as Ansible reads a boolean: true, yes, on or 1, in any case."""
    return str(value).strip().lower() in ("true", "yes", "on", "1", "y", "t") if value is not None else False


def _neighbours(steps):
    """Each task's step, a task in none taking its neighbour's: the next task's in its play, else the one before."""
    out = list(steps)
    for index, step in enumerate(out):
        if step is not None:
            continue
        after = next((s for s in out[index + 1 :] if s is not None), None)
        before = next((s for s in reversed(out[:index]) if s is not None), None)
        out[index] = after if after is not None else before
    return out


class FilterModule:
    """No filters: loaded by path. Here so the filter loader takes the file as a plugin."""

    def filters(self):
        return {}
