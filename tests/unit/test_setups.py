"""Setups (scripts/init/setups.py): each machine's local.yml, vault and vault password kept outside the checkout, the
one a checkout uses linked in; what just vault list, switch and new do, and every refusal.

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
    """A checkout holding what setups.py reads: local.yml.example and the vault's template, and nothing of a setup
    yet."""
    checkout = tmp_path / "checkout"
    (checkout / "group_vars" / "all").mkdir(parents=True)
    for example in (wizard.EXAMPLE, setups.VAULT_TEMPLATE):
        shutil.copy(ROOT / example, checkout / example)
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


def is_template(vault: Path, password_file: Path) -> bool:
    """The vault the template makes: its text whole, comments and all, each key empty."""
    plain = VaultLib([("default", VaultSecret(password_file.read_bytes().strip()))]).decrypt(vault.read_bytes())
    return plain.decode() == (ROOT / setups.VAULT_TEMPLATE).read_text()


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
    assert is_template(setup / "vault.yml", setup / "vault_pass"), "the template, opening with its password"
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
    with pytest.raises(setups.Refused, match="--from current"):
        setups.switch(root, where, "lab")
    with pytest.raises(setups.Refused, match="--from current"):
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
    with pytest.raises(setups.Refused, match="just init writes it"):
        setups.adopt(root, where, "home")
    assert not (where / "home").exists() and not links(root)
    assert (root / VAULT).is_file() and not (root / VAULT).is_symlink(), "the files stay where they were"


def test_a_local_yml_that_sets_nothing_gives_way_to_a_setup(root, where):
    """just init's first run writes local.yml with every setting commented out, and a setup restored after it still
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
    with pytest.raises(setups.Refused, match=r"\.vault_pass is a file of this checkout's own.*switch home finishes"):
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
    assert rows["home"].startswith("*") and "opens, 2 keys set" in rows["home"]
    assert not rows["lab"].startswith("*") and "opens, 0 keys set" in rows["lab"]
    assert "no vault_pass" in rows["old"]
    assert secret not in text and PASSWORD not in text


def test_list_counts_the_keys_a_vault_sets_not_those_left_empty(root, where):
    setup = setups.new(root, where, "home", wizard.commented_example(root))
    assert "opens, 0 keys set" in setups.listing(root, where), "the template sets nothing"
    template = (root / setups.VAULT_TEMPLATE).read_text()
    filled = template.replace('vault_haos_owner_password: ""', "vault_haos_owner_password: owner-password-of-the-test")
    assert filled != template
    (setup / "vault.yml").write_bytes(setups.encrypt(filled, (setup / "vault_pass").read_bytes().strip()))
    assert "opens, 1 key set" in setups.listing(root, where)


def test_list_says_what_a_checkout_without_a_setup_can_do(root, where):
    assert "No setups in" in setups.listing(root, where)
    own_files(root)
    assert "--from current" in setups.listing(root, where)


# ------------------------------------------------------------------------------------------------------ cli ---
@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["switch"], "needs a setup's name"),
        (["new", "--from", "current"], "which needs a name"),
        (["new", "home", "--from", "elsewhere"], "--from takes current alone"),
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
    assert is_template(root / VAULT, root / VAULT_PASS)
    assert not links(root) and stat.S_IMODE((root / VAULT_PASS).stat().st_mode) == 0o600
    assert setups.main(["new"], root=root) == 0 and "nothing to do" in capsys.readouterr().out


def test_the_store_is_never_inside_the_checkout(root, monkeypatch, capsys):
    monkeypatch.setenv("SETUPS", str(root / "setups"))
    assert setups.main(["new", "lab"], root=root) == 1
    assert "inside this checkout" in capsys.readouterr().err
    assert not (root / "setups").exists() and not links(root)


# ----------------------------------------------------------------------------------------------------- just ---
def tree(tmp_path: Path) -> Path:
    """The justfile and what just vault runs, copied into a checkout of their own."""
    checkout = tmp_path / "tree"
    (checkout / "scripts" / "init").mkdir(parents=True)
    (checkout / "group_vars" / "all").mkdir(parents=True)
    (checkout / "vars_plugins").mkdir()
    for name in (
        "justfile",
        "ansible.cfg",
        str(wizard.EXAMPLE),
        str(setups.VAULT_TEMPLATE),
        "vars_plugins/setup_links.py",
        "scripts/run_log.py",
    ):
        shutil.copy(ROOT / name, checkout / name)
    for script in ("init.py", "settings.py", "setups.py"):
        shutil.copy(ROOT / "scripts" / "init" / script, checkout / "scripts" / "init" / script)
    return checkout


# What an outer just (just test unit) exports to every process below it, VENV, and SETUPS, which a shell may export:
# each test gives its own.
OUTER_JUST = {"VENV", "SETUPS"}


