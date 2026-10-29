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

import re

from ansible.plugins.callback.default import CallbackModule as DefaultCallback

# The set_stats name every record goes under.
NEXT_STAT = "homeserver_next"
# The colours of the list, as just status draws its list of what needs attention: the heading, then each command.
HEADING_COLOR = "bright blue"
COMMAND_COLOR = "cyan"
# A record can quote what a machine answered, which a terminal must not act on: each escape sequence whole (a string
# sequence up to its terminator, a CSI with its parameters, any other ESC sequence, the 8-bit forms), then every
# control character left. An OSC 52 in a record would otherwise write the clipboard at the end of every run.
SEQUENCES = re.compile(
    r"(?:\x1b[\]PX^_]|[\x9d\x90\x98\x9e\x9f])[^\x07\x1b\x9c\n]*(?:\x07|\x1b\\|\x9c)?"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?"
    r"|\x1b[ -/]*[0-~]?"
)
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def one_line(said):
    """said as one line of plain text: no escape sequence, every control character and run of spaces one space."""
    return " ".join(CONTROL.sub(" ", SEQUENCES.sub("", str(said))).split())


def next_steps(run_stats):
    """What is left to do, from the run's custom stats: (subject, text, command) for each record that holds text, in
    the order the records were first made, each once."""
    out = []
    subjects = run_stats.get(NEXT_STAT)
    for subject, matters in (subjects if isinstance(subjects, dict) else {}).items():
        for record in (matters if isinstance(matters, dict) else {}).values():
            if not isinstance(record, dict) or not str(record.get("text") or "").strip():
                continue
            item = (one_line(subject), one_line(record["text"]), one_line(record.get("command") or ""))
            if item not in out:
                out.append(item)
    return out


def next_steps_lines(items):
    """NEXT STEPS as lines of (text, colour): a heading, then each record's subject and text and, under it, its
    command. Never wrapped: the terminal wraps a long line, and a command stays whole to copy."""
    lines = [(f"NEXT STEPS  {len(items)} to do", HEADING_COLOR)]
    for subject, text, command in items:
        lines.append((f" ● {subject:<12}{text}", None))
        if command:
            lines.append((f"{' ' * 15}→ {command}", COMMAND_COLOR))
    return lines


class CallbackModule(DefaultCallback):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "homeserver"

    def v2_playbook_on_stats(self, stats):
        super().v2_playbook_on_stats(stats)
        items = next_steps(stats.custom.get("_run", {}))
        if items:
            self._display.display("")
            for line, color in next_steps_lines(items):
                self._display.display(line, color=color)
