"""The stdout callback: every playbook run drawn live, or the default callback's output with NEXT STEPS after it.

At verbosity 0 every run is drawn (filter_plugins/live_ansible.py): live at a terminal, a line per step otherwise,
the stream going to the run's log alone and a summary replacing PLAY RECAP on the screen, with NEXT STEPS, what the
roles recorded as left to do. Any -v, or ANSIBLE_STDOUT_CALLBACK=default, is the stream as before; at -v this callback
prints NEXT STEPS after the recap itself.

Every hook calls the default callback's own first, which writes the stream to the log, and only then the engine,
inside a try: Ansible only warns when a callback raises, and the event would be lost from the screen and the log.
On any error the engine lets go (the region erased, one line on stderr) and the rest of the run prints classically.
The default's documentation fragments are carried for the same reason: a subclass without them has no options, every
hook fails on its first get_option, and every task's result disappears in a run that still exits 0.
tests/unit/test_callback.py holds this callback's output and log, at -v, to the default's, byte for byte, NEXT STEPS
aside.

ansible.cfg names it, with the directory it is in, so a playbook anywhere finds it. The tests pin the default callback
(tests/conftest.py); the idempotence tests read the records from its custom stats instead.
"""

DOCUMENTATION = r"""
name: homeserver
type: stdout
short_description: every run drawn live, or the default output with NEXT STEPS after the play recap
description:
  - At verbosity 0, the run drawn live at a terminal, or a line per step otherwise, every task's output in the run's
    log alone, and a summary after it. At any -v, the default callback.
  - Either way, after the play recap, what the roles recorded with set_stats as left to do (NEXT STEPS), under
    homeserver_next.
  - A record is homeserver_next.<subject>.<matter>, a mapping with text and, optionally, command; an empty string
    clears one. A later record of a subject and matter replaces an earlier one, and the records are printed in the
    order they were first made, each once, whole, so a command is copied from one line.
extends_documentation_fragment:
  - default_callback
  - result_format_callback
requirements:
  - set as stdout_callback in ansible.cfg
"""

import contextlib
import importlib.util
import sys
from pathlib import Path

from ansible.plugins.callback.default import CallbackModule as DefaultCallback
from ansible.utils.color import ANSIBLE_COLOR


def _sibling(stem):
    """filter_plugins/<stem>.py, loaded by path once as homeserver_<stem>: a callback cannot ask for a filter
    module by name."""
    name = f"homeserver_{stem}"
    if name not in sys.modules:
        path = Path(__file__).resolve().parents[1] / "filter_plugins" / f"{stem}.py"
        spec = importlib.util.spec_from_file_location(name, path)
        sys.modules[name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules[name])
    return sys.modules[name]


# NEXT STEPS is drawn as just status draws what needs attention, its words made safe to print.
screen = _sibling("screen")
text = _sibling("text")
live = _sibling("live_ansible")

# The set_stats name every record goes under.
NEXT_STAT = "homeserver_next"


def next_steps(run_stats):
    """What is left to do, from the run's custom stats: (subject, text, command) for each record that holds text, in
    the order the records were first made, each once."""
    out = []
    subjects = run_stats.get(NEXT_STAT)
    for subject, matters in (subjects if isinstance(subjects, dict) else {}).items():
        for record in (matters if isinstance(matters, dict) else {}).values():
            if not isinstance(record, dict) or not str(record.get("text") or "").strip():
                continue
            # One line each, without a sequence a terminal would act on: a record can quote what a machine answered.
            item = (
                text.one_line(subject, 40),
                text.one_line(record["text"], 1000),
                text.one_line(record.get("command") or "", 1000),
            )
            if item not in out:
                out.append(item)
    return out


