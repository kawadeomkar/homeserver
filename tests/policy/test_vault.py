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


def test_vault_init_makes_a_private_vault(tmp_path):
    """Without a NAS: a .vault_pass only its owner can read, and an empty mapping encrypted with it over the
    tracked vault, which that password cannot open. A second run finds a vault that opens and leaves it alone."""
    for name in ("Makefile", "ansible.cfg"):
        shutil.copy(ROOT / name, tmp_path)
    vault = tmp_path / "group_vars" / "all" / "vault.yml"
    vault.parent.mkdir(parents=True)
    shutil.copy(ROOT / "group_vars" / "all" / "vault.yml", vault)
    env = {**os.environ, "ANSIBLE_CONFIG": str(tmp_path / "ansible.cfg")}

    def run(*command):
        return subprocess.run(command, cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)

    make = ("make", "--no-print-directory", "vault-init", f"VENV={sys.prefix}")
    first = run(*make)
    assert first.returncode == 0, first.stdout + first.stderr
    assert (tmp_path / ".vault_pass").stat().st_mode & 0o777 == 0o600
    assert vault.read_text().startswith(HEADERS)
    view = run(str(Path(sys.prefix) / "bin" / "ansible-vault"), "view", "group_vars/all/vault.yml")
    assert view.returncode == 0 and yaml.safe_load(view.stdout) == {}, view.stderr
    before = vault.read_bytes()
    second = run(*make)
    assert second.returncode == 0 and "nothing to do" in second.stdout, second.stdout + second.stderr
    assert vault.read_bytes() == before
