"""A run log's name, worked out once: for just's playbook recipes and for a playbook run by hand.

logs/<when>-<setup>-<command>[-<tags>].log, the setup left out for a checkout's own files: deploy-proxmox,
check-kubernetes, check-deploy (just deploy --check), suspend, with the tags it was given joined by +. just's
_playbook prints it (`run_log.py name <recipe> <playbook> <tags> [--check]`) and runs ansible-playbook with it; the
live engine (filter_plugins/live_ansible.py) makes one with create() for a run started without a log, since a drawn
run's stream goes there. Logs hold addresses, so the directory is 0700 and each log 0600, and logs/latest.log points
at the newest.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = "logs"


def _setups():
    """scripts/init/setups.py, loaded by path once as homeserver_setups, as vars_plugins/setup_links.py loads it."""
    if "homeserver_setups" not in sys.modules:
        spec = importlib.util.spec_from_file_location("homeserver_setups", ROOT / "scripts" / "init" / "setups.py")
        sys.modules["homeserver_setups"] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sys.modules["homeserver_setups"])
    return sys.modules["homeserver_setups"]


def setup_name(root=ROOT):
    """The named setup the checkout links to, or "" for its own files or links that are not whole."""
    try:
        setups = _setups()
        found = setups.active(root)
    except Exception:  # noqa: BLE001 - a log's name without the setup, rather than no run: setups.py check says why
        return ""
    return found.name if found and setups.NAME.fullmatch(found.name) else ""


def name(recipe, playbook, check=False, tags="", setup="", when=None):
    """A log's file name: the recipe, then the playbook unless it is the recipe's own or site, "check-" before a dry
    run of a recipe that is not check itself, and the tags."""
    command = recipe if playbook in (recipe, "site") else f"{recipe}-{playbook}"
    if check and recipe != "check":
        command = f"check-{command}"
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
    tagged = f"-{tags.replace(',', '+')}" if tags else ""
    return f"{stamp}{f'-{setup}' if setup else ''}-{command}{tagged}.log"


def create(root, recipe, playbook, check=False, tags=""):
    """A new log under root/logs, empty, 0600 in a 0700 directory, with logs/latest.log pointing at it: its path."""
    logs = Path(root) / LOGS
    logs.mkdir(mode=0o700, exist_ok=True)
    path = logs / name(recipe, playbook, check, tags, setup_name(root))
    os.close(os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600))
    latest = logs / "latest.log"
    with contextlib.suppress(OSError):
        latest.unlink(missing_ok=True)
        latest.symlink_to(path.name)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    named = commands.add_parser("name", help="print a log's file name")
    named.add_argument("recipe")
    named.add_argument("playbook")
    named.add_argument("tags", nargs="?", default="")
    named.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    print(name(args.recipe, args.playbook, args.check, args.tags, setup_name()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
