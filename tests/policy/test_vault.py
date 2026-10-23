"""The vault is never committed, and `make vault TYPE=new` makes one of a user's own.

Ansible's secrets live in group_vars/all/local.vault.yml, encrypted with .vault_pass, and both are git-ignored: nothing
reads them from the repository, and this public repo would publish even their ciphertext. Until 2026-10-20 the vault
was committed as group_vars/all/vault.yml. The first tests, with gitleaks's sensitive-file rule, are what stop a vault,
encrypted or not, from reaching the repo again.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
HEADERS = ("$ANSIBLE_VAULT;1.1;AES256\n", "$ANSIBLE_VAULT;1.2;AES256;")
VAULT = "group_vars/all/local.vault.yml"
OLD_VAULT = "group_vars/all/vault.yml"
EXAMPLE = ROOT / "group_vars" / "all" / "local.vault.yml.example"
# What the group_vars read from the vault but no setup puts there: the Talos secrets bundle, which every playbook
# refuses in the vault.
NEVER_IN_THE_VAULT = {"vault_talos_secrets"}


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False)


def test_no_vault_is_tracked():
    tracked = git("ls-files", "--", "*vault*.yml", "*vault*.yaml").stdout.split()
    assert not tracked, f"a vault is never committed: {', '.join(tracked)}"


def test_the_vault_is_ignored_and_its_example_is_not():
    assert git("check-ignore", "-q", VAULT).returncode == 0, f"{VAULT} is not git-ignored"
    assert git("check-ignore", "-q", "--no-index", str(EXAMPLE.relative_to(ROOT))).returncode == 1


def test_the_example_lists_every_secret_the_playbooks_read():
    read = set()
    for path in (ROOT / "group_vars").rglob("*.yml"):
        if "vault" not in path.name and path.name != "local.yml":
            read |= set(re.findall(r"\bvault_[a-z0-9_]+\b", path.read_text()))
    text = EXAMPLE.read_text()
    assert set(re.findall(r"^# (vault_[a-z0-9_]+):", text, re.M)) == read - NEVER_IN_THE_VAULT
    assert yaml.safe_load(text) is None, "the example sets nothing: a placeholder secret would pass for a real one"


def vault_init_tree(tmp_path: Path) -> Path:
    """The Makefile, ansible.cfg and scripts/init/setups.py, which makes the vault, copied into tmp_path, with
    nowhere a vault yet."""
    for name in ("Makefile", "ansible.cfg"):
        shutil.copy(ROOT / name, tmp_path)
    (tmp_path / "scripts" / "init").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "init" / "setups.py", tmp_path / "scripts" / "init" / "setups.py")
    (tmp_path / "group_vars" / "all").mkdir(parents=True)
    return tmp_path / VAULT


def run_in(tree: Path, *command: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "ANSIBLE_CONFIG": str(tree / "ansible.cfg")}
    return subprocess.run(command, cwd=tree, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)


def vault_init(tree: Path, venv=sys.prefix) -> subprocess.CompletedProcess:
    return run_in(tree, "make", "--no-print-directory", "vault", "TYPE=new", f"VENV={venv}")


def view(tree: Path) -> subprocess.CompletedProcess:
    return run_in(tree, str(Path(sys.prefix) / "bin" / "ansible-vault"), "view", VAULT)


def test_vault_init_makes_a_vault_and_its_password(tmp_path):
    """A .vault_pass and a vault only their owner can read, the vault an empty mapping. A second run finds a vault
    that opens and leaves it alone."""
    vault = vault_init_tree(tmp_path)
    first = vault_init(tmp_path)
    assert first.returncode == 0, first.stdout + first.stderr
    for path in (tmp_path / ".vault_pass", vault):
        assert path.stat().st_mode & 0o777 == 0o600, path
    assert vault.read_text().startswith(HEADERS)
    opened = view(tmp_path)
    assert opened.returncode == 0 and yaml.safe_load(opened.stdout) == {}, opened.stderr
    before = vault.read_bytes()
    second = vault_init(tmp_path)
    assert second.returncode == 0 and "nothing to do" in second.stdout, second.stdout + second.stderr
    assert vault.read_bytes() == before


def test_vault_init_keeps_the_password_it_finds(tmp_path):
    """A .vault_pass restored without its vault: the new vault is encrypted with it, and it is not replaced."""
    vault_init_tree(tmp_path)
    (tmp_path / ".vault_pass").write_text("a restored password\n")
    assert vault_init(tmp_path).returncode == 0
    assert (tmp_path / ".vault_pass").read_text() == "a restored password\n"
    assert view(tmp_path).returncode == 0


def test_vault_init_never_replaces_a_vault_it_cannot_open(tmp_path):
    """A vault that .vault_pass does not open may hold the user's secrets with their password lost: it is refused,
    and neither file is written."""
    vault = vault_init_tree(tmp_path)
    assert vault_init(tmp_path).returncode == 0
    (tmp_path / ".vault_pass").unlink()
    before = vault.read_bytes()
    refused = vault_init(tmp_path)
    assert refused.returncode != 0 and "may hold your secrets" in refused.stderr, refused.stdout + refused.stderr
    assert vault.read_bytes() == before and not (tmp_path / ".vault_pass").exists()
    (tmp_path / ".vault_pass").write_text("not the password\n")
    assert vault_init(tmp_path).returncode != 0 and vault.read_bytes() == before


def test_vault_init_moves_no_vault_but_says_to(tmp_path):
    """A checkout from before 2026-10-20 still has the vault where it was committed: rather than a new, empty vault
    beside it, the user is told to move it."""
    vault = vault_init_tree(tmp_path)
    (tmp_path / OLD_VAULT).write_text(HEADERS[0] + "6162\n")
    refused = vault_init(tmp_path)
    assert refused.returncode != 0 and "committed until 2026-10-20" in refused.stderr, refused.stdout + refused.stderr
    assert not vault.exists() and not (tmp_path / ".vault_pass").exists()


def test_vault_init_writes_nothing_when_it_cannot_encrypt(tmp_path):
    """Without ansible-vault, as when VIRTUAL_ENV still names a deleted virtualenv, it stops before creating
    anything. When encryption fails, here for an empty .vault_pass, no vault is written, so an empty one never lands
    in plain text."""
    vault = vault_init_tree(tmp_path)
    missing = vault_init(tmp_path, venv=tmp_path / "deleted-venv")
    assert missing.returncode != 0 and "ansible-vault is not in" in missing.stdout, missing.stdout + missing.stderr
    assert not vault.exists() and not (tmp_path / ".vault_pass").exists()
    (tmp_path / ".vault_pass").write_text("")
    failed = vault_init(tmp_path)
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert not vault.exists()
