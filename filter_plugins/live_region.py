"""A live region at the bottom of a terminal, for a stdout callback: drawn in place, with Display's output above it.

Every live output draws with it: `just status`'s live lines (callback_plugins/homeserver_status.py) and, from the
live engine on, every playbook run. Ported from claude-on-proxmox's filter_plugins/live_region.py (commit 64c413a),
where it was proven over its every live output; the two projects stay independent, so a fix there is compared by
hand. Loaded by path as homeserver_live_region, as a callback loads a filter; it has no filters of its own, since a
module in callback_plugins/ that is no callback is warned about on every run.

What it does, once started:

- Draws what its frame function returns at the bottom of the screen: one write per redraw, inside
  synchronized-output marks (CSI ?2026), the cursor moved up exactly the lines drawn, the region cut to the height,
  and ten times a second for a spinner. A frame's lines come already cut to the width, so none wraps. A screen
  under MIN_ROWS rows or MIN_COLS columns gets no region at all: a region as tall as the screen scrolls each redraw.
- Wraps Display.display: anything printed erases the region first and redraws it after, so it stays above for
  good. A route function, given the call's keyword arguments, may send it to the log only (log_only), which leaves
  the screen alone.
- Wraps Display.prompt_until and Display.do_var_prompt: a pause prints with display() and only then takes Display's
  lock to read the keys (ansible-core 2.21), and a vars_prompt reads with input(), so the region is erased before
  either and drawn again only once it returns, and the run has worked half a second more. The spinner takes the
  same lock, and draws nothing while a prompt is up.
- On a resize (SIGWINCH), which can wrap a frame worked out for the old size, pushes the screen into the
  scrollback and draws again from the top. The cursor is never hidden.
- Sets the terminal's title (OSC 0) when it changes, and rings the bell when asked.
- Turns the terminal's echo off while it draws, and back on for a prompt and at the end, when standard input is a
  terminal (just status reads /dev/null, and keeps its echo as it is): an Enter typed during the run would move the
  cursor under the region, and the next redraw would leave a copy of it behind. Never from the background, where
  changing it would stop the run (SIGTTOU).
- Erases the region at exit, Ctrl-C included, and holds a Ctrl-C off between a write and its count of the lines it
  drew: a KeyboardInterrupt between the two left the count stale, and the erase that followed missed the region.
- A frame that cannot be drawn ends the region, never the run: it is erased, draws no more and only routes, and
  on_fault, when given, is told why. Raised in the spinner's thread, or in a display() it wraps, the error would
  otherwise print over the region or fail every later display().

Forked workers inherit the wrapped methods: only the process that started the region draws. Without a frame
function it draws nothing and only routes, for a run whose output is not a terminal.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
import sys
import termios
import threading
import time
from contextlib import contextmanager

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SYNC_ON, SYNC_OFF = "\x1b[?2026h", "\x1b[?2026l"
FRAME_SECONDS = 0.1
# After a prompt, the region comes back only once the run has worked this long: questions one after another, just
# init's, show no frame between them.
SETTLE_SECONDS = 0.5
MIN_ROWS, MIN_COLS = 4, 20


def can_draw(stream=None, environ=None) -> bool:
    """Whether a stream is a terminal that can take the region: one, not dumb, UTF-8 for the glyphs, and with a size.
    A pseudo-terminal nobody gave a size reads 0 by 0, and every line cut to it would be nothing: pre-commit runs its
    hooks on one, script(1) without a terminal of its own makes one."""
    stream = stream or sys.stdout
    environ = os.environ if environ is None else environ
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        tty = False
    encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
    return tty and environ.get("TERM", "") not in ("", "dumb") and encoding.startswith("utf8") and _sized(stream)


def _sized(stream):
    try:
        return os.get_terminal_size(stream.fileno()).columns > 0
    except (OSError, ValueError, AttributeError):
        return False


def _background(fd):
    """Whether this process is in the background of its terminal (just deploy proxmox &): one that changes the
    terminal's settings from there is stopped (SIGTTOU) until brought back. A terminal that is not the process's own
    stops nothing, and says so with an error."""
    try:
        return os.tcgetpgrp(fd) != os.getpgrp()
    except OSError:
        return False


class LiveRegion:
    """The region. frame(cols, rows, glyph) returns its lines; route(kwargs) returns display()'s keyword arguments;
    more says the lines a frame taller than the screen leaves out, {} their number."""

    def __init__(
        self, display, frame=None, route=None, stream=None, more="⋮ {} more lines", on_prompt=None, on_fault=None
    ):
        self._display = display
        self._frame = frame
        self._route = route
        self._on_prompt = on_prompt  # told True as a prompt takes the screen, False once it is answered
        self._on_fault = on_fault  # told the error when a frame cannot be drawn
        self._more = more
        self._stream = stream or sys.stdout
        self._pid = os.getpid()
        self._drawn = 0
        self._size = None
        self._resized = False
        self._tick = 0
        self._paused = False
        self._started = False
        self._stop = threading.Event()
        self._thread = None
        self._title = None
        self._echo = None  # the terminal's settings before the echo went off, while it is off
        self._settle = 0.0  # until when, after a prompt, nothing is drawn
        self.prompting = False

    # --- the screen -------------------------------------------------------------------------------------------
    def _write(self, text):
        self._stream.write(text)
        self._stream.flush()

    @staticmethod
    @contextmanager
    def _uninterrupted():
        """Ctrl-C held off for one write to the terminal and the count of the lines it drew, then delivered.

        Held at Python's level, where it is raised: a signal mask holds the signal in one thread only, the kernel
        hands it to another (Ansible's results thread, the spinner), and Python raises it in the main thread all the
        same. Only the main thread may set a handler, and only it is ever interrupted.
        """
        if threading.current_thread() is not threading.main_thread() or signal.getsignal(signal.SIGINT) is None:
            yield
            return
        held = []
        previous = signal.signal(signal.SIGINT, lambda signum, frame: held.append(frame))
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, previous)
            if held:
                if callable(previous):
                    previous(signal.SIGINT, held[0])
                elif previous == signal.SIG_DFL:
                    signal.raise_signal(signal.SIGINT)

    def _erase(self):
        if self._drawn:
            with self._uninterrupted():
                self._write((f"\x1b[{self._drawn - 1}A" if self._drawn > 1 else "") + "\r\x1b[J")
                self._drawn = 0

    def _terminal_size(self):
        return os.get_terminal_size(self._stream.fileno())

    def redraw(self):
        """Draw the frame again, in place. A no-op while stopped, paused for a prompt, settling after one, or without
        a frame."""
        if not self._frame or self._stop.is_set() or self._paused or not self._started:
            return
        if time.monotonic() < self._settle:
            return
        with self._display._lock:
            try:
                cols, rows = self._terminal_size()
            except (OSError, ValueError, AttributeError):
                return
            if self._resized or (self._size and self._size != (cols, rows)):
                # Everything on screen goes up into the scrollback, where nothing is lost, and the region is drawn
                # again from the top of an empty screen: no count of what was drawn before the resize holds.
                self._resized = False
                with self._uninterrupted():
                    self._write("\n" * rows + f"\x1b[{rows - 1}A\r")
                    self._drawn = 0
            self._size = (cols, rows)
            try:
                lines = self._frame(cols, rows, FRAMES[self._tick % len(FRAMES)])
            except Exception as error:  # noqa: BLE001 - a frame that cannot be drawn ends the region, never the run
                self._fault(error)
                return
            if rows < MIN_ROWS or cols < MIN_COLS:
                lines = []
            limit = rows - 1
            # A frame taller than the screen keeps its top and its last line; a screen too small has none at all -
            # a terminal that reports 0 rows, a pseudo-terminal nobody gave a size, among them.
            if lines and len(lines) > limit:
                lines = [*lines[: limit - 2], self._more.format(len(lines) - limit + 1)[: cols - 1], lines[-1]]
            with self._uninterrupted():
                up = f"\x1b[{self._drawn - 1}A" if self._drawn > 1 else ""
                self._write(SYNC_ON + up + "\r" + "\x1b[K\n".join(lines) + "\x1b[K\x1b[J" + SYNC_OFF)
                self._drawn = len(lines)

    def _fault(self, error):
        """A frame that cannot be drawn: the region erased, and nothing drawn again; what prints is routed as before."""
        with self._display._lock:
            self._erase()
            self._frame = None
            self._quiet(False)
        if self._on_fault is not None:
            self._on_fault(error)

    def title(self, text):
        """The terminal's title, written when it changes."""
        if self._frame and text != self._title and os.getpid() == self._pid:
            self._title = text
            with self._display._lock:
                self._write(f"\x1b]0;{text}\x07")

    def bell(self):
        if self._frame and os.getpid() == self._pid:
            with self._display._lock:
                self._write("\a")

    def _quiet(self, on):
        """The terminal's echo off (on=True), or back as it was. Nothing for an input that is no terminal, or from the
        background, where the shell holds the terminal and its settings: a run sent there after it started (Ctrl-Z,
        bg) leaves them to it."""
        try:
            fd = sys.stdin.fileno()
            if on and self._echo is None and os.isatty(fd) and not _background(fd):
                settings = termios.tcgetattr(fd)
                quiet = [*settings[:3], settings[3] & ~termios.ECHO, *settings[4:]]
                termios.tcsetattr(fd, termios.TCSADRAIN, quiet)
                self._echo = settings
            elif not on and self._echo is not None:
                echo, self._echo = self._echo, None
                if not _background(fd):
                    termios.tcsetattr(fd, termios.TCSADRAIN, echo)
        except (OSError, ValueError, AttributeError, termios.error):
            self._echo = None

    # --- the wrappers -----------------------------------------------------------------------------------------
    def _wrap(self):
        display = self._display
        original_display = display.display

        def wrapped_display(
            msg, color=None, stderr=False, screen_only=False, log_only=False, newline=True, caplevel=None
        ):
            kwargs = {"color": color, "stderr": stderr, "screen_only": screen_only, "log_only": log_only,
                      "newline": newline, "caplevel": caplevel}  # fmt: skip
            # A forked worker's display goes over the queue to this process, which draws.
            if os.getpid() != self._pid:
                return original_display(msg, **kwargs)
            # Routed even once the region is finished: what prints after it, a recap, follows the same rule.
            if self._route and not self.prompting:
                kwargs = self._route(kwargs)
            # Under the lock either way, so every write to the log takes the locks in one order.
            if kwargs.get("log_only") or not self._frame or self._stop.is_set():
                with display._lock:
                    return original_display(msg, **kwargs)
            with display._lock:
                self._erase()
                original_display(msg, **kwargs)
                self.redraw()

        def stepping_aside(original):
            def wrapped_prompt(*args, **kwargs):
                if os.getpid() != self._pid:
                    return original(*args, **kwargs)
                # The prompt prints through display() or input(), and goes to the screen whatever the route says.
                with display._lock:
                    self._erase()
                    self._paused = True
                    self.prompting = True
                    self._quiet(False)
                if self._on_prompt is not None:
                    self._on_prompt(True)
                try:
                    return original(*args, **kwargs)
                finally:
                    if self._on_prompt is not None:
                        self._on_prompt(False)
                    with display._lock:
                        self._paused = False
                        self.prompting = False
                        if self._frame and not self._stop.is_set():
                            self._quiet(True)
                        self._settle = time.monotonic() + SETTLE_SECONDS

            return wrapped_prompt

        display.display = wrapped_display
        display.prompt_until = stepping_aside(display.prompt_until)
        display.do_var_prompt = stepping_aside(display.do_var_prompt)

    def _spin(self):
        while not self._stop.wait(FRAME_SECONDS):
            with self._display._lock:
                self._tick += 1
                self.redraw()

    # --- the life of it ---------------------------------------------------------------------------------------
    def start(self):
        """Wrap Display, and with a frame, take the terminal - SIGWINCH, the echo, an erase at exit - from the main
        thread, which alone may set a signal handler. The region is drawn by redraw(), and by spin()'s ticks."""
        if self._started:
            return
        self._started = True
        self._wrap()
        if self._frame:
            signal.signal(signal.SIGWINCH, lambda *_: setattr(self, "_resized", True))
            atexit.register(self.finish)
            self._quiet(True)

    def spin(self):
        """Redraw ten times a second, for the spinner, until finish()."""
        if self._frame and self._started and self._thread is None:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()

    def finish(self):
        """Erase the region and draw no more; Display's output goes on as it would without the region."""
        if self._started and not self._stop.is_set() and os.getpid() == self._pid:
            with self._display._lock:
                self._erase()
                self._stop.set()
                self._quiet(False)


class Output:
    """What every live output does around its region, whatever it draws: whether to draw at all, the region, and its
    own words printed coloured to the screen and plain to the log.

    live says whether there is a region (can_draw(), unless the caller decided: a run piped is drawn as lines);
    color whether the screen gets the palette (the caller's ANSIBLE_NOCOLOR). With to_log, everything Display prints
    goes to the log alone - the stream a drawn run replaces - but what is printed inside say(), and anything on
    stderr, which still reaches the screen."""

    def __init__(
        self,
        display,
        frame=None,
        live=None,
        color=False,
        to_log=False,
        more="⋮ {} more lines",
        on_prompt=None,
        on_fault=None,
    ):
        self.display = display
        self.live = can_draw() if live is None else live
        self.color = bool(color) and self.live
        self._speaking = threading.local()
        self.region = LiveRegion(
            display,
            frame=frame if self.live else None,
            route=self._route if to_log else None,
            more=more,
            on_prompt=on_prompt,
            on_fault=on_fault,
        )

    # --- the region's life, as LiveRegion's --------------------------------------------------------------------
    def start(self):
        self.region.start()

    def redraw(self):
        self.region.redraw()

    def spin(self):
        self.region.spin()

    def finish(self):
        self.region.finish()

    def title(self, text):
        self.region.title(text)

    def bell(self):
        self.region.bell()

    # --- the output's own words --------------------------------------------------------------------------------
    @contextlib.contextmanager
    def say(self):
        """What is printed inside reaches the screen, where the route would send it to the log alone."""
        previous = getattr(self._speaking, "on", False)
        self._speaking.on = True
        try:
            yield
        finally:
            self._speaking.on = previous

    def _route(self, kwargs):
        if getattr(self._speaking, "on", False) or kwargs.get("stderr"):
            return kwargs
        return {**kwargs, "log_only": True}

    def print(self, lines):
        """Lines of the output's own, (coloured, plain) each: the colour to the screen, the plain text to the log."""
        with self.say():
            for coloured, plain in lines:
                if self.color:
                    self.display.display(coloured, screen_only=True)
                    self.display.display(plain, log_only=True)
                else:
                    self.display.display(plain)


class FilterModule:
    """No filters: a callback loads this module by path. Here so the filter loader takes the file as a plugin."""

    def filters(self):
        return {}
