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


# suspend.yml's plays, as the engine hands them over: the refusal on localhost and the apps' play, whose role includes
# its wave file once per wave; Home Assistant's; and the cluster's VMs', which --all alone draws.
SUSPEND = view.recipes.recipe_for("suspend.yml")
REFUSE = "Refuse a cluster without the kubernetes inventory group"
MAIN = {"file": "/r/roles/app_scale/tasks/main.yml"}
WAVE = "/r/roles/app_scale/tasks/wave.yml"
SCALE_PLAYS = [
    ("refuse", "/r/suspend.yml:11", [("r0", None, {"file": "/r/suspend.yml"})], REFUSE),
    (
        "apps",
        "/r/suspend.yml:24",
        [
            ("a0", None, {"file": "/r/suspend.yml"}),
            ("a1", "app_scale", MAIN),
            ("a2", "app_scale", MAIN | {"include": 3, "included": WAVE}),
            ("a3", "app_scale", MAIN),
        ],
        "Spin the apps down",
    ),
    (
        "haos",
        "/r/suspend.yml:70",
        [
            ("h0", None, {"file": "/r/suspend.yml"}),
            ("h1", "proxmox_vm", {"file": "/r/roles/proxmox_vm/tasks/power.yml"}),
        ],
        "Shut Home Assistant's VM down",
    ),
    (
        "vms",
        "/r/suspend.yml:130",
        [
            ("v0", None, {"file": "/r/suspend.yml"}),
            ("v1", "proxmox_vm", {"file": "/r/roles/proxmox_vm/tasks/power.yml"}),
        ],
        "Shut the cluster's VMs down",
    ),
]


def suspend(options=None, check=False):
    run = view.Run("suspend", 0.0, check=check, log="logs/x.log", started_at=START, recipe=SUSPEND, options=options)
    run.plays(SCALE_PLAYS)
    return run


def waves(run, now):
    """The apps' play: its refusal, its plan, both waves of three tasks, its report; the time after."""
    run.play_start("refuse", now)
    run.task("r0", "Refuse a cluster without the kubernetes inventory group", None, now, play="refuse")
    run.result("r0", "localhost", "ok", now + 0.1)
    run.play_start("apps", now + 0.2)
    for number, key in enumerate(("a0", "a1", "a2")):
        run.task(key, f"task {key}", None if key == "a0" else "app_scale", now + 1 + number, play="apps")
        run.result(key, "k8s", "ok", now + 1.5 + number)
    run.include("a2", 3, "apps")
    run.include("a2", 3, "late apps")
    now += 4
    for wave in ("apps", "late apps"):
        for number in range(3):
            key = f"{wave}{number}"
            run.task(key, f"wave task {number}", "app_scale", now, play="apps", file=WAVE, item=wave)
            run.result(key, "k8s", "changed" if number == 0 else "ok", now + 1)
            now += 1
    run.task("a3", "Say what was done", "app_scale", now, play="apps")
    lines = ["Stopped 2 apps, to come back with the replicas in brackets.", "media/sonarr (1)", "then media/db (1)"]
    run.note(run.find("a3"), lines[0], lines)
    run.result("a3", "k8s", "ok", now + 0.1)
    return now + 0.2


def test_a_stage_only_an_option_draws_is_left_out_without_it():
    assert [s.label for s in suspend().stages] == ["apps", "Home Assistant"]
    assert [s.label for s in suspend({"cluster_vms": True}).stages] == ["apps", "Home Assistant", "cluster VMs"]
    run = suspend({"cluster_vms": "false"})
    assert [s.label for s in run.stages] == ["apps", "Home Assistant"]
    # Its play's tasks are the log's alone.
    run.play_start("vms", 0.0)
    assert run.task("v0", "Leave the cluster's VMs alone without --all", None, 0.0, play="vms") is None


