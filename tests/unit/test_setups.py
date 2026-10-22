"""Setups (scripts/init/setups.py): each machine's local.yml, vault and vault password kept outside the checkout, the
one a checkout uses linked in; what make vault TYPE=list, switch and new do, and every refusal.

Each test has a checkout and a store of its own, under tmp_path, and HOME there too, so nothing reads or writes the
real ~/.config/homeserver/setups.
"""

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest
import yaml
from ansible.parsing.vault import VaultLib, VaultSecret

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "init"))
import init as wizard  # noqa: E402
import setups  # noqa: E402

LOCAL, VAULT, VAULT_PASS = (setups.FILES[f] for f in ("local.yml", "vault.yml", "vault_pass"))
PASSWORD = "a-vault-password-for-the-test"


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A checkout holding what setups.py reads: local.yml.example, and nothing of a setup yet."""
    checkout = tmp_path / "checkout"
    (checkout / "group_vars" / "all").mkdir(parents=True)
    shutil.copy(ROOT / wizard.EXAMPLE, checkout / wizard.EXAMPLE)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SETUPS", str(tmp_path / "setups"))
    return checkout


@pytest.fixture
def where(root):
    return setups.store()


def vault_text(data: dict, password: str = PASSWORD) -> bytes:
    return VaultLib([("default", VaultSecret(password.encode()))]).encrypt(yaml.safe_dump(data).encode())


def decrypt(vault: Path, password_file: Path) -> dict:
    secret = VaultSecret(password_file.read_bytes().strip())
    return yaml.safe_load(VaultLib([("default", secret)]).decrypt(vault.read_bytes()))


def own_files(root: Path, local: str = "---\nkubernetes_domain: home.internal\n", password: str = PASSWORD) -> None:
    """A checkout's own three files, as one from before setups has them."""
    (root / LOCAL).write_text(local)
    (root / VAULT).write_bytes(vault_text({"vault_haos_owner_password": "owner-password-of-the-test"}))
    (root / VAULT_PASS).write_text(password + "\n")


def stored(where: Path, name: str, data: dict | None = None, local: str = "---\n") -> Path:
    """A setup written straight into the store, the checkout left alone."""
    return setups.make_setup(
        where,
        name,
        {
            "local.yml": local.encode(),
            "vault.yml": vault_text(data or {}),
            "vault_pass": f"{PASSWORD}\n".encode(),
        },
    )


def links(root: Path) -> dict[Path, Path]:
    return {rel: setups.target(root / rel) for rel in setups.FILES.values() if (root / rel).is_symlink()}


# ------------------------------------------------------------------------------------------------------ new ---
def test_a_new_setup_is_whole_readable_by_its_owner_alone_and_used(root, where):
    setup = setups.new(root, where, "home", wizard.commented_example(root))
    assert setup == where / "home"
    assert stat.S_IMODE(setup.stat().st_mode) == 0o700
    for file, rel in setups.FILES.items():
        assert stat.S_IMODE((setup / file).stat().st_mode) == 0o600, file
        assert (root / rel).is_symlink() and setups.target(root / rel) == setup / file, rel
    assert decrypt(setup / "vault.yml", setup / "vault_pass") == {}, "an empty vault that opens with its password"
    password = (setup / "vault_pass").read_text()
    assert len(password.strip()) == 64 and password.endswith("\n")
    assert yaml.safe_load((root / LOCAL).read_text()) is None, "every setting commented out: the playbooks read none"
    assert "# proxmox_address:" in (root / LOCAL).read_text()
    assert setups.active(root) == setup
    assert setups.check(root) == f"Setup: home ({setup})", "a run names the setup it uses"


def test_two_new_setups_never_share_a_password(root, where):
    home = setups.new(root, where, "home", "---\n")
    lab = setups.new(root, where, "lab", "---\n")
    assert (home / "vault_pass").read_text() != (lab / "vault_pass").read_text()
    assert setups.active(root) == lab, "the checkout uses the setup just made"


