"""make init asks for every setting local.yml can hold and every secret the playbooks read from the vault.

A setting added to local.yml.example, or a vault variable added to the group_vars, fails here until it is in
scripts/init/settings.py too, and the other way round.
"""

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "init"))
import settings as cfg  # noqa: E402

EXAMPLE = ROOT / "group_vars" / "all" / "local.yml.example"
# The vault variables no run asks for: the Talos secrets bundle, which every playbook refuses in the vault.
NEVER_ASKED = {"vault_talos_secrets"}


def example_keys() -> set[str]:
    """The settings local.yml.example sets or shows commented out."""
    return set(re.findall(r"^#?\s?([a-z][a-z0-9]*_[a-z0-9_]+):", EXAMPLE.read_text(), re.M))


def test_make_init_asks_for_every_setting_in_the_example():
    asked = {s.key for s in cfg.SETTINGS if not s.secret}
    assert example_keys() == asked


def test_make_init_asks_for_every_secret_the_playbooks_read():
    read = set()
    for path in (ROOT / "group_vars").rglob("*.yml"):
        if path.name not in ("vault.yml", "local.yml"):
            read |= set(re.findall(r"\bvault_[a-z0-9_]+\b", path.read_text()))
    assert read - NEVER_ASKED == {s.key for s in cfg.SETTINGS if s.secret}


def test_the_example_holds_only_placeholders():
    """So a copy of it never passes for a configured setup, in whole or in part: make init counts every value in it
    as unset."""
    values = yaml.safe_load(EXAMPLE.read_text())
    assert [k for k, v in values.items() if cfg.real_part(v) is not None] == []


def test_every_setting_is_of_a_kind_make_init_can_ask():
    for s in cfg.SETTINGS:
        assert s.kind in cfg.PARSERS or s.kind in cfg.COMPOSITE, s.key
        assert s.section in cfg.SECTIONS, s.key
        assert s.need in ("required", "recommended", "optional"), s.key
        assert s.after is None or s.after in cfg.BY_KEY, s.key