@pytest.mark.parametrize("cols", [100, 80])
def test_a_looped_includes_items_are_steps_of_their_own(cols):
    run = suspend()
    apps = run.stages[0]
    assert [row.label for row in apps.rows()] == ["plan", "apps"]
    # The include's file counted at the start, in the step its tasks are: the waves'.
    assert apps.steps[1].expected == 3 and apps.expected == 8
    now = waves(run, 0.0)
    assert [row.label for row in apps.rows()] == ["plan", "apps", "databases"]
    assert [part.expected for part in apps.steps[1].parts] == [3, 3] and apps.expected == 11
    assert [len(part.tasks) for part in apps.steps[1].parts] == [3, 3]
    # The report runs in the plan's step after both waves, which takes no more than its own tasks' time.
    assert apps.steps[0].seconds(now) == pytest.approx(3.6, abs=0.01)
    run.end(now)
    lines = frame(run, now, cols)
    assert all(len(line) <= cols for line in lines), lines
    piped = run.ended_lines(now, cols)
    assert [line.split("  ")[0] for line in piped[:3]] == ["✓ apps · plan", "✓ apps · apps", "✓ apps · databases"]
    summary = [plainly for _c, plainly in run.summary(now, cols)]
    said = " ".join(" ".join(summary).split())
    assert "plan 3.6 s · apps 3.0 s, 1 changed · databases 3.0 s, 1 changed" in said, summary
    # The declared report, whole, under its stage.
    at = summary.index(next(line for line in summary if line.startswith("APPS")))
    assert summary[at : at + 3] == [
        "APPS            Stopped 2 apps, to come back with the replicas in brackets.",
        "                media/sonarr (1)",
        "                then media/db (1)",
    ]


def test_a_running_item_is_drawn_with_its_tasks_and_a_failure_says_which():
    run = suspend()
    run.play_start("apps", 0.0)
    run.task("a2", "Include", "app_scale", 0.0, play="apps")
    run.result("a2", "k8s", "ok", 0.1)
    run.include("a2", 3, "apps")
    run.include("a2", 3, "late apps")
    run.task("w0", "Patch each app", "app_scale", 1.0, play="apps", file=WAVE, item="apps")
    lines = frame(run, 2.0, 100)
    assert any(line.startswith("     ⠋ apps") for line in lines), lines
    assert any(line.startswith("     ○ databases") for line in lines), lines
    assert any("⠋ Patch each app" in line for line in lines), lines
    run.result("w0", "k8s", "failed", 2.0)
    assert run.where(run.find("w0")) == "apps · apps"


def test_a_play_that_ends_its_host_before_its_work_leaves_its_stage_out_of_the_run():
    run = suspend()
    now = waves(run, 0.0)
    run.play_start("haos", now)
    run.task("h0", "Leave Home Assistant alone when --only names others", None, now, play="haos", ends=True)
    # Ending its host, the task says nothing: the next event closes it.
    run.end(now + 1)
    assert run.stages[1].state(True) == "none"
    assert f" {view.DASH} Home Assistant not in this run" in frame(run, now + 1, 100)
    summary = [plainly for _c, plainly in run.summary(now + 1, 100)]
    assert f" {view.DASH} Home Assistant not in this run" in summary
    assert any(
        line.startswith(f"{view.DASH} Home Assistant") and "not in this run" in line
        for line in run.ended_lines(now + 1)
    )
    # A report its play printed just before is the reason, and no report of the summary's.
    run = suspend()
    run.play_start("haos", 0.0)
    run.task("h0", "Say the VM comes back later", None, 0.0, play="haos")
    run.note(run.find("h0"), "The VM is not up yet; a real run starts it.")
    run.result("h0", "pve", "ok", 0.1)
    run.task("h1", "Leave it for the real run", None, 0.2, play="haos", ends=True)
    run.end(1.0)
    assert run.stages[1].reason == "The VM is not up yet; a real run starts it." and run.notes == []
    assert f" {view.DASH} Home Assistant The VM is not up yet; a real run starts it." in frame(run, 1.0, 100)
    # One that is skipped, or ends its host only after its absent task said why, leaves the stage as it is.
    run = suspend()
    run.play_start("haos", 0.0)
    run.task("h0", "Leave Home Assistant alone when --only names others", None, 0.0, play="haos", ends=True)
    run.result("h0", "pve", "skipped", 0.1)
    run.task("h1", "Shut Home Assistant's VM down", "proxmox_vm", 0.2, play="haos")
    run.end(1.0)
    assert run.stages[1].state(True) == "done"


