"""Stop a playbook run whose setup's links are not whole (scripts/init/setups.py), however it was started.

Ansible reads group_vars/all/local.yml and group_vars/all/local.vault.yml, and the vault's password from .vault_pass,
which a checkout may link from a named setup. A link among them to a file that is gone is passed over without a word,
as if there were no settings or no secrets, and the run would go on without them: a missing local.yml reads as no NAS
and no cluster. make's playbook targets check the links first and name the setup; this does the same for
ansible-playbook started any other way, since Ansible loads the vars plugins beside a playbook by itself. It adds no
variable, and reads only the links and the old vault's path, never a vault.
"""

import importlib.util
from functools import cache
from pathlib import Path

from ansible.errors import AnsibleError
from ansible.plugins.vars import BaseVarsPlugin

ROOT = Path(__file__).resolve().parents[1]


@cache
def refusal() -> str | None:
    """Why this checkout's setup stops every run, or None: asked once per run."""
    spec = importlib.util.spec_from_file_location("homeserver_setups", ROOT / "scripts" / "init" / "setups.py")
    setups = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setups)
    try:
        setups.check(ROOT)
    except setups.Refused as e:
        return str(e)
    return None


class VarsModule(BaseVarsPlugin):
    def get_vars(self, loader, path, entities):
        why = refusal()
        if why:
            raise AnsibleError(why)
        return {}
