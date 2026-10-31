"""A run log's name (scripts/run_log.py), worked out once for just's playbook recipes and for a run started by hand,
and a log made for a run started without one: 0600 in a 0700 logs/, with logs/latest.log pointing at it."""

import importlib.util
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("test_run_log_module", ROOT / "scripts" / "run_log.py")
run_log = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_log)
WHEN = time.mktime((2026, 10, 29, 21, 5, 9, 0, 0, -1))


def test_a_log_is_named_as_just_names_it():
    def named(*args, **kwargs):
        return run_log.name(*args, **kwargs, when=WHEN)

    assert named("deploy", "site") == "20261029-210509-deploy.log"
    assert named("deploy", "proxmox", setup="home") == "20261029-210509-home-deploy-proxmox.log"
    assert named("check", "kubernetes", check=True) == "20261029-210509-check-kubernetes.log"
    assert named("deploy", "site", check=True) == "20261029-210509-check-deploy.log"
    assert named("suspend", "suspend", check=True) == "20261029-210509-check-suspend.log"
    assert named("deploy", "truenas", tags="truenas_pools,truenas_nfs") == (
        "20261029-210509-deploy-truenas-truenas_pools+truenas_nfs.log"
    )


def test_a_log_made_for_a_run_is_private_and_the_latest(tmp_path):
    (tmp_path / "scripts" / "init").mkdir(parents=True)
    path = run_log.create(tmp_path, "check", "proxmox", True, "")
    assert path.parent == tmp_path / "logs" and path.name.endswith("-check-proxmox.log") and path.read_text() == ""
    assert oct(path.stat().st_mode & 0o777) == "0o600" and oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert os.readlink(tmp_path / "logs" / "latest.log") == path.name