def test_a_stage_whose_play_comes_later_waits_for_it():
    resume = view.recipes.recipe_for("resume.yml")
    plays = [
        ("refuse", "/r/resume.yml:9", [("r0", None, {})], REFUSE),
        ("vms", "/r/resume.yml:20", [("v0", "proxmox_vm", {})], "Start the cluster's VMs"),
        ("apps", "/r/resume.yml:50", [("a0", "app_scale", MAIN)], "Bring the apps back up"),
    ]
    run = view.Run("resume", 0.0, started_at=START, recipe=resume, options={"cluster_vms": True})
    run.plays(plays)
    assert [s.label for s in run.stages] == ["cluster VMs", "apps"]
    run.play_start("refuse", 0.0)
    run.task("r0", "Refuse", None, 0.0, play="refuse")
    run.result("r0", "localhost", "ok", 0.5)
    run.play_start("vms", 1.0)
    run.task("v0", "Start each VM", "proxmox_vm", 1.0, play="vms")
    vms, apps = run.stages
    assert apps.state(False) == "todo" and vms.state(False) == "running"
    assert any(line.startswith(" ○ apps") for line in frame(run, 50.0, 100))
    run.result("v0", "pve", "changed", 60.0)
    run.play_start("apps", 60.0)
    run.task("a0", "Read", "app_scale", 61.0, play="apps")
    run.result("a0", "k8s", "ok", 62.0)
    assert vms.state(False) == "done" and apps.state(False) == "running"
    run.end(62.0)
    # Its time is its own tasks', never the VMs' minute between them.
    assert apps.seconds(62.0) == pytest.approx(1.5)


def test_a_skipped_include_takes_back_what_it_was_counted_with():
    run = suspend(check=True)
    run.play_start("apps", 0.0)
    run.task("a2", "Stop or start the apps, a wave at a time", "app_scale", 0.0, play="apps")
    run.result("a2", "k8s", "skipped", 0.1)
    run.end(0.2)
    assert run.stages[0].steps[1].state(True) == "none"
    assert run.stages[0].expected == 5


# talos-upgrade.yml's plays: the refusal, then the play whose one task includes the talos role's upgrade entry point,
# listed as live_ansible lists a literal include_role's file: each task's name and file.
UPGRADE = view.recipes.recipe_for("talos-upgrade.yml")
TASKS = "/r/roles/talos/tasks"
LISTED = [
    ("Check the role's inputs", f"{TASKS}/validate.yml"),
    ("Make a scratch directory", f"{TASKS}/prepare.yml"),
    ("Refuse to upgrade a cluster that is not all up", f"{TASKS}/upgrade.yml"),
    ("Show the plan", f"{TASKS}/upgrade.yml"),
    ("Take an etcd snapshot", f"{TASKS}/snapshot.yml"),
    ("Upgrade Talos, control plane first", f"{TASKS}/upgrade.yml"),
    ("Upgrade Kubernetes", f"{TASKS}/upgrade.yml"),
    ("Wait for the cluster to be healthy", f"{TASKS}/upgrade.yml"),
    ("Remove the scratch directory", f"{TASKS}/upgrade.yml"),
]
UPGRADE_PLAYS = [
    ("refuse", "/r/talos-upgrade.yml:9", [("r0", None, {"file": "/r/talos-upgrade.yml"})], REFUSE),
    (
        "up",
        "/r/talos-upgrade.yml:21",
        [
            ("u0", None, {"file": "/r/talos-upgrade.yml", "name": "Refuse an incomplete Kubernetes section"}),
            (
                "u1",
                None,
                {
                    "file": "/r/talos-upgrade.yml",
                    "name": "Upgrade the nodes, then Kubernetes",
                    "include": len(LISTED),
                    "included": f"{TASKS}/upgrade.yml",
                    "listed": LISTED,
                    "role": "talos",
                },
            ),
        ],
        "Upgrade the cluster to the pinned versions",
    ),
]
NODES = ("homeserver-cp-1", "homeserver-worker-1")


