"""What every report and live output draws with: lines of styled text, measured, cut and rendered in one palette.

A line is a list of segments, (text, style): a style is one of PALETTE's keys, or "" for plain. Line builds one, with
columns that line up with or without colour. `just status`'s report (roles/status/filter_plugins/status.py) and its
live lines (callback_plugins/homeserver_status.py), and NEXT STEPS after every playbook run
(callback_plugins/homeserver.py), are built as lines and rendered here, so they cut, pad, colour and measure alike;
live_region.py beside this file puts a live output's lines on the screen.

A line is measured in characters, one each: a terminal set to draw East Asian ambiguous-width characters (█ ● ○ · …)
double-wide wraps a line drawn with them.

Pure, nothing read or written. Loaded by path as homeserver_screen, since a filter plugin is no module another can
import; it has no filters of its own.
"""

from __future__ import annotations

import textwrap

# The palette, as ANSI codes: bold, dim, a key's grey, headings, and the colours of done, to do, failed and the rest.
# A URL (u) is also a link a terminal can open (OSC 8).
PALETTE = {"b": "1", "d": "90", "k": "37", "h": "1;34", "g": "32", "y": "33", "r": "31", "c": "36", "u": "36"}
SPARK = "▁▂▃▄▅▆▇█"
# The column an attention item's text starts at: a dot, its subject in 12, a space.
ITEM_INDENT = 15


def width(line):
    return sum(len(text) for text, _ in line)


def cut(line, cols):
    """line cut to cols characters, ending in a dim "…" when cut: a live line that wrapped would break the count of
    lines drawn."""
    if width(line) <= cols:
        return list(line)
    out, used = [], 0
    for text, style in line:
        room = cols - 1 - used
        if room <= 0:
            break
        out.append((text[:room], style))
        used += len(text[:room])
    out.append(("…", "d"))
    return out


def render(line, color):
    """line as text: in the palette's colours, or plain; a style on spaces alone is left out, and so are trailing
    spaces."""
    out = []
    for text, style in line:
        if not (color and style and text.strip()):
            out.append(text)
        elif style == "u":
            out.append(f"\033]8;;{text}\033\\\033[{PALETTE[style]}m{text}\033[0m\033]8;;\033\\")
        else:
            out.append(f"\033[{PALETTE[style]}m{text}\033[0m")
    return "".join(out).rstrip()


class Line:
    """A line being built, in styled segments, padded on the text alone so that columns line up with or without
    colour."""

    def __init__(self):
        self.parts = []

    def add(self, text, style=""):
        self.parts.append((str(text), style))
        return self

    def cell(self, text, cells, style="", right=False):
        text = str(text)
        pad = " " * max(cells - len(text), 0)
        return self.add(pad).add(text, style) if right else self.add(text, style).add(pad)

    def to(self, column):
        return self.add(" " * max(column - len(self), 0))

    def __len__(self):
        return width(self.parts)

    def text(self, color):
        return render(self.parts, color)


def right(line, tail, cols):
    """line with tail at the right edge of cols, at least two spaces between them; line alone when they do not fit."""
    room = cols - width(line) - width(tail)
    return [*line, (" " * room, ""), *tail] if room >= 2 else list(line)


def duration(seconds):
    """A duration as a report says it: 0.6 s, 33 s, 3:58."""
    if seconds < 10:
        return f"{seconds:.1f} s"
    if seconds < 60:
        return f"{round(seconds)} s"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}:{rest:02d}"


def long_duration(seconds):
    """A duration as a sentence says it: 42 s, 3 min 58 s."""
    seconds = round(seconds)
    return f"{seconds} s" if seconds < 60 else f"{seconds // 60} min {seconds % 60} s"


def clock(seconds):
    """A running timer: 0:07, 12:30."""
    minutes, rest = divmod(int(max(0, seconds)), 60)
    return f"{minutes}:{rest:02d}"


def fill(text, cols):
    """text in lines of cols characters, never breaking a word, such as a path or a URL, in two."""
    return textwrap.wrap(str(text), max(1, cols), break_long_words=False, break_on_hyphens=False) or [""]


def and_list(names):
    """Names as a sentence lists them: a, b and c."""
    names = list(names)
    return " and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names)


def plural(count, word, words=None):
    return f"{count} {word if count == 1 else words or word + 's'}"


def bar(fraction, cells=12):
    """A bar of cells, filled for fraction."""
    full = min(cells, round(max(fraction, 0) * cells))
    return "█" * full + "░" * (cells - full)


def fullness(fraction, todo=0.8, problem=0.9):
    """The style for how full something is: cyan, yellow from todo, red from problem."""
    return "r" if fraction >= problem else "y" if fraction >= todo else "c"


def spark(values):
    """A sparkline of fractions from 0 to 1 as segments, a blank where there is no value, a bucket from 0.75 in
    yellow."""
    out = []
    for value in values or []:
        if value is None:
            out.append((" ", ""))
        else:
            level = min(len(SPARK) - 1, int(max(value, 0) * len(SPARK)))
            out.append((SPARK[level], "y" if value >= 0.75 else "c"))
    return out


def attention_items(items, cols=None, whole_commands=False):
    """Lines for what needs attention, each item a mapping with level (problem, unread or todo), subject, text and
    command: a dot, yellow for a thing to do and red for the rest, the subject, the text, and under it the command
    after an arrow. Wrapped to cols, or each whole on its line without them, so that a command copies from one line;
    with whole_commands, the text alone is wrapped."""
    lines = []
    for item in items:
        subject = str(item["subject"])
        first = Line().add(" ").add("●", "y" if item["level"] == "todo" else "r").add(" ").cell(subject, 12, "b")
        if len(subject) >= 12:
            first.add(" ")
        text = str(item["text"])
        for i, part in enumerate(fill(text, cols - ITEM_INDENT) if cols else [text]):
            lines.append((first if i == 0 else Line().add(" " * ITEM_INDENT)).add(part))
        command = str(item.get("command") or "")
        wrapped = fill(command, cols - ITEM_INDENT - 2) if cols and not whole_commands else [command]
        for i, part in enumerate(wrapped if command else []):
            lines.append(Line().add(" " * ITEM_INDENT).add("→ " if i == 0 else "  ", "d").add(part, "c"))
    return lines


class FilterModule:
    """No filters: loaded by path. Here so the filter loader takes the file as a plugin."""

    def filters(self):
        return {}
