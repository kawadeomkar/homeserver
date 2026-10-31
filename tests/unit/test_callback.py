"""The stdout callback ansible.cfg names, callback_plugins/homeserver.py: at -v the default callback's output and log,
byte for byte, then NEXT STEPS from what the run recorded under homeserver_next; at verbosity 0, off a terminal, a line
per step, the reports and the summary, with the default's stream in the log.

A subclass of the default callback without its documentation fragments has no options: every hook fails on its first
get_option, Ansible only warns, and every result disappears from the screen and the log in a run that exits 0. Each
run here is a real ansible-playbook run with each callback, compared line by line, so that cannot pass unseen.
"""

import importlib.util
import os
import pty
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = str(Path(sys.executable).parent / "ansible-playbook")
# The prefix Ansible writes before each line of its log: when, which process, which user.
LOG_PREFIX = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} p=\d+ u=\S+ n=ansible \w*\| ?", re.MULTILINE)
# A module's temporary directory, which -v shows in a copy's result and which differs from run to run.
TEMPORARY = re.compile(r"ansible-tmp-[\d.]+-\d+-\d+")

PLAY = """\
- name: Every kind of result
  hosts: localhost
  gather_facts: false
  tasks:
    - name: A message of two lines
      ansible.builtin.debug:
        msg: [the first line, the second line]
    - name: A change
      ansible.builtin.set_fact:
        changed_on_purpose: true
      changed_when: true
    - name: A skip
      ansible.builtin.debug:
        msg: never shown
      when: false
    - name: A loop
      ansible.builtin.debug:
        msg: "{{ item }}"
      loop: [one, two]
    - name: A failure, ignored
      ansible.builtin.fail:
        msg: failed on purpose
      ignore_errors: true
    - name: A file changed, with its diff
      ansible.builtin.copy:
        content: "new\\n"
        dest: "{{ scratch }}/file"
        mode: "0600"
      diff: true
    - name: Something left to do
      ansible.builtin.set_stats:
        data:
          homeserver_next:
            reboot:
              host: {text: the host waits for a reboot, command: reboot it at a quiet time}
            restart:
              node: ""
    - name: Something left to do, said again, and something more
      ansible.builtin.set_stats:
        data:
          homeserver_next:
            reboot:
              host: {text: the host waits for a reboot, command: reboot it at a quiet time}
            tailscale:
              route: {text: a route is advertised but not approved}
"""


def callback_module():
    spec = importlib.util.spec_from_file_location("homeserver_callback", ROOT / "callback_plugins" / "homeserver.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def play(tmp_path, callback, *args, books=("play.yml",), terminal=False):
    """The playbook with `callback`, from a file in the state it starts in: its output and its log without the
    prefixes. books, the playbooks run, each the same playbook; terminal, its output on a pseudo-terminal that nobody
    gave a size, as pre-commit runs its hooks on."""
    (tmp_path / "file").write_text("old\n")
    for book in books:
        (tmp_path / book).write_text(PLAY)
    config = tmp_path / "ansible.cfg"
    config.write_text(
        "[defaults]\nretry_files_enabled = false\ncallback_result_format = yaml\n"
        f"callback_plugins = {ROOT / 'callback_plugins'}\n"
    )
    log = tmp_path / f"{callback}.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")} | {
        "ANSIBLE_CONFIG": str(config),
        "ANSIBLE_STDOUT_CALLBACK": callback,
        "ANSIBLE_LOG_PATH": str(log),
        "ANSIBLE_NOCOLOR": "1",
    }
    command = [PLAYBOOK, "-i", "localhost,", "-c", "local", *books, "-e", f"scratch={tmp_path}"]
    command += ["-e", f"ansible_python_interpreter={sys.executable}", *args]
    if terminal:
        returncode, stdout = _on_a_terminal(command, env | {"TERM": "xterm-256color"}, tmp_path)
    else:
        proc = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        returncode, stdout = proc.returncode, proc.stdout
    assert returncode == 0, stdout
    return TEMPORARY.sub("ansible-tmp-X", stdout), TEMPORARY.sub("ansible-tmp-X", LOG_PREFIX.sub("", log.read_text()))


