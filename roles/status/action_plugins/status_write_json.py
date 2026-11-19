"""status_write_json: one of just status's files written as JSON in the controller's own process (library/ holds its
documentation). ansible.builtin.copy of the same content runs two modules on the controller, a stat and a copy, which
took 0.4 s of every just status (2026-11-12)."""

import json
import os
import tempfile

from ansible.errors import AnsibleActionFail
from ansible.plugins.action import ActionBase


def make_directories(directory):
    """Each missing directory down to `directory` made 0700, as ansible.builtin.file's state=directory makes every one
    it creates: os.makedirs would give the parents the umask's mode."""
    missing = []
    while directory and not os.path.isdir(directory):
        missing.append(directory)
        directory = os.path.dirname(directory)
    for path in reversed(missing):
        os.mkdir(path, 0o700)
        os.chmod(path, 0o700)


class ActionModule(ActionBase):
    TRANSFERS_FILES = False
    _VALID_ARGS = frozenset(("path", "content"))

    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        path, content = self._task.args.get("path"), self._task.args.get("content")
        if not isinstance(path, str) or not os.path.isabs(path):
            raise AnsibleActionFail("path must be an absolute path")
        if not isinstance(content, dict):
            raise AnsibleActionFail("content must be a mapping")
        data = (json.dumps(content, indent=4, sort_keys=True) + "\n").encode()
        try:
            with open(path, "rb") as existing:
                changed = existing.read() != data
        except OSError:
            changed = True
        result.update(changed=changed, path=path)
        if self._task.check_mode:
            return result
        directory = os.path.dirname(path)
        try:
            make_directories(directory)
            # mkstemp makes the file 0600, beside the target, so the rename never crosses a filesystem.
            handle, scratch = tempfile.mkstemp(dir=directory, prefix=".write-", suffix=".json")
            try:
                with os.fdopen(handle, "wb") as out:
                    out.write(data)
                os.replace(scratch, path)
            except BaseException:
                os.unlink(scratch)
                raise
        except OSError as error:
            raise AnsibleActionFail(f"could not write {path}: {error}") from error
        return result
