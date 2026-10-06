"""Every tracked vault file is encrypted.

CI cannot decrypt the vault, so its linters skip what is inside. This test is what stops a decrypted
vault.yml, an `ansible-vault decrypt` committed by mistake, from reaching this public repo.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HEADERS = ("$ANSIBLE_VAULT;1.1;AES256\n", "$ANSIBLE_VAULT;1.2;AES256;")


def test_every_tracked_vault_file_is_encrypted():
    tracked = subprocess.run(
        ["git", "ls-files", "--", "*vault*.yml", "*vault*.yaml"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    assert "group_vars/all/vault.yml" in tracked, f"git ls-files did not list group_vars/all/vault.yml: {tracked}"
    plain = [name for name in tracked if not (ROOT / name).read_text().startswith(HEADERS)]
    assert not plain, f"not encrypted with ansible-vault: {', '.join(plain)}"
