"""Text a machine printed, made safe to print here: no escape sequence, no control character, and one line when asked.

A failure's message quotes what a command printed on the Proxmox host, the NAS or a node, and a report quotes what
they answered. ansible-core 2.21 writes a failure's lines to the terminal and the run log raw, so an escape sequence
quoted in one would retitle the terminal, recolour it, or write the clipboard (OSC 52); removing only the control
characters would leave a sequence's payload behind as text. NEXT STEPS (callback_plugins/homeserver.py) prints each
record the roles made through these, since a record can quote what a machine answered, and every live run
(filter_plugins/live_ansible.py) its reports and failures, its summary keeping each report to one line.

Ported from claude-on-proxmox's filter_plugins/text.py (commit 55e53c2), its banner left out; the two projects stay
independent, so a fix there is compared by hand. Pure, nothing read or written. Loaded by path as homeserver_text; it
has no filters of its own.
"""

from __future__ import annotations

import re
import unicodedata

# Each escape sequence whole: a string sequence (OSC, DCS, SOS, PM, APC) up to its terminator, or the end of its line
# when it has none; a CSI with its parameters and final byte; any other ESC sequence; and the 8-bit forms.
_SEQUENCES = re.compile(
    r"(?:\x1b[\]PX^_]|[\x9d\x90\x98\x9e\x9f])[^\x07\x1b\x9c\n]*(?:\x07|\x1b\\|\x9c)?"
    r"|(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?"
    r"|\x1b[ -/]*[0-~]?"
)
# What is left that a terminal acts on: every C0 and C1 control but tab and newline.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Unicode's format, line and paragraph separators - the bidi overrides and isolates, zero-width characters - which
# can make a line read in an order other than its own.
_FORMAT_CATEGORIES = frozenset({"Cf", "Zl", "Zp"})
_SPACES = re.compile(r" {2,}")


def plain_text(text):
    """text without escape sequences or control characters; tab and newline stay."""
    if text is None:
        return ""
    return _CONTROL.sub("", _SEQUENCES.sub("", str(text)))


def visible_text(text):
    """text through plain_text, each Unicode format or separator character a space; its lines, tabs and indents
    stay, so a report printed line by line reads in the order it was written."""
    return "".join(" " if unicodedata.category(ch) in _FORMAT_CATEGORIES else ch for ch in plain_text(text))


def one_line(text, limit=120):
    """text as one line of plain text, at most limit characters, ending in "…" when cut: each newline, carriage
    return and tab a space, and runs of spaces one. None is ""."""
    if text is None:
        return ""
    cleaned = "".join(" " if unicodedata.category(ch) == "Cc" else ch for ch in visible_text(text))
    cleaned = _SPACES.sub(" ", cleaned).strip()
    limit = max(int(limit), 1)
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1].rstrip() + "…"


class FilterModule:
    """No filters: loaded by path. Here so the filter loader takes the file as a plugin."""

    def filters(self):
        return {}
