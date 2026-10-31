"""The model of a run drawn live (filter_plugins/run_view.py): every moment of a deploy drawn from events alone, at
100 and 80 columns, with no line over the width; the bars that never move back; a stage this setup lacks; the lines
off a terminal; and the summary."""

import importlib.util
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("test_run_view_module", ROOT / "filter_plugins" / "run_view.py")
view = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(view)
deploy = view.recipes.recipe_for("site.yml")

# Two plays of site.yml, as the engine hands them over: the NAS's (the role's validate.yml, connect.yml, pools.yml)
# and the Proxmox host's (a refusal of its own, then two roles), each task (key, role, meta).
NAS = [
    ("n0", None, {"file": "/r/truenas.yml"}),  # the play's own: its first step's
    ("n1", "truenas", {"file": "/r/roles/truenas/tasks/validate.yml"}),
    ("n2", "truenas", {"file": "/r/roles/truenas/tasks/connect.yml"}),
    ("n3", "truenas", {"file": "/r/roles/truenas/tasks/connect.yml"}),
    ("n4", "truenas", {"file": "/r/roles/truenas/tasks/pools.yml"}),
]
PROXMOX = [
    ("p0", None, {"file": "/r/proxmox.yml"}),
    ("p1", "proxmox_apt", {"file": "/r/roles/proxmox_apt/tasks/main.yml"}),
    ("p2", "proxmox_apt", {"file": "/r/roles/proxmox_apt/tasks/main.yml"}),
    ("p3", "proxmox_storage", {"file": "/r/roles/proxmox_storage/tasks/main.yml", "include": 3}),
    ("p4", "proxmox_storage", {"file": "/r/roles/proxmox_storage/tasks/main.yml"}),
]
PLAYS = [
    ("nas", "/r/truenas.yml:10", NAS, "Configure the TrueNAS storage server"),
    ("pve", "/r/proxmox.yml:12", PROXMOX, "Configure the Proxmox host's packages and VM storage"),
]
START = time.localtime(0)


def run_of(plays=PLAYS, check=False):
    run = view.Run("deploy", 0.0, check=check, log="logs/x.log", setup="home", started_at=START, recipe=deploy)
    run.plays(plays)
    return run


def play(run, key, tasks, now, outcomes=None):
    """Each task of a play started and ended, a second each, from now: the time after."""
    run.play_start(key)
    for task, role, _meta in tasks:
        run.task(task, f"task {task}", role, now, play=key)
        run.result(task, "host", (outcomes or {}).get(task, "ok"), now + 1)
        now += 1
    return now


def frame(run, now, cols, rows=40):
    return run.frame(now, cols, rows, "⠋", color=False)


def test_stages_and_steps_come_from_the_declaration_and_the_neighbour_takes_a_plays_own_task():
    run = run_of()
    assert [s.label for s in run.stages] == ["NAS", "Proxmox host"], "only the stages this run's plays reach"
    nas, pve = run.stages
    assert [s.expected for s in nas.steps][:3] == [4, 0, 1] and nas.expected == 5
    # The include's file counts 3 more, at the start.
    assert [s.expected for s in pve.steps] == [3, 0, 5, 0, 0] and pve.expected == 8
    assert run.plan["n0"] == (0, 0) and run.plan["p0"] == (1, 0)