def _on_a_terminal(command, env, cwd):
    """The command's exit code and what it wrote, with its output on a pseudo-terminal of the test's own, unsized."""
    master, terminal = pty.openpty()
    try:
        proc = subprocess.Popen(command, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=terminal, stderr=terminal)
        os.close(terminal)
        terminal = None
        written = b""
        while True:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            written += chunk
        return proc.wait(timeout=120), written.decode().replace("\r\n", "\n")
    finally:
        os.close(master)
        if terminal is not None:
            os.close(terminal)


NEXT_STEPS = """
NEXT STEPS  2 to do
 ● reboot      the host waits for a reboot
               → reboot it at a quiet time
 ● tailscale   a route is advertised but not approved
"""


def in_order(lines, within):
    """Whether lines are within, in their order, others between them."""
    rest = iter(within)
    return all(any(line == other for other in rest) for line in lines)


def test_at_v_the_output_and_the_log_are_the_default_callbacks_then_next_steps(tmp_path):
    default_out, default_log = play(tmp_path, "default", "-v")
    out, log = play(tmp_path, "homeserver", "-v")
    assert "the second line" in default_out and "failed on purpose" in default_out and "+new" in default_out
    assert out == default_out + NEXT_STEPS, out
    assert log.startswith(default_log), "the log differs from the default callback's"
    assert [line for line in log[len(default_log) :].splitlines() if line] == NEXT_STEPS.strip().splitlines()


def test_next_steps_lists_each_record_left_once_in_the_order_made():
    module = callback_module()
    stats = {
        "homeserver_next": {
            "restart": {
                "homeserver-cp-1": {"text": "300 homeserver-cp-1 waits for a restart", "command": "qm reboot 300"}
            },
            "reboot": {"host": ""},
            "talos": {
                "pins": {"text": " the pins\n wait ", "command": ""},
                "manifests": "",
                "odd": ["not", "a record"],
            },
            "tailscale": "not a mapping",
        },
        "something_else": {"x": {"text": "not ours"}},
    }
    assert module.next_steps(stats) == [
        ("restart", "300 homeserver-cp-1 waits for a restart", "qm reboot 300"),
        ("talos", "the pins wait", ""),
    ]
    # What a machine answered, quoted in a record, reaches the terminal without a sequence it would act on.
    quoted = {
        "homeserver_next": {
            "backup": {"x": {"text": "\x1b]52;c;ZXZpbA==\x07a job\x1b[31m waits\x9b2J", "command": "a\x07b"}}
        }
    }
    assert module.next_steps(quoted) == [("backup", "a job waits", "ab")]
    assert module.next_steps({}) == [] and module.next_steps({"homeserver_next": "nothing"}) == []
    lines = module.next_steps_lines(module.next_steps(stats))
    assert [line.text(False) for line in lines] == [
        "NEXT STEPS  2 to do",
        " ● restart     300 homeserver-cp-1 waits for a restart",
        "               → qm reboot 300",
        " ● talos       the pins wait",
    ]


def test_off_a_terminal_a_run_is_lines_its_reports_and_a_summary_and_its_log_keeps_the_stream(tmp_path):
    _default_out, default_log = play(tmp_path, "default")
    out, log = play(tmp_path, "homeserver")
    assert "TASK [" not in out and "PLAY RECAP" not in out, out
    lines = out.splitlines()
    # A playbook no declaration names: a stage per play, its own tasks one step.
    assert any(line.startswith("✓ tasks") and "8 tasks" in line for line in lines), out
    assert "  tasks          the first line" in lines and " " * 17 + "the second line" in lines, out
    assert "+new" not in out, "a real run's diff goes to the log alone"
    assert lines[-1] == "exit 0 · for the whole picture: just status", out
    assert NEXT_STEPS.strip().splitlines()[0] in lines
    # The log: the default's every line, in order, then the summary.
    assert in_order([line for line in default_log.splitlines() if line], log.splitlines()), log
    assert "exit 0 · for the whole picture: just status" in log and "  tasks          the first line" in log


