"""The live region every live output draws with: filter_plugins/live_region.py, against a stand-in Display. Ported
from claude-on-proxmox's tests/unit/test_live_region.py, with the module.

What is printed goes above the region and the region is drawn again below it; a frame taller than the screen is cut
and says how much; a prompt - pause's prompt_until or a vars_prompt's do_var_prompt - has the screen to itself; the
route sends what it is given to the log alone; a screen too small gets no region; the echo is off while it draws,
on a pseudo-terminal of the test's own; a finish holding the lock the spinner waits for never waits for it; and the
Display it wraps is held to ansible-core's. Each live output's own tests run it for real at a pseudo-terminal:
tests/integration/test_status.py's for just status.
"""

from __future__ import annotations

import importlib.util
import inspect
import io
import os
import signal
import termios
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("test_live_region_module", REPO / "filter_plugins" / "live_region.py")
live = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(live)


class Display:
    """What LiveRegion needs of Ansible's Display: display, prompt_until, and the lock."""

    def __init__(self, stream):
        self._lock = threading.RLock()
        self.stream = stream
        self.logged = []

    def display(self, msg, color=None, stderr=False, screen_only=False, log_only=False, newline=True, caplevel=None):
        if not log_only:
            self.stream.write(msg + ("\n" if newline else ""))
        if not screen_only:
            self.logged.append(msg)

    def prompt_until(self, msg, private=False, seconds=None, interrupt_input=None, complete_input=None):
        self.display(msg, newline=False)
        return b"typed"

    def do_var_prompt(self, varname, private=True, prompt=None, **kwargs):
        self.stream.write(f"{prompt}: ")
        return "typed"