def test_an_upgrade_is_its_plan_its_snapshot_a_step_per_node_and_kubernetes():
    run = view.Run("talos-upgrade", 0.0, started_at=START, recipe=UPGRADE)
    run.plays(UPGRADE_PLAYS)
    steps = run.stages[0].steps
    assert [step.label for step in steps] == ["plan", "etcd snapshot", "nodes", "Kubernetes"]
    # The include's tasks counted at the start, each in its own step; the nodes' file comes with its include.
    assert [step.expected for step in steps] == [3 + 6, 1, 0, 2]
    assert steps[2].state(False) == "todo"
    assert any(line.startswith(" ○ nodes ") and line.endswith("a step each") for line in frame(run, 0.0, 100))
    now = 0.0
    run.play_start("refuse", now)
    run.task("r0", REFUSE, None, now, play="refuse")
    run.result("r0", "localhost", "ok", now + 0.1)
    run.play_start("up", 0.2)
    for key in ("u0", "u1"):
        run.task(key, key, None, now + 0.3, play="up")
        run.result(key, "k8s", "ok", now + 0.4)
    run.include("u1", len(LISTED), None, f"{TASKS}/upgrade.yml", "talos")
    now = 1.0

    def task(key, name, file, item=None, outcome="ok", seconds=1.0):
        nonlocal now
        run.task(key, name, "talos", now, play="up", file=file, item=item)
        run.result(key, "k8s", outcome, now + seconds)
        now += seconds

    for number, (name, file) in enumerate(LISTED[:6]):
        task(f"t{number}", name, file)
        if name == "Show the plan":
            lines = ["To the pins: 2 nodes, the control plane first, then Kubernetes."]
            lines += [f"{node}: Talos v1.14.2 -> v1.14.4" for node in NODES] + ["Kubernetes: 1.37.1 -> 1.37.2"]
            run.note(run.find(f"t{number}"), lines[0], lines)
    for node in NODES:
        run.include("t5", 2, node, f"{TASKS}/upgrade-node.yml", "talos")
    assert [row.label for row in run.stages[0].rows()] == ["plan", "etcd snapshot", *NODES, "Kubernetes"]
    for node in NODES:
        task(f"{node}-up", "Upgrade Talos on the node", f"{TASKS}/upgrade-node.yml", node, "changed", 60)
        task(
            f"{node}-wait", "Wait for the cluster to be healthy before the next node", f"{TASKS}/upgrade-node.yml", node
        )
    lines = frame(run, now, 100)
    # The label column fits the longest node's name, so every bar starts in one column.
    bars = {line.index("█") for line in lines if "█" in line}
    assert len(bars) == 1 and run.label == len("homeserver-worker-1") + 1, lines
    for number, (name, file) in enumerate(LISTED[6:], 6):
        task(f"t{number}", name, file, outcome="changed" if name == "Upgrade Kubernetes" else "ok")
    run.end(now)
    kubernetes = run.stages[0].steps[3]
    assert [t.name for t in kubernetes.tasks] == ["Upgrade Kubernetes", "Wait for the cluster to be healthy"]
    summary = [plainly for _c, plainly in run.summary(now, 100)]
    assert summary[2].startswith("STEPS" + " " * 21 + "TIME"), summary[2]
    rows = [line.split()[1] for line in summary[3:8]]
    assert rows == ["plan", "etcd", *NODES, "Kubernetes"], summary
    assert all(" 2 " in line for line in summary[5:7]), "each node's two tasks"
    at = summary.index(next(line for line in summary if line.startswith("CLUSTER")))
    assert summary[at].endswith("To the pins: 2 nodes, the control plane first, then Kubernetes.")
    assert summary[at + 3] == " " * 16 + "Kubernetes: 1.37.1 -> 1.37.2", summary[at:]


def test_an_include_no_step_names_is_its_first_tasks_and_its_plays_own_tasks_beside_it():
    run = view.Run("talos-upgrade", 0.0, started_at=START, recipe=UPGRADE)
    run.plays(UPGRADE_PLAYS)
    assert run.plan["u0"] == (0, 0) and run.plan["u1"] == (0, 0)


def test_task_list_names_each_task_and_its_file_as_task_file_sees_it(tmp_path):
    (tmp_path / "upgrade.yml").write_text(
        "- name: upgrade | Check\n  ansible.builtin.import_tasks: validate.yml\n"
        "- name: upgrade | Move\n  block:\n    - name: upgrade | Plan\n      ansible.builtin.debug: {msg: x}\n"
        "    - name: upgrade | Each node\n      ansible.builtin.include_tasks: node.yml\n"
    )
    (tmp_path / "validate.yml").write_text(
        "- name: validate | Inputs\n  ansible.builtin.assert: {that: true}\n"
        "- name: validate | More\n  ansible.builtin.import_tasks: more.yml\n"
    )
    (tmp_path / "more.yml").write_text("- name: more | Deeper\n  ansible.builtin.assert: {that: true}\n")
    top, validate = str(tmp_path / "upgrade.yml"), str(tmp_path / "validate.yml")
    # An include_role's file: a file it imports is that file's own, and one that file imports stays the first's.
    assert view.task_list(top, imports_own=True) == [
        ("Inputs", validate),
        ("Deeper", validate),
        ("Plan", top),
        ("Each node", top),
    ]
    # An include_tasks's file: every task is its file's.
    assert {file for _name, file in view.task_list(top)} == {top}
    assert view.count_tasks(top) == 4