def test_on_a_terminal_nobody_sized_a_run_is_lines_as_off_a_terminal(tmp_path):
    """Nothing can be drawn at 0 by 0, and a summary cut to no width at all was nothing but "…"."""
    out, _log = play(tmp_path, "homeserver", terminal=True)
    lines = out.splitlines()
    assert "  tasks          the first line" in lines and "…" not in lines, out
    assert "\x1b[" not in out and "\x1b]" not in out, "a region was drawn"
    assert lines[-1] == "exit 0 · for the whole picture: just status", out


def test_a_run_of_several_playbooks_is_the_stream_with_next_steps_after_each_recap(tmp_path):
    """ansible-playbook runs each playbook in turn, a recap after each: drawn, the first's engine would route the
    second's words, its failures among them, to the log alone. The records add up from one playbook to the next, so
    each recap's NEXT STEPS holds every one made so far."""
    books = ("play.yml", "again.yml")
    default_out, _default_log = play(tmp_path, "default", books=books)
    out, _log = play(tmp_path, "homeserver", books=books)
    assert default_out.count("PLAY RECAP") == 2, default_out
    assert out.count(NEXT_STEPS) == 2 and out.replace(NEXT_STEPS, "") == default_out, out


def test_a_failure_prints_whole_with_where_it_is_and_its_log_line(tmp_path):
    (tmp_path / "play.yml").write_text(
        "- name: A refusal\n  hosts: localhost\n  gather_facts: false\n  tasks:\n"
        "    - name: Refuse\n      ansible.builtin.assert:\n        that: false\n"
        "        fail_msg: the settings are refused, and this says why\n"
    )
    config = tmp_path / "ansible.cfg"
    config.write_text(f"[defaults]\nretry_files_enabled = false\ncallback_plugins = {ROOT / 'callback_plugins'}\n")
    log = tmp_path / "run.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")} | {
        "ANSIBLE_CONFIG": str(config),
        "ANSIBLE_STDOUT_CALLBACK": "homeserver",
        "ANSIBLE_LOG_PATH": str(log),
        "ANSIBLE_NOCOLOR": "1",
    }
    command = [PLAYBOOK, "-i", "localhost,", "-c", "local", "play.yml"]
    command += ["-e", f"ansible_python_interpreter={sys.executable}"]
    proc = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    out = proc.stdout.splitlines()
    head = next(line for line in out if line.startswith("✗ "))
    assert head.startswith("✗ tasks · localhost: Refuse (") and head.endswith("refused"), head
    assert "    the settings are refused, and this says why" in out and "assertion" not in proc.stdout
    number = next(i for i, line in enumerate(log.read_text().splitlines(), 1) if "fatal: [localhost]" in line)
    assert any(line.startswith("    play.yml:5 · log line ") and line.endswith(str(number)) for line in out), out
    assert "failed after" in proc.stdout and out[-1] == "exit 2 · for the whole picture: just status"