def next_steps_lines(items, cols=None):
    """NEXT STEPS as lines: a heading, then each record as just status lists a thing to do, its text wrapped to cols
    (never, without them: the terminal wraps a long line) and its command whole, to copy from one line."""
    head = screen.Line().add("NEXT STEPS", "h").add("  ").add(f"{len(items)} to do", "y")
    things = [{"level": "todo", "subject": s, "text": t, "command": c} for s, t, c in items]
    return [head, *screen.attention_items(things, cols, whole_commands=True)]


class CallbackModule(DefaultCallback):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "homeserver"

    def __init__(self):
        super().__init__()
        # The live run's engine, when the run is drawn: decided when the playbook starts, once the verbosity is known.
        self._live = None

    def _engine(self, event, *args):
        """An event for the engine, after the default callback's own hook: on any error, it lets go."""
        if self._live is None:
            return
        try:
            getattr(self._live, event)(*args)
        except Exception as error:  # noqa: BLE001 - the drawing never costs the run an event
            engine, self._live = self._live, None
            with contextlib.suppress(Exception):
                engine.release(error)
            return
        # A frame that could not be drawn let go of the region by itself.
        if self._live.released:
            self._live = None

    def v2_playbook_on_start(self, playbook):
        super().v2_playbook_on_start(playbook)
        try:
            self._live = live.from_callback(self, playbook)
        except Exception as error:  # noqa: BLE001 - a run that cannot be drawn is the stream
            self._display.display(f"[homeserver] the run is not drawn ({type(error).__name__}: {error})", stderr=True)

    def v2_playbook_on_play_start(self, play):
        super().v2_playbook_on_play_start(play)
        self._engine("play_start", play)

    def v2_playbook_on_task_start(self, task, is_conditional):
        super().v2_playbook_on_task_start(task, is_conditional)
        self._engine("task_start", task)

    def v2_playbook_on_handler_task_start(self, task):
        super().v2_playbook_on_handler_task_start(task)
        self._engine("task_start", task)

    def v2_playbook_on_include(self, included_file):
        super().v2_playbook_on_include(included_file)
        self._engine("include", included_file)

    def v2_runner_on_ok(self, result):
        # Read before the default's hook, which cleans a debug's result in place, its changed with it.
        changed = bool(result.result.get("changed"))
        super().v2_runner_on_ok(result)
        self._engine("ok", result, changed)

    def v2_runner_item_on_ok(self, result):
        super().v2_runner_item_on_ok(result)
        self._engine("item_ok", result)

    def v2_runner_on_skipped(self, result):
        super().v2_runner_on_skipped(result)
        self._engine("skipped", result)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        super().v2_runner_on_failed(result, ignore_errors)
        self._engine("failed", result, ignore_errors)

    def v2_runner_item_on_failed(self, result):
        super().v2_runner_item_on_failed(result)
        self._engine("item_failed", result)

    def v2_runner_on_unreachable(self, result):
        super().v2_runner_on_unreachable(result)
        self._engine("unreachable", result)

    def v2_runner_retry(self, result):
        super().v2_runner_retry(result)
        self._engine("retry", result)

    def v2_on_file_diff(self, result):
        # A dry run is read for its diffs: drawn, they go above the region, to the screen and the log. A real run's
        # go to the log alone, and the summary names what changed.
        if self._live is not None and not self._live.released and self._live.run.check:
            with self._live.say():
                super().v2_on_file_diff(result)
            return
        super().v2_on_file_diff(result)

    def v2_playbook_on_stats(self, stats):
        super().v2_playbook_on_stats(stats)
        items = next_steps(stats.custom.get("_run", {}))
        if self._live is not None:
            self._engine("stats", stats, lambda cols: next_steps_lines(items, cols) if items else [])
            if self._live is not None:
                return
        lines = next_steps_lines(items) if items else []
        if lines:
            self._display.display("")
            for line in lines:
                # In colour on the screen, as just status's, and plain in the log.
                if ANSIBLE_COLOR:
                    self._display.display(line.text(True), screen_only=True)
                    self._display.display(line.text(False), log_only=True)
                else:
                    self._display.display(line.text(False))
