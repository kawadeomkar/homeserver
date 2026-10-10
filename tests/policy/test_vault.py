"""Every tracked vault file is encrypted, and `make vault-init` makes a private vault of a user's own.

CI cannot decrypt the vault, so its linters skip what is inside. The first test is what stops a decrypted
vault.yml, an `ansible-vault decrypt` committed by mistake, from reaching this public repo.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
HEADERS = ("$ANSIBLE_VAULT;1.1;AES256\n", "$ANSIBLE_VAULT;1.2;AES256;")


def test_every_tracked_vault_file_is_encrypted():
    tracked = subprocess.run(
        ["git", "ls-files", "--", "*vault*.yml", "*vault*.yaml"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert "group_vars/all/vault.yml" in tracked, f"git ls-files did not list group_vars/all/vault.yml: {tracked}"
    plain = [name for name in tracked if not (ROOT / name).read_text().startswith(HEADERS)]
    assert not plain, f"not encrypted with ansible-vault: {', '.join(plain)}"


def vault_init_tree(tmp_path):
    """The Makefile, ansible.cfg and the tracked vault, which no test password opens, copied into tmp_path."""
    for name in ("Makefile", "ansible.cfg"):
        shutil.copy(ROOT / name, tmp_path)
    vault = tmp_path / "group_vars" / "all" / "vault.yml"
    vault.parent.mkdir(parents=True)
    shutil.copy(ROOT / "group_vars" / "all" / "vault.yml", vault)
    return vault


def run_in(tree, *command):
    env = {**os.environ, "ANSIBLE_CONFIG": str(tree / "ansible.cfg")}
    return subprocess.run(command, cwd=tree, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)


def vault_init(tree, venv=sys.prefix):
    return run_in(tree, "make", "--no-print-directory", "vault-init", f"VENV={venv}")


def test_vault_init_makes_a_private_vault(tmp_path):
    """Without a NAS: a .vault_pass only its owner can read, and an empty mapping encrypted with it over the
    tracked vault, which that password cannot open. A second run finds a vault that opens and leaves it alone."""
    vault = vault_init_tree(tmp_path)
    first = vault_init(tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (tmp_path / ".vault_pass").stat().st_mode & 0o777 == 0o600
    assert vault.read_text().startswith(HEADERS)
    view = run_in(tmp_path, str(Path(sys.prefix) / "bin" / "ansible-vault"), "view", "group_vars/all/vault.yml")
    assert view.returncode == 0 and yaml.safe_load(view.stdout) == {}, view.stderr
    before = vault.read_bytes()
    second = vault_init(tmp_path)
    assert second.returncode == 0 and "nothing to do" in second.stdout, second.stdout + second.stderr
    assert vault.read_bytes() == before


def test_vault_init_leaves_the_vault_alone_when_it_cannot_encrypt(tmp_path):
    """Without ansible-vault, as when VIRTUAL_ENV still names a deleted virtualenv, it stops before creating
    anything; a vault it cannot open would otherwise pass for someone else's. When encryption fails, here for
    an empty .vault_pass, the tracked vault is left as it was: the empty one never lands in plain text."""
    vault = vault_init_tree(tmp_path)
    before = vault.read_bytes()
    missing = vault_init(tmp_path, venv=tmp_path / "deleted-venv")
    assert missing.returncode != 0 and "ansible-vault is not in" in missing.stdout, missing.stdout + missing.stderr
    assert vault.read_bytes() == before and not (tmp_path / ".vault_pass").exists()
    (tmp_path / ".vault_pass").write_text("")
    failed = vault_init(tmp_path)
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert vault.read_bytes() == before
