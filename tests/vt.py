"""Enough of a VT100 to read back what a live region drew at a terminal: just status's live lines
(tests/integration/test_status.py), and every live output after them. Ported from claude-on-proxmox's tests/unit/vt.py.

Text wraps at the right edge with the pending-wrap rule terminals use; a line feed at the bottom row scrolls the
screen into the history (the scrollback); cursor up (CSI n A), carriage return, erase to the end of the line
(CSI K, CSI 2K) and erase below (CSI J) move and clear; colours are taken and ignored, and synchronized output
(CSI ?2026 h/l) marks what is drawn as a frame, where a line that wraps is counted (`frame_wraps`): a live region
must never wrap. A title (OSC 0, ended by BEL or ST) is recorded in `titles`, a bell on its own is counted in
`bells`; an ESC [ before a control is the control (Ansible's pause writes them). Any other sequence is recorded, so a
test can say the callback wrote none. A resize keeps lines as they are, as xterm does (no reflow), and a shorter
window keeps its bottom rows, the rest going to the history. Bytes arrive as a pseudo-terminal's reads cut them: a
character or an escape sequence cut at the end of one feed is kept until the next completes it, as a terminal does.
"""

from __future__ import annotations

import re

CSI = re.compile(rb"\x1b\[([?0-9;]*)([A-Za-z])")
OSC = re.compile(rb"\x1b\]([0-9]*);([^\x07\x1b]*)(?:\x07|\x1b\\)")
# What a feed may end inside of: an escape sequence not yet ended, an OSC by BEL or by ST (ESC \).
OPEN = re.compile(rb"\x1b(\[[?0-9;]*|\][^\x07\x1b]*\x1b?)?\Z")


class VT:
    def __init__(self, cols: int, rows: int):
        self.cols, self.rows = cols, rows
        self.lines = [""] * rows
        self.history: list[str] = []
        self.r = self.c = 0
        self.wrap = False
        self.ups: list[int] = []
        self.unknown: set[str] = set()
        self.in_frame = False
        self.frame_wraps = 0
        self.titles: list[str] = []
        self.bells = 0
        self.pending = b""

    def resize(self, cols: int, rows: int) -> None:
        self.cols = cols
        while len(self.lines) > rows:
            if self.r > 0:
                self.history.append(self.lines.pop(0))
                self.r -= 1
            else:
                self.lines.pop()
        self.lines += [""] * (rows - len(self.lines))
        self.rows = rows

    def _line_feed(self) -> None:
        if self.r == self.rows - 1:
            self.history.append(self.lines.pop(0))
            self.lines.append("")
        else:
            self.r += 1

    def _put(self, ch: str) -> None:
        if self.wrap:
            self.c, self.wrap = 0, False
            self.frame_wraps += self.in_frame
            self._line_feed()
        line = self.lines[self.r].ljust(self.c)
        self.lines[self.r] = line[: self.c] + ch + line[self.c + 1 :]
        if self.c == self.cols - 1:
            self.wrap = True
        else:
            self.c += 1

    @staticmethod
    def _complete(data: bytes) -> int:
        """How much of data ends whole: not inside an escape sequence or a UTF-8 character."""
        open_seq = OPEN.search(data)
        if open_seq:
            start = open_seq.start()
            # An ESC [ just before it is pause's, before a control: it waits to be read with what follows.
            while start >= 2 and data[start - 2 : start] == b"\x1b[":
                start -= 2
            return start
        tail = len(data)
        while tail > 0 and (data[tail - 1] & 0xC0) == 0x80:
            tail -= 1
        if tail > 0 and data[tail - 1] >= 0xC0:
            lead = data[tail - 1]
            if len(data) - (tail - 1) < (2 if lead < 0xE0 else 3 if lead < 0xF0 else 4):
                return tail - 1
        return len(data)

    def feed(self, data: bytes) -> None:
        data, self.pending = self.pending + data, b""
        whole = self._complete(data)
        data, self.pending = data[:whole], data[whole:]
        i = 0
        while i < len(data):
            match = CSI.match(data, i)
            if match:
                arg, op = match.group(1).decode(), match.group(2).decode()
                if op == "A":
                    n = int(arg or 1)
                    self.ups.append(n)
                    self.r, self.wrap = max(0, self.r - n), False
                elif op == "K" and arg in ("", "0"):
                    self.lines[self.r] = self.lines[self.r][: self.c]
                elif op == "K" and arg == "2":
                    self.lines[self.r] = ""
                elif op == "J" and arg in ("", "0"):
                    self.lines[self.r] = self.lines[self.r][: self.c]
                    for k in range(self.r + 1, self.rows):
                        self.lines[k] = ""
                elif op in "hl" and arg == "?2026":
                    self.in_frame = op == "h"
                elif op == "m":
                    pass
                else:
                    self.unknown.add(match.group().decode())
                i = match.end()
                continue
            if data[i : i + 2] == b"\x1b[" and i + 2 < len(data) and data[i + 2] < 0x20:
                # Ansible's pause writes ESC [ before its carriage return and its erase: a terminal runs a control
                # inside a sequence and drops a sequence another ESC starts over, so the two are what is left.
                i += 2
                continue
            match = OSC.match(data, i)
            if match:
                if match.group(1) == b"0":
                    self.titles.append(match.group(2).decode("utf-8", "replace"))
                else:
                    self.unknown.add(f"OSC {match.group(1).decode()}")
                i = match.end()
                continue
            byte = data[i : i + 1]
            if byte == b"\r":
                self.c, self.wrap = 0, False
                i += 1
            elif byte == b"\n":
                self.wrap = False
                self._line_feed()
                i += 1
            elif byte == b"\x07":
                self.bells += 1
                i += 1
            elif byte == b"\x1b":
                self.unknown.add("ESC")
                i += 1
            else:
                j = i + 1
                while j < len(data) and (data[j] & 0xC0) == 0x80:
                    j += 1
                self._put(data[i:j].decode("utf-8", "replace"))
                i = j

    def transcript(self) -> list[str]:
        """The scrollback and the screen, as lines, without the blank ones at the bottom."""
        out = self.history + self.lines
        while out and not out[-1].strip():
            out.pop()
        return out