@pytest.mark.parametrize("name", ["Home", "-home", "home-", "home_2", "home.d", "../home", "a" * 41])
def test_a_name_is_lower_case_letters_digits_and_dashes(root, where, name):
    with pytest.raises(setups.Refused, match="no setup's name"):
        setups.new(root, where, name, "---\n")
    assert not where.exists() or not list(where.iterdir())


def test_new_never_replaces_a_setup(root, where):
    setup = stored(where, "home", {"vault_truenas_api_key": "kept"})
    before = {f: (setup / f).read_bytes() for f in setups.FILES}
    with pytest.raises(setups.Refused, match="exists already"):
        setups.new(root, where, "home", "---\n")
    assert {f: (setup / f).read_bytes() for f in setups.FILES} == before
    assert not links(root)


# --------------------------------------------------------------------------------------------------- switch ---
def test_switch_points_every_link_at_the_other_setup(root, where):
    home = stored(where, "home", local="---\nkubernetes_domain: home.internal\n")
    lab = stored(where, "lab", local="---\nkubernetes_domain: lab.internal\n")
    assert setups.switch(root, where, "lab") is True
    assert set(links(root).values()) == {lab / f for f in setups.FILES}
    assert "lab.internal" in (root / LOCAL).read_text()
    assert setups.switch(root, where, "home") is True
    assert set(links(root).values()) == {home / f for f in setups.FILES}
    assert setups.switch(root, where, "home") is False, "already in use: nothing to do"
    assert not list(root.rglob("*.setup-tmp")), "no link is left half made"


def test_switch_refuses_a_setup_that_is_missing_or_not_whole(root, where):
    home = stored(where, "home")
    setups.switch(root, where, "home")
    with pytest.raises(setups.Refused, match="There is no setup nope"):
        setups.switch(root, where, "nope")
    partial = stored(where, "partial")
    (partial / "vault_pass").unlink()
    with pytest.raises(setups.Refused, match="it has no vault_pass"):
        setups.switch(root, where, "partial")
    locked = stored(where, "locked")
    (locked / "vault_pass").write_text("not the password\n")
    with pytest.raises(setups.Refused, match="does not open"):
        setups.switch(root, where, "locked")
    assert set(links(root).values()) == {home / f for f in setups.FILES}, "the checkout keeps the setup it had"


def test_switch_and_new_never_replace_a_file_of_the_checkouts_own(root, where):
    own_files(root)
    before = {rel: (root / rel).read_bytes() for rel in setups.FILES.values()}
    stored(where, "lab")
    with pytest.raises(setups.Refused, match="FROM=current"):
        setups.switch(root, where, "lab")
    with pytest.raises(setups.Refused, match="FROM=current"):
        setups.new(root, where, "spare", "---\n")
    assert {rel: (root / rel).read_bytes() for rel in setups.FILES.values()} == before
    assert not links(root) and not (where / "spare").exists()


def test_a_vault_at_its_old_committed_path_is_moved_first(root, where):
    stored(where, "home")
    (root / setups.OLD_VAULT).write_text("$ANSIBLE_VAULT;1.1;AES256\n6162\n")
    for attempt in (
        lambda: setups.switch(root, where, "home"),
        lambda: setups.new(root, where, "spare", "---\n"),
    ):
        with pytest.raises(setups.Refused, match="committed until 2026-10-20"):
            attempt()
    own_files(root)
    with pytest.raises(setups.Refused, match="committed until 2026-10-20"):
        setups.adopt(root, where, "mine")
    assert not links(root)


# ---------------------------------------------------------------------------------------------------- adopt ---
def test_a_checkouts_own_files_move_into_a_setup_and_are_linked_back(root, where):
    own_files(root)
    before = {rel: (root / rel).read_bytes() for rel in setups.FILES.values()}
    setup = setups.adopt(root, where, "home")
    for file, rel in setups.FILES.items():
        assert (root / rel).is_symlink(), rel
        assert (setup / file).read_bytes() == before[rel], "moved as it was"
        assert stat.S_IMODE((setup / file).stat().st_mode) == 0o600
    assert setups.active(root) == setup
    with pytest.raises(setups.Refused, match="links to setup home already"):
        setups.adopt(root, where, "again")