def test_a_stage_is_absent_only_once_its_absent_task_has_run(tmp_path):
    """deploy's declaration names truenas.yml's "Say why the NAS is skipped": Ansible announces it before it checks its
    when, so only its running, never its start, says the setup has no NAS."""
    config = tmp_path / "ansible.cfg"
    config.write_text(f"[defaults]\nretry_files_enabled = false\ncallback_plugins = {ROOT / 'callback_plugins'}\n")
    dash = "\u2013"
    for absent in (False, True):
        (tmp_path / "truenas.yml").write_text(
            "- name: Configure the TrueNAS storage server\n  hosts: localhost\n  gather_facts: false\n  tasks:\n"
            "    - name: Say why the NAS is skipped\n      ansible.builtin.debug:\n"
            "        msg: There is no NAS in this setup. local.yml gives it no address.\n"
            f"      when: {str(absent).lower()}\n"
            "    - name: Report the NAS's state\n      ansible.builtin.debug:\n"
            "        msg: TrueNAS 25.10.7 at the NAS\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")} | {
            "ANSIBLE_CONFIG": str(config),
            "ANSIBLE_STDOUT_CALLBACK": "homeserver",
            "ANSIBLE_LOG_PATH": str(tmp_path / f"{absent}.log"),
            "ANSIBLE_NOCOLOR": "1",
        }
        command = [PLAYBOOK, "-i", "localhost,", "-c", "local", "truenas.yml"]
        command += ["-e", f"ansible_python_interpreter={sys.executable}"]
        proc = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        lines = proc.stdout.splitlines()
        drawn_absent = [line for line in lines if line.startswith(f"{dash} NAS") or line.startswith(f" {dash} NAS")]
        if absent:
            assert drawn_absent and all("There is no NAS in this setup" in line for line in drawn_absent), proc.stdout
        else:
            assert not drawn_absent and "There is no NAS" not in proc.stdout, proc.stdout
            assert "  NAS            TrueNAS 25.10.7 at the NAS" in lines, proc.stdout


SUSPEND = """\
- name: Refuse a cluster without the kubernetes inventory group
  hosts: localhost
  gather_facts: false
  tasks:
    - name: Refuse a cluster without the kubernetes inventory group
      ansible.builtin.assert: {that: true, quiet: true}
- name: Spin the apps down
  hosts: localhost
  gather_facts: false
  roles: [app_scale]
- name: Shut Home Assistant's VM down
  hosts: localhost
  gather_facts: false
  pre_tasks:
    - name: Leave Home Assistant alone when --only names others
      ansible.builtin.meta: end_host
      when: only | default('') | length > 0
  tasks:
    - name: Shut it down
      ansible.builtin.import_role: {name: proxmox_vm, tasks_from: power}
- name: Shut the cluster's VMs down
  hosts: localhost
  gather_facts: false
  pre_tasks:
    - name: Leave the cluster's VMs alone without --all
      ansible.builtin.meta: end_host
      when: not (cluster_vms | default(false) | bool)
  tasks:
    - name: Shut them down
      ansible.builtin.import_role: {name: proxmox_vm, tasks_from: power}
"""
APP_SCALE = """\
- name: Work out what to do
  ansible.builtin.set_fact:
    plan: {waves: [[{name: sonarr, late: false}, {name: jellyfin, late: false}], [{name: db, late: true}]]}
- name: Stop or start the apps, a wave at a time
  ansible.builtin.include_tasks: wave.yml
  loop: "{{ plan.waves }}"
  loop_control:
    loop_var: wave
    label: "{{ 'late apps' if wave[0].late else 'apps' }}"
- name: Say what was done
  ansible.builtin.debug:
    msg: ["Stopped 3 apps, to come back with the replicas in brackets.", sonarr (1), jellyfin (1), then db (1)]
"""
WAVE = """\
- name: wave | Patch each app
  ansible.builtin.set_fact: {patched: "{{ item.name }}"}
  changed_when: true
  loop: "{{ wave }}"
- name: wave | Wait for each app's pods to stop
  ansible.builtin.set_fact: {waited: "{{ item.name }}"}
  loop: "{{ wave }}"
"""