def just(checkout: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    """just in the checkout, with this test's virtualenv. VENV goes in the environment: after a recipe's name, VENV=…
    would be one of the recipe's arguments."""
    inherited = {k: v for k, v in os.environ.items() if k not in OUTER_JUST}
    return subprocess.run(
        ["just", *args],
        cwd=checkout,
        env={**inherited, "VENV": sys.prefix, "ANSIBLE_CONFIG": str(checkout / "ansible.cfg"), **env},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def test_just_vault_makes_lists_and_switches_setups(tmp_path):
    checkout = tree(tmp_path)
    store = str(tmp_path / "setups")
    for name in ("home", "lab"):
        made = just(checkout, "vault", "new", name, SETUPS=store)
        assert made.returncode == 0, made.stdout + made.stderr
    listed = just(checkout, "vault", "list", SETUPS=store)
    assert listed.returncode == 0 and "*  lab" in listed.stdout and "  home" in listed.stdout, listed.stdout
    switched = just(checkout, "vault", "switch", "home", SETUPS=store)
    assert switched.returncode == 0 and "uses setup home now" in switched.stdout, switched.stdout + switched.stderr
    assert setups.target(checkout / LOCAL) == Path(store) / "home" / "local.yml"
    plain = just(checkout, "vault", "new", SETUPS=store)
    assert plain.returncode != 0 and "uses setup home" in plain.stderr, "never written through a setup's links"
    nameless = just(checkout, "vault", "switch", SETUPS=store)
    assert nameless.returncode != 0 and "switch needs" in nameless.stderr, nameless.stderr
    both = just(checkout, "vault", "list", "switch", SETUPS=store)
    assert both.returncode != 0 and "just vault list takes no setup" in both.stderr, both.stderr
    unknown = just(checkout, "vault", "lists", SETUPS=store)
    assert unknown.returncode != 0 and "does not match pattern" in unknown.stderr, unknown.stderr


def test_just_vault_edits_another_setups_vault_without_switching(tmp_path):
    checkout = tree(tmp_path)
    store = tmp_path / "setups"
    for name in ("lab", "home"):
        assert just(checkout, "vault", "new", name, SETUPS=str(store)).returncode == 0
    editor = tmp_path / "editor.py"
    editor.write_text("import sys\nopen(sys.argv[1], 'w').write('vault_haos_owner_password: edited\\n')\n")
    edited = just(checkout, "vault", "edit", "lab", SETUPS=str(store), EDITOR=f"{sys.executable} {editor}")
    assert edited.returncode == 0, edited.stdout + edited.stderr
    lab, home = store / "lab", store / "home"
    assert decrypt(lab / "vault.yml", lab / "vault_pass") == {"vault_haos_owner_password": "edited"}
    assert is_template(home / "vault.yml", home / "vault_pass")
    assert setups.target(checkout / VAULT) == home / "vault.yml", "the checkout keeps its setup"


def test_a_playbook_run_names_its_setup_and_stops_on_a_broken_one(tmp_path):
    """The log is named after the setup. A dry run cannot show it: just --dry-run stops at the just a recipe starts
    again, so the run is real, in a tree with no proxmox.yml, where ansible-playbook stops at once."""
    checkout = tree(tmp_path)
    store = tmp_path / "setups"
    assert just(checkout, "vault", "new", "home", SETUPS=str(store)).returncode == 0
    ran = just(checkout, "check", "proxmox")
    assert ran.returncode != 0 and "Setup: home" in ran.stdout, ran.stdout + ran.stderr
    assert "-home-check-proxmox.log" in ran.stdout, "the log is named after the setup"
    assert os.readlink(checkout / "logs" / "latest.log").endswith("-home-check-proxmox.log")
    logs = sorted((checkout / "logs").iterdir())
    (store / "home" / "local.yml").unlink()
    stopped = just(checkout, "check", "proxmox")
    assert stopped.returncode != 0 and "home, is not whole" in stopped.stderr, stopped.stdout + stopped.stderr
    assert "Logging to" not in stopped.stdout and sorted((checkout / "logs").iterdir()) == logs, "stopped first"


def test_just_takes_no_parameter_from_the_environment_or_in_makes_style(tmp_path):
    """A NAME the shell exports (WSL sets it to the machine's name) reaches nothing; a make-style NAME=lab after a
    playbook recipe is refused before anything runs, rather than handed to ansible-playbook as a playbook; and
    --from belongs to just vault new alone."""
    checkout = tree(tmp_path)
    store = tmp_path / "setups"
    made = just(checkout, "vault", "new", SETUPS=str(store), NAME="mybox")
    assert made.returncode == 0, made.stdout + made.stderr
    assert not store.exists() and (checkout / VAULT).is_file() and not links(checkout), "the checkout's own vault"
    refused = just(checkout, "deploy", "proxmox", "NAME=lab")
    assert refused.returncode != 0 and "NAME=lab: just takes no make-style parameter" in refused.stderr, refused.stderr
    assert not (checkout / "logs").exists(), "refused before anything ran"
    stray = just(checkout, "vault", "switch", "lab", "--from", "current")
    assert stray.returncode != 0 and "--from current is just vault new's alone" in stray.stderr, stray.stderr


def test_ansible_playbook_started_without_just_stops_on_a_broken_setup(tmp_path):
    """vars_plugins/setup_links.py, beside the playbooks, which Ansible loads by itself."""
    checkout = tree(tmp_path)
    store = tmp_path / "setups"
    assert just(checkout, "vault", "new", "home", SETUPS=str(store)).returncode == 0
    (checkout / "play.yml").write_text(
        "- hosts: localhost\n  gather_facts: false\n  tasks:\n    - ansible.builtin.debug:\n        msg: ran\n"
    )
    inherited = {k: v for k, v in os.environ.items() if k not in OUTER_JUST}

    def play() -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(Path(sys.prefix) / "bin" / "ansible-playbook"), "-i", "localhost,", "-c", "local", "play.yml"],
            cwd=checkout,
            env={**inherited, "ANSIBLE_CONFIG": str(checkout / "ansible.cfg")},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
        )

    ran = play()
    assert ran.returncode == 0 and "msg: ran" in ran.stdout, ran.stdout + ran.stderr
    (store / "home" / "local.yml").unlink()
    stopped = play()
    output = stopped.stdout + stopped.stderr
    assert stopped.returncode != 0 and "home, is not whole" in output and "msg: ran" not in output, output
