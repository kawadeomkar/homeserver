"""What every report and live output draws with: filter_plugins/screen.py's lines, palette and attention items."""

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("test_screen_module", ROOT / "filter_plugins" / "screen.py")
screen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(screen)


def test_a_line_is_cut_to_a_width_with_a_dim_ellipsis_and_left_whole_when_it_fits():
    line = [("reading  ", ""), ("proxmox", "g"), (" 1.2 s", "d")]
    assert screen.cut(line, 22) == line
    assert screen.cut(line, 12) == [("reading  ", ""), ("pr", "g"), ("…", "d")]
    assert screen.width(screen.cut(line, 12)) == 12


def test_a_line_renders_in_the_palette_or_plain_and_a_url_as_a_link():
    line = [("APPS", "h"), ("  ", "d"), ("https://jellyfin.example", "u"), ("   ", "")]
    assert screen.render(line, False) == "APPS  https://jellyfin.example"
    coloured = screen.render(line, True)
    assert coloured.startswith("\033[1;34mAPPS\033[0m  "), "a style on spaces alone is left out"
    assert "\033]8;;https://jellyfin.example\033\\" in coloured and coloured.endswith("\033]8;;\033\\")


def test_cells_line_up_with_or_without_colour():
    line = screen.Line().add("vm", "b").cell("300", 6, right=True).to(12).add("running", "g")
    assert line.text(False) == "vm   300    running" and len(line) == 19
    assert line.text(True).endswith("\033[32mrunning\033[0m")


def test_text_fills_lines_without_breaking_a_word_and_names_join_as_a_sentence():
    assert screen.fill("see scripts/runbooks/a-very-long-runbook-name.md now", 20) == [
        "see",
        "scripts/runbooks/a-very-long-runbook-name.md",
        "now",
    ]
    assert screen.fill("", 10) == [""]
    assert [screen.and_list(n) for n in ([], ["a"], ["a", "b"], ["a", "b", "c"])] == ["", "a", "a and b", "a, b and c"]
    assert screen.plural(1, "task") == "1 task" and screen.plural(2, "task") == "2 tasks"


def test_bars_sparks_and_how_full():
    assert screen.bar(0.5, 4) == "██░░" and screen.bar(2, 4) == "████" and screen.bar(-1, 4) == "░░░░"
    assert [screen.fullness(f) for f in (0.5, 0.8, 0.95)] == ["c", "y", "r"]
    assert screen.spark([0.0, None, 0.8]) == [("▁", "c"), (" ", ""), ("▇", "y")]


def test_attention_items_wrap_to_a_width_or_stay_whole_each_on_its_line():
    items = [
        {"level": "todo", "subject": "reboot", "text": "the host waits for a reboot", "command": "reboot it later"},
        {"level": "problem", "subject": "home_assistant", "text": "a long subject", "command": ""},
    ]
    whole = [line.text(False) for line in screen.attention_items(items)]
    assert whole == [
        " ● reboot      the host waits for a reboot",
        "               → reboot it later",
        " ● home_assistant a long subject",
    ]
    wrapped = [line.text(False) for line in screen.attention_items(items[:1], 30)]
    assert wrapped == [
        " ● reboot      the host waits",
        "               for a reboot",
        "               → reboot it",
        "                 later",
    ]
    assert screen.attention_items(items)[0].text(True).startswith(" \033[33m●\033[0m \033[1mreboot\033[0m")