def test_files_that_are_not_whole_are_never_adopted(root, where):
    own_files(root, password="not the password")
    with pytest.raises(setups.Refused, match=r"does not open with \.vault_pass"):
        setups.adopt(root, where, "home")
    (root / LOCAL).unlink()
    with pytest.raises(setups.Refused, match="make init writes it"):
        setups.adopt(root, where, "home")
    assert not (where / "home").exists() and not links(root)
    assert (root / VAULT).is_file() and not (root / VAULT).is_symlink(), "the files stay where they were"


def test_a_local_yml_that_sets_nothing_gives_way_to_a_setup(root, where):
    """make init's first run writes local.yml with every setting commented out, and a setup restored after it still
    links in, since nothing is lost; a local.yml that sets anything is refused by name."""
    (root / LOCAL).write_text(wizard.commented_example(root))
    lab = setups.new(root, where, "lab", "---\n")
    assert setups.active(root) == lab
    (root / LOCAL).unlink()
    (root / LOCAL).write_text(wizard.commented_example(root))
    home = stored(where, "home")
    assert setups.switch(root, where, "home") is True and setups.active(root) == home
    (root / LOCAL).unlink()
    (root / LOCAL).write_text("---\nkubernetes_domain: home.internal\n")
    with pytest.raises(setups.Refused, match=r"local\.yml is this checkout's own.*move it out of the checkout"):
        setups.switch(root, where, "lab")
    assert (root / LOCAL).read_text() == "---\nkubernetes_domain: home.internal\n"


def test_a_link_of_the_persons_own_is_no_setup(root, where, tmp_path):
    """A .vault_pass linked to a password file kept elsewhere, or a worktree whose three paths link to its main
    checkout's, uses its own files as before. Adopting them reads through the link and leaves the file it leads to."""
    own_files(root)
    kept = tmp_path / "secrets" / "pw"
    kept.parent.mkdir()
    (root / VAULT_PASS).rename(kept)
    (root / VAULT_PASS).symlink_to(kept)
    assert setups.check(root) == "Setup: this checkout's own files"
    worktree = tmp_path / "worktree"
    (worktree / "group_vars" / "all").mkdir(parents=True)
    for rel in setups.FILES.values():
        (worktree / rel).symlink_to(root / rel)
    assert setups.check(worktree) == "Setup: this checkout's own files"
    setup = setups.adopt(root, where, "home")
    assert setups.active(root) == setup and kept.read_text() == PASSWORD + "\n", "the file it linked to stays"
    assert setups.check(worktree) == f"Setup: home ({setup})", "followed through its main checkout's links"


def test_relative_links_into_a_whole_setup_are_whole(root, where):
    home = stored(where, "home")
    for file, rel in setups.FILES.items():
        (root / rel).symlink_to(os.path.relpath(home / file, (root / rel).parent))
    assert setups.active(root) == home


def test_a_move_cut_short_is_finished_by_switching_to_its_setup(root, where):
    """new --from current stopped between two links: runs are refused, saying how to finish, and a switch to the
    setup replaces the files left, which are the same as the setup's."""
    own_files(root)
    setup = setups.make_setup(where, "home", {f: (root / rel).read_bytes() for f, rel in setups.FILES.items()})
    for file in ("local.yml", "vault.yml"):
        (root / setups.FILES[file]).unlink()
        (root / setups.FILES[file]).symlink_to(setup / file)
    with pytest.raises(setups.Refused, match=r"\.vault_pass is a file of this checkout's own.*NAME=home finishes"):
        setups.check(root)
    assert setups.switch(root, where, "home") is True
    assert setups.active(root) == setup


# ---------------------------------------------------------------------------------------------------- check ---
def test_a_checkout_with_its_own_files_or_none_passes_the_check(root):
    assert setups.check(root) == "Setup: this checkout's own files"
    own_files(root)
    assert setups.check(root) == "Setup: this checkout's own files"


def test_a_run_stops_on_a_link_to_a_missing_file(root, where):
    """Ansible skips a link to a missing file without a word, so a run would go without the setup's settings or
    secrets: the check names the setup and the file."""
    home = stored(where, "home")
    setups.switch(root, where, "home")
    (home / "vault.yml").unlink()
    with pytest.raises(setups.Refused, match=r"setup, home, is not whole.*local\.vault\.yml links to .*missing"):
        setups.check(root)