@pytest.mark.parametrize("cols", [100, 80])
def test_every_moment_fits_the_width(cols):
    run = run_of()
    lines = frame(run, 0.5, cols)
    assert lines[0].startswith("homeserver  deploy · home · started ")
    assert any("○ NAS" in line and "connect · network · pools" in line for line in lines)
    now = play(run, "nas", NAS[:3], 1.0)
    lines = frame(run, now, cols)
    assert all(len(line) <= cols for line in lines), lines
    assert any(line.startswith(" ⠋ NAS") for line in lines), "the running stage"
    assert any(line.startswith("     ⠋ connect") for line in lines), "its running step, under it"
    assert any("✓ task n2" in line for line in lines), "its last tasks, under it"
    assert lines[-1].startswith("⠋ 0:0") and "stage 1 of 2 · NAS" in lines[-1] and "logs/x.log" in lines[-1]
    now = play(run, "nas", NAS[3:], now)
    now = play(run, "pve", PROXMOX, now, {"p2": "changed"})
    run.end(now)
    lines = frame(run, now, cols)
    assert all(len(line) <= cols for line in lines), lines
    assert any(line.startswith(" ✓ NAS") for line in lines) and any(
        line.startswith(" ✓ Proxmox host") for line in lines
    )
    if cols >= 90:
        assert any("1 changed" in line for line in lines if "Proxmox host" in line)


def test_a_running_task_says_its_try_its_items_and_its_time():
    run = run_of()
    run.play_start("pve")
    run.task("p1", "Refresh the package lists", "proxmox_apt", 0.0, play="pve")
    run.tried("p1", 30.0, 3, 60, 10)
    run.item("p1")
    run.item("p1")
    line = next(line for line in frame(run, 31.0, 100) if "Refresh the package lists" in line)
    assert "2 items" in line and "try 3 of 60 · up to 10:00" in line and line.endswith("0:31")


def test_a_bar_never_moves_back_and_a_step_that_ended_reads_whole():
    run = run_of()
    run.play_start("pve")
    run.task("p3", "Include", "proxmox_storage", 0.0, play="pve")
    storage = run.stages[1].steps[2]
    first = storage.fraction()
    # The include counted at the start: its event takes nothing more; a second inclusion adds its tasks.
    run.include("p3", 3)
    assert storage.expected == 5
    run.include("p3", 3)
    assert storage.expected == 8 and storage.fraction() >= first
    run.task("p4", "Last", "proxmox_storage", 1.0, play="pve")
    run.end(2.0)
    assert storage.fraction() == 1.0


def test_a_task_the_start_did_not_count_is_expected_as_it_starts():
    run = run_of()
    run.play_start("pve")
    run.task("facts", "Gathering Facts", None, 0.0, play="pve", counted=False)
    assert run.stages[1].expected == 9 and run.find("facts").step == 0


def test_a_stage_this_setup_lacks_is_drawn_absent_with_its_reason():
    run = run_of()
    run.play_start("nas")
    run.task("why", "Say why the NAS is skipped", None, 0.0, play="nas", counted=False)
    run.absent("why")
    run.note(run.find("why"), "The NAS is skipped: local.yml gives it no address. Everything stays on the host.")
    run.result("why", "truenas", "ok", 0.1)
    play(run, "pve", PROXMOX, 1.0)
    lines = frame(run, 7.0, 100)
    assert any(line.startswith(f" {view.DASH} NAS") and "The NAS is skipped" in line for line in lines), lines
    piped = run.ended_lines(7.0)
    assert piped[0].startswith(f"{view.DASH} NAS") and "The NAS is skipped" in piped[0]


def test_a_short_terminal_folds_the_steps_to_come_then_shows_fewer_tasks():
    run = run_of()
    run.play_start("nas")
    for number in range(8):
        file = "/r/roles/truenas/tasks/connect.yml"
        run.task(f"x{number}", f"task {number}", "truenas", float(number), play="nas", file=file)
        run.result(f"x{number}", "host", "ok", number + 0.5)
    tall = frame(run, 9.0, 100, rows=40)
    short = frame(run, 9.0, 100, rows=10)
    assert len(short) <= 10 < len(tall)
    # Folded: the steps to come on one line, and those with nothing in this run left out.
    assert "     ○ pools" in short and not any("network" in line for line in short), short
    assert any(line.startswith(f"     {view.DASH} network") and "not in this run" in line for line in tall), tall


