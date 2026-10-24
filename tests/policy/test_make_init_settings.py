"""just init asks for every setting local.yml can hold and every secret the playbooks read from the vault, and shows
the defaults the playbooks use.

A setting added to local.yml.example, or a vault variable added to the group_vars, fails here until it is in
scripts/init/settings.py too, and the other way round; so does a default or a node changed in the group_vars.
"""

import os
import re
import sys
from pathlib import Path

import yaml
from helpers import example_tree, resolve_on

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
        if path.name not in ("local.vault.yml", "vault.yml", "local.yml"):
            read |= set(re.findall(r"\bvault_[a-z0-9_]+\b", path.read_text()))
    assert read - NEVER_ASKED == {s.key for s in cfg.SETTINGS if s.secret}


# Documentation values: RFC 5737 addresses, <...>, made-up serials and the made-up certificate pin.
DOCUMENTATION = re.compile(r"\b(192\.0\.2|198\.51\.100|203\.0\.113)\.\d+|<[^>]*>|^SERIAL-\d+$|^AA:BB:CC:DD:EE:FF:")


def leaves(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from leaves(v)
    elif isinstance(value, list):
        for v in value:
            yield from leaves(v)
    else:
        yield value


def test_the_example_holds_only_documentation_values():
    """So a copy made by hand never points the playbooks at a real host or disk. just init itself writes local.yml
    with these values commented out, and comments out any an older copy still holds."""
    values = yaml.safe_load(EXAMPLE.read_text())
    assert [k for k, v in values.items() for leaf in leaves(v) if not DOCUMENTATION.search(str(leaf))] == []


def test_the_defaults_make_init_shows_are_the_ones_the_playbooks_use(tmp_path):
    """settings.py restates them, for its prompts: each is resolved here as the playbooks resolve it, with a
    local.yml that sets nothing."""
    tree = example_tree(tmp_path, "---\n")
    used = resolve_on(
        tree,
        "homeserver-k8s",
        "{'kubernetes_domain': kubernetes_domain_default, 'kubernetes_talos_config_dir': talos_config_dir,"
        " 'sops_age_key_file': flux_bootstrap_age_key_file, 'flux_sync_ref': flux_bootstrap_instance.ref,"
        " 'truenas_pool_persistent': nas_pools.persistent, 'truenas_pool_ephemeral': nas_pools.ephemeral,"
        " 'haos_owner_name': haos_owner.name, 'haos_owner_username': haos_owner.username,"
        " 'haos_vmid': haos_vm_vmid, 'haos_size': haos_vm_size, 'proxmox_local_vm_storage': vm_storage_installer,"
        " 'kubernetes_iso_storage': kubernetes_iso_storage, 'haos_import_storage': haos_import_storage,"
        " 'nodes': kubernetes_node_shape | items2dict(key_name='name', value_name='role'),"
        " 'size_keys': kubernetes_node_size_keys}",
        "-e",
        "flux_sync_url=https://example.invalid/homeserver.git",
    )
    mail = resolve_on(tree, "truenas", "truenas_mail", "-e", "truenas_mail_server=smtp.example.invalid")
    used |= {"truenas_mail_port": mail["port"], "truenas_mail_security": mail["security"]}
    for path in ("kubernetes_talos_config_dir", "sops_age_key_file"):
        used[path] = cfg.DEFAULTS[path] if used[path] == os.path.expanduser(cfg.DEFAULTS[path]) else used[path]
    nodes, size_keys = used.pop("nodes"), used.pop("size_keys")
    assert used == cfg.DEFAULTS
    assert nodes == {cfg.CONTROL_PLANE: "controlplane", cfg.WORKER: "worker"}
    assert cfg.NODE_SIZE_FIELDS[cfg.WORKER] == tuple(size_keys)
    # The control plane takes no data or media disk, which the playbooks refuse.
    assert cfg.NODE_SIZE_FIELDS[cfg.CONTROL_PLANE] == tuple(
        k for k in size_keys if k not in ("data_disk_gib", "media_disk_gib")
    )
    assert set(cfg.OPTIONAL_DISKS) <= set(size_keys)


def test_every_setting_is_of_a_kind_make_init_can_ask():
    for s in cfg.SETTINGS:
        assert s.kind in cfg.PARSERS or s.kind in cfg.COMPOSITE, s.key
        assert s.section in cfg.SECTIONS, s.key
        assert s.need in ("required", "recommended", "optional"), s.key
        assert s.after is None or s.after in cfg.BY_KEY, s.key