class Screen(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture(autouse=True)
def _quiet_region(monkeypatch):
    """An input that is no terminal - under pytest -s it would be the developer's, whose echo the region turns off -
    no spinner between two looks at the screen, and SIGWINCH's handler put back."""
    monkeypatch.setattr(live.sys, "stdin", io.StringIO())
    monkeypatch.setattr(live, "FRAME_SECONDS", 3600)
    handler = signal.getsignal(signal.SIGWINCH)
    yield
    signal.signal(signal.SIGWINCH, handler)


def _region(frame=None, route=None):
    stream = Screen()
    display = Display(stream)
    region = live.LiveRegion(display, frame=frame, route=route, stream=stream)
    region._terminal_size = lambda: (40, 10)
    return region, display, stream


def test_a_region_is_drawn_only_at_a_terminal_that_takes_utf8():
    class Pipe(io.StringIO):
        encoding = "utf-8"

    class Terminal(Screen):
        encoding = "UTF-8"

    class Latin(Screen):
        encoding = "latin-1"

    assert live.can_draw(Terminal(), {"TERM": "xterm-256color"})
    assert not live.can_draw(Pipe(), {"TERM": "xterm-256color"})
    assert not live.can_draw(Terminal(), {"TERM": "dumb"})
    assert not live.can_draw(Terminal(), {})
    assert not live.can_draw(Latin(), {"TERM": "xterm"})


def test_what_is_printed_goes_above_the_region_which_is_drawn_again_below_it():
    region, display, stream = _region(frame=lambda cols, rows, glyph: ["one", "two"])
    region.start()
    region.redraw()
    try:
        drawn = stream.getvalue()
        assert drawn == f"{live.SYNC_ON}\rone\x1b[K\ntwo\x1b[K\x1b[J{live.SYNC_OFF}"
        stream.seek(0)
        stream.truncate()
        display.display("said")
        # Up one line, to the region's first, erased below; the words; the region again.
        assert stream.getvalue() == f"\x1b[1A\r\x1b[Jsaid\n{live.SYNC_ON}\rone\x1b[K\ntwo\x1b[K\x1b[J{live.SYNC_OFF}"
        assert display.logged == ["said"]
    finally:
        region.finish()
    assert stream.getvalue().endswith("\x1b[1A\r\x1b[J")


def test_a_frame_that_cannot_be_drawn_ends_the_region_never_the_run():
    """The frame raises on its second draw: the region is erased and never drawn again, display() goes on working and
    routing, and the owner is told why."""
    draws, faults = [], []

    def frame(cols, rows, glyph):
        draws.append(glyph)
        if len(draws) > 1:
            raise ValueError("no such row")
        return ["one", "two"]

    stream = Screen()
    display = Display(stream)
    region = live.LiveRegion(display, frame=frame, stream=stream, on_fault=faults.append)
    region._terminal_size = lambda: (40, 10)
    region.start()
    region.redraw()
    try:
        stream.seek(0)
        stream.truncate()
        display.display("said")
        assert stream.getvalue() == "\x1b[1A\r\x1b[Jsaid\n", "erased, the words, and no region after them"
        assert [type(e) for e in faults] == [ValueError] and len(draws) == 2
        display.display("more")
        region.redraw()
        assert stream.getvalue().endswith("said\nmore\n") and len(draws) == 2, "never drawn again"
    finally:
        region.finish()


@pytest.mark.parametrize("step", ["redraw", "finish"])
def test_a_ctrl_c_between_a_write_and_its_count_leaves_the_count_right(step):
    # A Ctrl-C after a frame (or its erase) reached the terminal and before its lines were counted left the count
    # stale: the erase that followed missed the region, which stayed on the screen above the run's last words. Here
    # the signal comes at that very moment, from the write itself, with a second thread alive as Ansible's results
    # thread is: the kernel may hand the signal to it, which is why a signal mask in the main thread held nothing off.
    region, _, _ = _region(frame=lambda cols, rows, glyph: ["one", "two", "three"])
    region.start()
    if step == "finish":
        region.redraw()
    written = region._write

    def write_then_interrupt(text):
        written(text)
        os.kill(os.getpid(), signal.SIGINT)

    # Python's own handler, whatever started the suite: under nohup SIGINT is ignored.
    held = signal.signal(signal.SIGINT, signal.default_int_handler)
    idle = threading.Event()
    other = threading.Thread(target=idle.wait, daemon=True)
    other.start()
    region._write = write_then_interrupt
    try:
        with pytest.raises(KeyboardInterrupt):
            getattr(region, step)()
    finally:
        region._write = written
        idle.set()
        other.join()
        signal.signal(signal.SIGINT, held)
    assert region._drawn == (3 if step == "redraw" else 0)
    region.finish()


def test_a_frame_taller_than_the_screen_is_cut_and_says_how_much():
    region, _, stream = _region(frame=lambda cols, rows, glyph: [f"line {n}" for n in range(20)])
    region.start()
    region.redraw()
    region.finish()
    drawn = stream.getvalue().split(live.SYNC_ON, 1)[1].split(live.SYNC_OFF, 1)[0]
    lines = drawn.replace("\x1b[K", "").replace("\x1b[J", "").lstrip("\r").split("\n")
    assert lines == [*(f"line {n}" for n in range(7)), "⋮ 12 more lines", "line 19"]


def test_a_prompt_has_the_screen_to_itself_and_the_region_comes_back_after():
    region, display, stream = _region(frame=lambda cols, rows, glyph: ["region"])
    seen = []

    def prompt_until(msg, **kwargs):
        seen.append((stream.getvalue(), region.prompting))
        region.redraw()  # the spinner's: nothing while the prompt is up
        seen.append(stream.getvalue())
        return b"typed"

    display.prompt_until = prompt_until
    region.start()
    region.redraw()
    try:
        assert display.prompt_until("Code:") == b"typed"
        # Back only once the run has worked half a second: the next question, at once, finds nothing drawn.
        region.redraw()
        settled = stream.getvalue()
        time.sleep(live.SETTLE_SECONDS)
        region.redraw()
    finally:
        region.finish()
    (before, prompting), during = seen
    assert prompting and before.endswith("\r\x1b[J") and during == before and settled == before
    assert stream.getvalue()[len(before) :].startswith(f"{live.SYNC_ON}\rregion")


def test_the_route_sends_to_the_log_alone_and_never_a_prompt():
    region, display, stream = _region(frame=None, route=lambda kwargs: {**kwargs, "log_only": True})
    region.start()
    region.redraw()
    display.display("task output")
    assert stream.getvalue() == "" and display.logged == ["task output"]
    assert display.prompt_until("Code:") == b"typed"
    assert stream.getvalue() == "Code:"
    region.finish()
    display.display("after the end")
    assert display.logged[-1] == "after the end" and "after the end" not in stream.getvalue()


# 0 by 0: a pseudo-terminal nobody gave a size, as script(1) makes one without a terminal of its own.
@pytest.mark.parametrize("size", [(40, 3), (40, 1), (0, 0)])
def test_a_region_on_a_screen_too_small_for_one_draws_nothing(size):
    region, _, stream = _region(frame=lambda cols, rows, glyph: ["one", "two"])
    region._terminal_size = lambda: size
    region.start()
    region.redraw()
    region.finish()
    assert "one" not in stream.getvalue()


def test_a_region_steps_aside_for_a_vars_prompt_too():
    region, display, stream = _region(frame=lambda cols, rows, glyph: ["region"])
    seen = []
    display.do_var_prompt = lambda *args, **kwargs: seen.append((stream.getvalue(), region.prompting)) or "typed"
    region.start()
    region.redraw()
    try:
        assert display.do_var_prompt("confirm_destroy", prompt="Delete these VMs?") == "typed"
    finally:
        region.finish()
    before, prompting = seen[0]
    assert prompting and before.endswith("\r\x1b[J")


def test_the_region_turns_the_echo_off_while_it_draws_and_on_for_a_prompt_and_at_the_end(monkeypatch):
    # A pseudo-terminal of the test's own, never the developer's.
    master, slave = os.openpty()
    terminal = os.fdopen(slave, "r")
    monkeypatch.setattr(live.sys, "stdin", terminal)
    echoes = lambda: bool(termios.tcgetattr(slave)[3] & termios.ECHO)  # noqa: E731
    try:
        assert echoes()
        region, display, _ = _region(frame=lambda cols, rows, glyph: ["region"])
        during = []
        display.prompt_until = lambda *args, **kwargs: during.append(echoes()) or b"typed"
        region.start()
        region.redraw()
        assert not echoes()
        display.prompt_until("Code:")
        assert during == [True] and not echoes()
        region.finish()
        assert echoes()
    finally:
        terminal.close()
        os.close(master)


def test_a_title_is_written_when_it_changes_and_a_bell_when_asked():
    region, _, stream = _region(frame=lambda cols, rows, glyph: [])
    region.title("deploy alpha")
    region.title("deploy alpha")
    region.bell()
    assert stream.getvalue() == "\x1b]0;deploy alpha\x07\x07"
    quiet, _, quiet_stream = _region(frame=None)
    quiet.title("deploy alpha")
    quiet.bell()
    assert quiet_stream.getvalue() == ""


def test_the_display_the_region_wraps_is_the_one_it_was_built_against():
    # The region replaces display and prompt_until and takes _lock (live_region.py): a prompt prints through
    # display before it locks to read the keys, which is why the region can step aside first.
    from ansible.utils.display import Display

    parameters = list(inspect.signature(Display.display).parameters)
    assert parameters == ["self", "msg", "color", "stderr", "screen_only", "log_only", "newline", "caplevel"]
    source = inspect.getsource(Display.prompt_until)
    assert "self._final_q.send_prompt(" in source, "a worker's prompt is the main process's to print"
    assert source.index("self.display(msg)") < source.index("with self._lock:")
    # A vars_prompt reads with input(), through the prompt its do_var_prompt looks up each time.
    assert "do_prompt = self.prompt" in inspect.getsource(Display.do_var_prompt)
    assert "input(msg)" in inspect.getsource(Display.prompt)
    assert isinstance(Display()._lock, type(threading.RLock()))


def _output(**kwargs):
    stream = Screen()
    display = Display(stream)
    out = live.Output(display, live=False, **kwargs)
    return out, display, stream


def test_an_outputs_own_words_go_coloured_to_the_screen_and_plain_to_the_log():
    out, display, stream = _output(color=True, to_log=True)
    out.start()
    # Without a region there is no colour: a pipe gets the plain words, once, on the screen and in the log.
    assert out.color is False
    out.print([("\x1b[1msaid\x1b[0m", "said")])
    assert stream.getvalue() == "said\n" and display.logged == ["said"]
    out.finish()
    coloured = live.Output(Display(Screen()), frame=lambda cols, rows, glyph: [], live=True, color=True)
    assert coloured.color is True and coloured.region._frame is not None


def test_with_to_log_only_the_outputs_own_words_and_stderr_reach_the_screen():
    out, display, stream = _output(to_log=True)
    out.start()
    display.display("a task's line")
    display.display("a warning", stderr=True)
    out.print([("the run's own", "the run's own")])
    with out.say():
        display.display("said inside")
    out.finish()
    assert stream.getvalue() == "a warning\nthe run's own\nsaid inside\n"
    assert display.logged == ["a task's line", "a warning", "the run's own", "said inside"]


def test_without_to_log_everything_reaches_the_screen():
    out, display, stream = _output()
    out.start()
    display.display("a line")
    out.finish()
    assert stream.getvalue() == "a line\n" and display.logged == ["a line"]


def test_a_finish_holding_the_lock_the_spinner_waits_for_never_waits_for_it(monkeypatch):
    """just status finishes its region holding Display's lock, which the spinner's thread takes to draw: finish never
    waits for the thread, which once it has the lock finds the region finished and draws nothing more."""
    monkeypatch.setattr(live, "FRAME_SECONDS", 0.01)
    region, display, stream = _region(frame=lambda cols, rows, glyph: [glyph])
    region.start()
    region.spin()
    with display._lock:
        time.sleep(0.1)
        start = time.monotonic()
        region.finish()
        assert time.monotonic() - start < 1
    drawn = stream.getvalue()
    time.sleep(0.05)
    assert stream.getvalue() == drawn, "drawn after the finish"
