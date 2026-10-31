"""The default stdout callback, with NEXT STEPS after the play recap: what the roles recorded as left to do.

Everything else is the default callback's, its options included. A subclass without the default's documentation
fragments has no options: every hook then fails on its first get_option, Ansible only warns, and every task's result
disappears from the screen and the log in a run that still exits 0. tests/unit/test_callback.py holds this callback's
output and log to the default's, byte for byte, NEXT STEPS aside.

ansible.cfg names it, with the directory it is in, so a playbook anywhere finds it. The idempotence tests run with
tests/integration/ansible.cfg, which names no callback, and read the records from the default callback's custom
stats instead.
"""

DOCUMENTATION = r"""
name: homeserver
type: stdout
short_description: the default output, with NEXT STEPS after the play recap
description:
  - The default callback, then, after the play recap, what the roles recorded with set_stats as left to do (NEXT
    STEPS), under homeserver_next.
  - A record is homeserver_next.<subject>.<matter>, a mapping with text and, optionally, command; an empty string
    clears one. A later record of a subject and matter replaces an earlier one, and the records are printed in the
    order they were first made, each once, whole, so a command is copied from one line.
extends_documentation_fragment:
  - default_callback
  - result_format_callback
requirements:
  - set as stdout_callback in ansible.cfg
"""

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


def next_steps_lines(items):
    """NEXT STEPS as lines: a heading, then each record as just status lists a thing to do, never wrapped: the
    terminal wraps a long line, and a command stays whole to copy."""
    head = screen.Line().add("NEXT STEPS", "h").add("  ").add(f"{len(items)} to do", "y")
    things = [{"level": "todo", "subject": s, "text": t, "command": c} for s, t, c in items]
    return [head, *screen.attention_items(things)]


class CallbackModule(DefaultCallback):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "homeserver"

    def v2_playbook_on_stats(self, stats):
        super().v2_playbook_on_stats(stats)
        items = next_steps(stats.custom.get("_run", {}))
        if items:
            self._display.display("")
            for line in next_steps_lines(items):
                # In colour on the screen, as just status's, and plain in the log.
                if ANSIBLE_COLOR:
                    self._display.display(line.text(True), screen_only=True)
                    self._display.display(line.text(False), log_only=True)
                else:
                    self._display.display(line.text(False))
