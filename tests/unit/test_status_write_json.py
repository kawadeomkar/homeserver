"""roles/status's action plugin status_write_json: one of just status's files written as JSON on the controller.

The integration tests run it inside ansible-playbook (test_status.py reads the report every run writes); these cover
what they never see: the file's and the directories' modes, the same bytes the copy module wrote with to_nice_json, a
dry run that writes nothing, a file already there that is not JSON or not text, and a failed write that leaves the old
file whole.
"""

import importlib.util
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from ansible.errors import AnsibleActionFail
from ansible.plugins.filter.core import to_nice_json

PLUGIN = Path(__file__).resolve().parents[2] / "roles" / "status" / "action_plugins" / "status_write_json.py"
_spec = importlib.util.spec_from_file_location("status_write_json", PLUGIN)
status_write_json = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(status_write_json)

REPORT = {
    "exit": 1,
    "setup": {"nas": False, "cluster": True},
    "attention": [{"subject": "upgrades", "text": "2 package upgrades wait", "command": "just deploy proxmox"}],
    "read_at": "2026-11-18 21:08 PST",
    "vms": [{"vmid": 310, "name": "homeassistant", "cpu_24h": [0.01, 0.5, None], "memory": {"used": 2, "total": 4}}],
    "note": "na\u00efve, text past ASCII",
}


def run(path, content=REPORT, check_mode=False, **more):
    """The plugin's run() as ansible-playbook calls it, with the task's arguments."""
    task = SimpleNamespace(
        args={"path": str(path), "content": content, **more},
        async_val=0,
        check_mode=check_mode,
        action="status_write_json",
    )
    connection = SimpleNamespace(_shell=SimpleNamespace(tmpdir="unused"))
    plugin = status_write_json.ActionModule(task, connection, None, None, None)
    return plugin.run(task_vars={})


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_the_file_holds_what_the_copy_module_wrote(tmp_path):
    """The bytes copy wrote from to_nice_json and a newline: just status itself reads it for its exit code, and --json
    prints it. Each directory it makes is 0700, not only the last."""
    path = tmp_path / ".cache" / "status" / "report.json"
    result = run(path)
    assert result["changed"] is True and result["path"] == str(path)
    assert path.read_text() == to_nice_json(REPORT) + "\n"
    assert mode(path) == 0o600 and mode(path.parent) == 0o700 and mode(path.parent.parent) == 0o700
    # The same content again changes nothing, and nothing is left beside it.
    assert run(path)["changed"] is False
    assert sorted(p.name for p in path.parent.iterdir()) == ["report.json"]


def test_an_existing_directory_is_left_as_it_is(tmp_path):
    os.chmod(tmp_path, 0o750)
    run(tmp_path / "report.json")
    assert mode(tmp_path) == 0o750 and mode(tmp_path / "report.json") == 0o600


def test_a_file_already_there_that_is_not_text_is_replaced_not_a_failure(tmp_path):
    path = tmp_path / "upgrades.json"
    path.write_bytes(b"\xff\xfe not UTF-8")
    assert run(path)["changed"] is True
    assert path.read_text() == to_nice_json(REPORT) + "\n"


def test_a_dry_run_writes_nothing_and_says_whether_it_would(tmp_path):
    path = tmp_path / "report.json"
    assert run(path, check_mode=True)["changed"] is True and not path.exists()
    run(path)
    assert run(path, check_mode=True)["changed"] is False


def test_a_failed_write_leaves_the_old_file_whole(tmp_path, monkeypatch):
    path = tmp_path / "report.json"
    run(path)
    before = path.read_text()

    def refuse(*_):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(status_write_json.os, "replace", refuse)
    with pytest.raises(AnsibleActionFail, match=r"could not write .*No space left on device"):
        run(path, content={"exit": 0})
    assert path.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["report.json"]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"path": "relative/report.json"}, "absolute path"),
        ({"content": ["not", "a", "mapping"]}, "mapping"),
        ({"mode": "0644"}, "Invalid options"),
    ],
)
def test_what_it_refuses(tmp_path, args, message):
    path = args.pop("path", tmp_path / "report.json")
    content = args.pop("content", REPORT)
    with pytest.raises(AnsibleActionFail, match=message):
        run(path, content=content, **args)
    assert not (tmp_path / "report.json").exists()
