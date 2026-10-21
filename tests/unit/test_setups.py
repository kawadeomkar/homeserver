"""Setups (scripts/init/setups.py): each machine's local.yml, vault and vault password kept outside the checkout, the
one a checkout uses linked in; what make vault TYPE=list, switch and new do, and every refusal.

Each test has a checkout and a store of its own, under tmp_path, and HOME there too, so nothing reads or writes the
real ~/.config/homeserver/setups.
"""

import os
import shutil
import stat
import subprocess
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
    with pytest.raises(setups.Refused, match="link already"):
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
    assert setups.main(["new", "home"], root=root) == 0
    assert "Made setup home" in capsys.readouterr().out
    assert (root / LOCAL).read_text() == wizard.commented_example(root)


# ----------------------------------------------------------------------------------------------------- make ---
def tree(tmp_path: Path) -> Path:
    """The Makefile and what make vault runs, copied into a checkout of their own."""
    checkout = tmp_path / "tree"
    (checkout / "scripts" / "init").mkdir(parents=True)
    (checkout / "group_vars" / "all").mkdir(parents=True)
    for name in ("Makefile", "ansible.cfg", str(wizard.EXAMPLE)):
        shutil.copy(ROOT / name, checkout / name)
    for script in ("init.py", "settings.py", "setups.py"):
        shutil.copy(ROOT / "scripts" / "init" / script, checkout / "scripts" / "init" / script)
    return checkout


# What an outer make (make test TYPE=unit) hands every make below it: its flags, and the variables of its command line,
# which it puts in the environment too, TYPE among them.
OUTER_MAKE = {
    *("MAKEFLAGS", "MFLAGS", "MAKELEVEL", "MAKEOVERRIDES"),
    *("TYPE", "NAME", "FROM", "SETUPS", "TAGS", "ONLY", "ALL", "ANSIBLE_ARGS", "PYTEST_ARGS", "UPDATE"),
}


def make(checkout: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    inherited = {k: v for k, v in os.environ.items() if k not in OUTER_MAKE}
    return subprocess.run(
        ["make", "--no-print-directory", *args, f"VENV={sys.prefix}"],
        cwd=checkout,
        env={**inherited, "ANSIBLE_CONFIG": str(checkout / "ansible.cfg"), **env},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def test_make_vault_makes_lists_and_switches_setups(tmp_path):
    checkout = tree(tmp_path)
    store = tmp_path / "setups"
    for name in ("home", "lab"):
        made = make(checkout, "vault", "TYPE=new", f"NAME={name}", f"SETUPS={store}")
        assert made.returncode == 0, made.stdout + made.stderr
    listed = make(checkout, "vault", "TYPE=list", f"SETUPS={store}")
    assert listed.returncode == 0 and "*  lab" in listed.stdout and "  home" in listed.stdout, listed.stdout
    switched = make(checkout, "vault", "TYPE=switch", "NAME=home", f"SETUPS={store}")
    assert switched.returncode == 0 and "uses setup home now" in switched.stdout, switched.stdout + switched.stderr
    assert setups.target(checkout / LOCAL) == store / "home" / "local.yml"
    plain = make(checkout, "vault", "TYPE=new", f"SETUPS={store}")
    assert plain.returncode != 0 and "uses a setup" in plain.stdout, "never written through a setup's links"
    nameless = make(checkout, "vault", "TYPE=switch", f"SETUPS={store}")
    assert nameless.returncode != 0 and "needs NAME" in nameless.stderr
    both = make(checkout, "vault", "TYPE=list,switch")
    assert both.returncode != 0 and "one TYPE at a time" in both.stderr


def test_make_vault_edits_another_setups_vault_without_switching(tmp_path):
    checkout = tree(tmp_path)
    store = tmp_path / "setups"
    for name in ("lab", "home"):
        assert make(checkout, "vault", "TYPE=new", f"NAME={name}", f"SETUPS={store}").returncode == 0
    editor = tmp_path / "editor.py"
    editor.write_text("import sys\nopen(sys.argv[1], 'w').write('vault_haos_owner_password: edited\\n')\n")
    edited = make(checkout, "vault", "NAME=lab", f"SETUPS={store}", EDITOR=f"{sys.executable} {editor}")
    assert edited.returncode == 0, edited.stdout + edited.stderr
    lab, home = store / "lab", store / "home"
    assert decrypt(lab / "vault.yml", lab / "vault_pass") == {"vault_haos_owner_password": "edited"}
    assert decrypt(home / "vault.yml", home / "vault_pass") == {}
    assert setups.target(checkout / VAULT) == home / "vault.yml", "the checkout keeps its setup"


def test_a_playbook_run_names_its_setup_and_stops_on_a_broken_one(tmp_path):
    checkout = tree(tmp_path)
    store = tmp_path / "setups"
    assert make(checkout, "vault", "TYPE=new", "NAME=home", f"SETUPS={store}").returncode == 0
    planned = make(checkout, "-n", "check", "TYPE=proxmox")
    assert "-home-check-proxmox.log" in planned.stdout, "the log is named after the setup"
    (store / "home" / "local.yml").unlink()
    stopped = make(checkout, "check", "TYPE=proxmox")
    assert stopped.returncode != 0 and "home, is not whole" in stopped.stderr, stopped.stdout + stopped.stderr
    assert not (checkout / "logs").exists(), "stopped before anything ran"
