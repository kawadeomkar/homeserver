"""The stdout callback ansible.cfg names, callback_plugins/homeserver.py: the default callback's output and log, byte
for byte, then NEXT STEPS from what the run recorded under homeserver_next.

A subclass of the default callback without its documentation fragments has no options: every hook fails on its first
get_option, Ansible only warns, and every result disappears from the screen and the log in a run that exits 0. Each
run here is a real ansible-playbook run with each callback, compared line by line, so that cannot pass unseen.
"""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = str(Path(sys.executable).parent / "ansible-playbook")
# The prefix Ansible writes before each line of its log: when, which process, which user.
LOG_PREFIX = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} p=\d+ u=\S+ n=ansible \w*\| ?", re.MULTILINE)

PLAY = """\
- name: Every kind of result
  hosts: localhost
  gather_facts: false
  tasks:
    - name: A message of two lines
      ansible.builtin.debug:
        msg: [the first line, the second line]
    - name: A change
      ansible.builtin.command: "true"
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


def play(tmp_path, callback):
    """The playbook with `callback`, from a file in the state it starts in: its output and its log without the
    prefixes."""
    (tmp_path / "file").write_text("old\n")
    (tmp_path / "play.yml").write_text(PLAY)
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
    command = [PLAYBOOK, "-i", "localhost,", "-c", "local", "play.yml", "-e", f"scratch={tmp_path}"]
    command += ["-e", f"ansible_python_interpreter={sys.executable}"]
    proc = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout, LOG_PREFIX.sub("", log.read_text())


NEXT_STEPS = """
NEXT STEPS  2 to do
 ● reboot      the host waits for a reboot
               → reboot it at a quiet time
 ● tailscale   a route is advertised but not approved
"""


def test_the_output_and_the_log_are_the_default_callbacks_then_next_steps(tmp_path):
    default_out, default_log = play(tmp_path, "default")
    out, log = play(tmp_path, "homeserver")
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