def test_a_run_stops_on_links_into_two_setups_or_beside_a_file_of_its_own(root, where):
    stored(where, "home")
    lab = stored(where, "lab")
    setups.switch(root, where, "home")
    (root / VAULT_PASS).unlink()
    os.symlink(lab / "vault_pass", root / VAULT_PASS)
    with pytest.raises(setups.Refused, match=r"\.vault_pass links to .*lab/vault_pass, not to"):
        setups.check(root)
    (root / VAULT_PASS).unlink()
    (root / VAULT_PASS).write_text(PASSWORD + "\n")
    with pytest.raises(setups.Refused, match=r"\.vault_pass is a file of this checkout's own, not a link"):
        setups.check(root)


def test_a_run_stops_when_the_old_vault_is_back_beside_the_vault(root, where):
    """A branch from before 2026-10-20 brings the committed vault back, and Ansible would read it after the
    setup's."""
    stored(where, "home")
    setups.switch(root, where, "home")
    (root / setups.OLD_VAULT).write_text("$ANSIBLE_VAULT;1.1;AES256\n6162\n")
    with pytest.raises(setups.Refused, match="its keys would win over the vault's"):
        setups.check(root)


# ----------------------------------------------------------------------------------------------------- list ---
def test_list_marks_the_setup_in_use_and_shows_no_secret(root, where):
    secret = "a-secret-the-list-never-shows"
    stored(where, "home", {"vault_truenas_api_key": secret, "vault_haos_owner_password": secret})
    stored(where, "lab")
    broken = stored(where, "old")
    (broken / "vault_pass").unlink()
    setups.switch(root, where, "home")
    text = setups.listing(root, where)
    rows = {line.split()[1] if line.startswith("*") else line.split()[0]: line for line in text.splitlines()[2:]}
    assert rows["home"].startswith("*") and "opens, 2 keys" in rows["home"]
    assert not rows["lab"].startswith("*") and "opens, 0 keys" in rows["lab"]
    assert "no vault_pass" in rows["old"]
    assert secret not in text and PASSWORD not in text


def test_list_says_what_a_checkout_without_a_setup_can_do(root, where):
    assert "No setups in" in setups.listing(root, where)
    own_files(root)
    assert "FROM=current" in setups.listing(root, where)


# ------------------------------------------------------------------------------------------------------ cli ---
@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["switch"], "needs NAME=<setup>"),
        (["new", "--from", "current"], "which needs a name"),
        (["new", "home", "--from", "elsewhere"], "FROM takes current alone"),
        (["edit", "nope"], "There is no setup nope"),
    ],
)
def test_the_command_line_says_why_it_refuses(root, capsys, argv, says):
    assert setups.main(argv, root=root) == 1
    assert says in capsys.readouterr().err


def test_the_command_line_makes_a_setup_of_the_example(root, capsys):
    assert setups.main(["name"], root=root) == 0 and capsys.readouterr().out == "", "no setup, no name"
    assert setups.main(["new", "home"], root=root) == 0
    assert "Made setup home" in capsys.readouterr().out
    assert (root / LOCAL).read_text() == wizard.commented_example(root)
    assert setups.main(["name"], root=root) == 0 and capsys.readouterr().out == "home\n", "for the run's log"


def test_without_a_name_new_makes_the_checkouts_own_vault(root, capsys):
    assert setups.main(["new"], root=root) == 0
    assert decrypt(root / VAULT, root / VAULT_PASS) == {}
    assert not links(root) and stat.S_IMODE((root / VAULT_PASS).stat().st_mode) == 0o600
    assert setups.main(["new"], root=root) == 0 and "nothing to do" in capsys.readouterr().out


def test_the_store_is_never_inside_the_checkout(root, monkeypatch, capsys):
    monkeypatch.setenv("SETUPS", str(root / "setups"))
    assert setups.main(["new", "lab"], root=root) == 1
    assert "inside this checkout" in capsys.readouterr().err
    assert not (root / "setups").exists() and not links(root)