def test_suspend_is_drawn_by_its_declaration_its_waves_steps_and_its_options_in_its_title(tmp_path):
    """suspend.yml by its declaration (filter_plugins/live_recipes.py): the apps' waves, each a step, by their loop
    labels, databases for the late apps; the report whole in the summary; Home Assistant's play, which --only ends
    before its work, not in this run; the cluster's VMs not drawn without --all; and the command as typed."""
    (tmp_path / "suspend.yml").write_text(SUSPEND)
    for role, files in (("app_scale", {"main": APP_SCALE, "wave": WAVE}), ("proxmox_vm", {"power": "[]\n"})):
        (tmp_path / "roles" / role / "tasks").mkdir(parents=True)
        for name, body in files.items():
            (tmp_path / "roles" / role / "tasks" / f"{name}.yml").write_text(body)
    (tmp_path / "roles" / "proxmox_vm" / "tasks" / "power.yml").write_text(
        "- name: power | Say what was done\n  ansible.builtin.debug: {msg: Shut down VM 310.}\n"
    )
    config = tmp_path / "ansible.cfg"
    config.write_text(f"[defaults]\nretry_files_enabled = false\ncallback_plugins = {ROOT / 'callback_plugins'}\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")} | {
        "ANSIBLE_CONFIG": str(config),
        "ANSIBLE_STDOUT_CALLBACK": "homeserver",
        "ANSIBLE_LOG_PATH": str(tmp_path / "run.log"),
        "ANSIBLE_NOCOLOR": "1",
    }
    dash = "\u2013"
    for args, title in (
        (["-e", '{"only": "sonarr, db"}'], "suspend --only sonarr, db"),
        (["--check", "-e", '{"cluster_vms": true}'], "suspend --check --all"),
    ):
        command = [PLAYBOOK, "-i", "localhost,", "-c", "local", "suspend.yml", *args]
        command += ["-e", f"ansible_python_interpreter={sys.executable}"]
        proc = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        lines = proc.stdout.splitlines()
        assert any(line.startswith(f"homeserver  {title} · ") for line in lines), proc.stdout
        steps = [line.split("  ")[0] for line in lines if line.startswith("✓ apps · ")]
        assert steps == ["✓ apps · plan", "✓ apps · apps", "✓ apps · databases"], proc.stdout
        assert "APPS            Stopped 3 apps, to come back with the replicas in brackets." in lines, proc.stdout
        assert "                then db (1)" in lines, proc.stdout
        if "--all" in title:
            assert any(line.startswith(" ✓ cluster VMs") for line in lines), proc.stdout
            assert any(line.startswith(" ✓ Home Assistant") for line in lines), proc.stdout
        else:
            assert not any("cluster VMs" in line for line in lines), proc.stdout
            assert any(line.startswith(f"{dash} Home Assistant") and "not in this run" in line for line in lines)
            assert f" {dash} Home Assistant not in this run" in lines, proc.stdout


def test_a_report_that_changes_something_is_counted_as_a_change(tmp_path):
    """A report whose changed_when holds, as a dry run's plan is: the default callback's hook strips a debug's
    result to its message in place, so the engine reads changed before it."""
    (tmp_path / "play.yml").write_text(
        "- name: A plan\n  hosts: localhost\n  gather_facts: false\n  tasks:\n"
        "    - name: Say what would change\n      ansible.builtin.debug:\n        msg: Would add a storage.\n"
        "      changed_when: true\n"
    )
    config = tmp_path / "ansible.cfg"
    config.write_text(f"[defaults]\nretry_files_enabled = false\ncallback_plugins = {ROOT / 'callback_plugins'}\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")} | {
        "ANSIBLE_CONFIG": str(config),
        "ANSIBLE_STDOUT_CALLBACK": "homeserver",
        "ANSIBLE_LOG_PATH": str(tmp_path / "run.log"),
        "ANSIBLE_NOCOLOR": "1",
    }
    command = [PLAYBOOK, "-i", "localhost,", "-c", "local", "play.yml", "--check"]
    command += ["-e", f"ansible_python_interpreter={sys.executable}"]
    proc = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    assert "  tasks          Would add a storage." in lines, proc.stdout
    assert lines[-2].startswith("1 task · 1 would change · 0 failed"), proc.stdout