def test_off_a_terminal_a_line_as_each_step_ends():
    run = run_of()
    now = play(run, "nas", NAS, 0.0)
    lines = run.ended_lines(now)
    assert [line.split("  ")[0] for line in lines] == ["✓ NAS · connect"], lines
    now = play(run, "pve", PROXMOX, now, {"p1": "changed"})
    run.end(now)
    lines = run.ended_lines(now)
    assert lines[0].startswith("✓ NAS · pools") and "1 task" in lines[0]
    assert any(line.startswith("✓ Proxmox host · packages") and "1 changed" in line for line in lines), lines
    assert run.ended_lines(now) == [], "each line once"


def test_the_summary_says_how_it_ended_each_stage_what_changed_next_steps_and_the_exit():
    run = run_of()
    now = play(run, "nas", NAS, 0.0)
    run.note(run.find("n3"), "TrueNAS 25.10.7 at the NAS: pools persistent, ephemeral")
    now = play(run, "pve", PROXMOX, now, {"p2": "changed", "p4": "failed"})
    run.end(now, code=2)
    item = view.screen.Line().add(" ● reboot      the host waits for a reboot")
    lines = [plainly for _coloured, plainly in run.summary(now, 100, [item], color=False)]
    assert lines[0].startswith("homeserver  deploy · home · failed after 10 s at Proxmox host · storage"), lines[0]
    assert lines[2].startswith("STAGES") and "CHANGED" in lines[2]
    assert any(line.startswith(" ✓ NAS") for line in lines) and any(
        line.startswith(" ✗ Proxmox host") for line in lines
    )
    assert any(line.startswith("NAS") and "TrueNAS 25.10.7" in line for line in lines)
    assert any(line.startswith("CHANGED") and "task p2" in line for line in lines)
    assert " ● reboot      the host waits for a reboot" in lines
    assert lines[-2] == "10 tasks · 1 changed · 1 failed · log logs/x.log"
    assert lines[-1] == "exit 2 · for the whole picture: just status"
    assert all(len(line) <= 100 for line in lines)


def test_a_dry_run_says_so_and_what_would_change():
    run = run_of(check=True)
    now = play(run, "pve", PROXMOX, 0.0, {"p2": "changed"})
    assert "dry run, nothing is changed" in frame(run, now, 100)[0]
    run.end(now)
    lines = [plainly for _c, plainly in run.summary(now, 100)]
    assert " · dry run · done in" in lines[0] and "WOULD" in lines[2]
    assert any(line.startswith("WOULD CHANGE") for line in lines) and "1 would change" in lines[-2]


def test_a_run_of_one_stage_is_drawn_as_its_steps():
    run = run_of(plays=PLAYS[1:])
    now = play(run, "pve", PROXMOX[:2], 0.0)
    lines = frame(run, now, 100)
    assert not any("Proxmox host" in line for line in lines[1:]), "no stage row"
    assert any(line.startswith(" ⠋ packages") for line in lines) and "step 1 of 5" in lines[-1]
    assert run.title_text(now).startswith("deploy · ")
    run.end(now + 1)
    lines = [plainly for _c, plainly in run.summary(now + 1, 100)]
    assert lines[2].startswith("STEPS") and run.title_text(now + 1) == "deploy · done"


def test_a_playbook_no_declaration_names_is_a_stage_per_play_and_its_roles():
    plays = [
        ("a", "/r/suspend.yml:12", [("a0", None, {})], "Refuse a cluster without the kubernetes inventory group"),
        ("b", "/r/suspend.yml:25", [("b0", "app_scale", {}), ("b1", "app_scale", {})], "Spin the apps down"),
    ]
    run = view.Run("suspend", 0.0, started_at=START)
    run.plays(plays)
    assert [s.label for s in run.stages] == [
        "Refuse a cluster without the kubernetes inventory group",
        "Spin the apps down",
    ]
    assert [step.label for step in run.stages[0].steps] == ["tasks"]
    assert [step.label for step in run.stages[1].steps] == ["app_scale"]
