"""just vault's setups: each machine's local.yml, vault and vault password, kept together outside the checkout, and
the one a checkout uses linked in.

A setup is a directory of the store holding three files: local.yml, vault.yml and vault_pass. The store is the
environment's $SETUPS, else $XDG_CONFIG_HOME/homeserver/setups, ~/.config/homeserver/setups without it, and never inside
the checkout, where git would offer a setup's password to a commit. The checkout links the files of the setup it uses in
at the paths Ansible and just read, each a path no commit ever held, since a checkout of an older branch overwrites an
ignored file at a path it tracks: group_vars/all/local.yml, group_vars/all/local.vault.yml and .vault_pass, which
ansible.cfg names. Setups cannot sit side by side in group_vars/all/, where Ansible reads every .yml file, and a link to
a missing file is skipped there without a word, as if there were no settings or no secrets: every playbook run first
checks that the three links point into one setup and that each of its files is there (check; just's playbook recipes,
and vars_plugins/setup_links.py for ansible-playbook started any other way). A checkout whose three paths are its own
files, or missing, uses no setup and works as it always did.

  list                    every setup, the checkout's marked: whether its vault opens, its number of keys (never a
                          value), when each file last changed
  switch NAME             link NAME's files in
  new NAME                a setup: local.yml.example with every setting commented out, an empty vault and a random
                          password, readable by their owner alone; then switch to it
  new NAME --from current this checkout's own three files moved into a new setup, and links left in their place
  new                     the checkout's own vault, what it lacks of it: a random .vault_pass and an empty vault
  check                   which setup a playbook run uses, or stop when its links are broken
  name                    the setup's name alone, for the run's log; nothing for the checkout's own files
  edit NAME               ansible-vault edit on NAME's vault, with NAME's password, without switching

Nothing here deletes a setup or replaces a file of the checkout's own that holds anything: switch and new refuse one,
which new --from current moves into a setup first. A local.yml that sets nothing, as just init first writes it, gives
way, and so does a file the same as the setup's, as a switch cut short leaves it.
"""

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
# Each file of a setup, and the path in the checkout that links to it.
FILES = {
    "local.yml": Path("group_vars/all/local.yml"),
    "vault.yml": Path("group_vars/all/local.vault.yml"),
    "vault_pass": Path(".vault_pass"),
}
# What makes each file in a checkout that uses no setup.
MAKES = {
    "local.yml": "just init writes it",
    "vault.yml": "just vault new makes it and its password",
    "vault_pass": "just vault new makes it and the vault",
}
# Where the vault was committed until 2026-10-20: Ansible would read a vault there beside the setup's.
OLD_VAULT = Path("group_vars/all/vault.yml")
NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
NAME_RULE = "lower-case letters, digits and dashes, such as home or lab"
EMPTY_VAULT = "---\n{}\n"
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*:", re.M)


class Refused(Exception):
    """What will not be done, and why. Nothing was changed."""


def store(root: Path | None = None) -> Path:
    """Where the setups are kept; with root, refused when that is inside the checkout."""
    if os.environ.get("SETUPS"):
        where = Path(os.environ["SETUPS"]).expanduser()
    else:
        where = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "homeserver" / "setups"
    where = Path(os.path.abspath(where))
    if root is not None:
        real, top = resolved(where), resolved(root)
        if real == top or top in real.parents:
            raise Refused(
                f"The setups' store, {shown(where)}, is inside this checkout, where git add would take a setup's "
                "vault_pass, its password in clear, into this public repository: keep it outside, in the default "
                "~/.config/homeserver/setups or SETUPS=<dir>"
            )
    return where


def shown(path: Path) -> str:
    """A path as a person types it, with ~ for the home directory."""
    home, text = str(Path.home()), str(path)
    return "~" + text[len(home) :] if text == home or text.startswith(home + os.sep) else text


def resolved(path: Path) -> Path:
    """Where a path leads, through every link, whether a file is there or not."""
    return Path(os.path.realpath(path))


def names(where: Path) -> list[str]:
    """The setups in the store; a directory that is no setup's name, such as one being written, is not one."""
    if not where.is_dir():
        return []
    return sorted(p.name for p in where.iterdir() if p.is_dir() and NAME.fullmatch(p.name))


def check_name(name: str) -> None:
    if not NAME.fullmatch(name) or len(name) > 40:
        raise Refused(f"{name!r} is no setup's name: a name is {NAME_RULE}, 40 at most")


def write_new(path: Path, data: bytes) -> None:
    """A file that was not there, readable by its owner alone, on the disk before this returns."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


# ------------------------------------------------------------------------------------------------- the vault ---
def decrypt(data: bytes, password: bytes) -> bytes | None:
    """A vault's text, None when the password does not open it: how just vault and just init both read a vault."""
    from ansible.errors import AnsibleError
    from ansible.parsing.vault import VaultLib, VaultSecret

    try:
        return VaultLib([("default", VaultSecret(password))]).decrypt(data)
    except (AnsibleError, ValueError):
        return None


def encrypt(text: str, password: bytes, vault_id: str | None = None) -> bytes:
    """text as a vault, for password; a vault id other than the default goes in its header, as ansible-vault puts it."""
    from ansible.parsing.vault import VaultLib, VaultSecret

    secret = VaultSecret(password)
    return VaultLib([("default", secret)]).encrypt(text.encode(), secret=secret, vault_id=vault_id)


def vault_keys(vault: Path, password_file: Path) -> int | None:
    """How many keys a vault holds, None when it does not open with the password. The keys are counted in the
    decrypted text, never parsed or shown."""
    try:
        plain = decrypt(vault.read_bytes(), password_file.read_bytes().strip())
    except OSError:
        return None
    return None if plain is None else len(_KEY.findall(plain.decode(errors="replace")))


def problems(setup: Path) -> list[str]:
    """What keeps a setup from being used: a file missing, or a vault its password does not open."""
    missing = [file for file in FILES if not (setup / file).is_file()]
    if missing:
        return [f"it has no {' and no '.join(missing)}"]
    if vault_keys(setup / "vault.yml", setup / "vault_pass") is None:
        return ["its vault does not open with its vault_pass"]
    return []


# ------------------------------------------------------------------------------------------- the checkout ---
def target(link: Path) -> Path:
    to = Path(os.readlink(link))
    return to if to.is_absolute() else link.parent / to


def active(root: Path) -> Path | None:
    """The setup the checkout links to; None when it uses files of its own. A setup is known by its own names: the
    vault's or .vault_pass's link leads to a file named vault.yml or vault_pass, as nothing but a setup names them, so a
    link of the person's own, such as .vault_pass to a password file kept elsewhere, is one of the checkout's own files.
    Links are followed to the end and compared there, however they are spelt. Refused when the links are not whole: a
    path missing or a file of the checkout's own beside a setup's links, links into two setups, or a link to a file
    that is gone, which Ansible would pass over without a word."""
    paths = {file: root / rel for file, rel in FILES.items()}
    links = [(f, resolved(paths[f])) for f in ("vault.yml", "vault_pass") if paths[f].is_symlink()]
    marks = [to for f, to in links if to.name == f]
    if not marks:
        gone = [rel for rel in FILES.values() if (root / rel).is_symlink() and not (root / rel).exists()]
        if gone:
            raise Refused(
                "Ansible would run without what is missing, without a word: "
                + "; ".join(f"{rel} links to {shown(resolved(root / rel))}, which is missing" for rel in gone)
                + ". Restore the file it links to, or remove the link."
            )
        return None
    setup = marks[0].parent
    found = []
    for file, path in paths.items():
        if not path.is_symlink():
            what = "a file of this checkout's own" if os.path.lexists(path) else "missing"
            found.append(f"{FILES[file]} is {what}, not a link")
            continue
        to = resolved(path)
        if to != setup / file:
            found.append(f"{FILES[file]} links to {shown(to)}, not to {shown(setup / file)}")
        elif not to.is_file():
            found.append(f"{FILES[file]} links to {shown(to)}, which is missing")
    if found:
        raise Refused(
            f"This checkout's setup, {setup.name}, is not whole, and Ansible would run without what is missing, "
            f"without a word: {'; '.join(found)}. Restore the setup's files from your password manager, or link a "
            f"whole setup in: just vault switch <setup> (just vault list lists them; just vault switch {setup.name} "
            "finishes a switch that was cut short)."
        )
    return setup


def sets_nothing(path: Path) -> bool:
    """A local.yml with no setting, as just init first writes it: the example with every setting commented out. Nothing
    is lost when a setup's link replaces it."""
    try:
        return yaml.safe_load(path.read_text()) in (None, {})
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return False


def own_files(root: Path, setup: Path | None = None) -> list[Path]:
    """The checkout's own files at the three paths that a setup's link would lose: every one but a local.yml that sets
    nothing, and, with setup, one the same as setup's file. A link is not one: replacing it loses nothing."""
    own = []
    for file, rel in FILES.items():
        path = root / rel
        if not os.path.lexists(path) or path.is_symlink():
            continue
        if file == "local.yml" and sets_nothing(path):
            continue
        if setup is not None and (setup / file).is_file() and path.read_bytes() == (setup / file).read_bytes():
            continue
        own.append(rel)
    return own


def refuse_own_files(root: Path, setup: Path | None = None) -> None:
    own = own_files(root, setup)
    if own:
        listed = ", ".join(str(p) for p in own)
        one = len(own) == 1
        raise Refused(
            f"{listed} {'is' if one else 'are'} this checkout's own, and a setup's links would replace "
            f"{'it' if one else 'them'}: make this checkout's files a setup first, just vault new <name> "
            f"--from current, or move {'it' if one else 'them'} out of the checkout"
        )


def refuse_old_vault(root: Path) -> None:
    if os.path.lexists(root / OLD_VAULT):
        raise Refused(
            f"{OLD_VAULT} is where the vault was committed until 2026-10-20, and Ansible would read it beside a "
            "setup's vault: move it first (README.md, Setups and the vault)"
        )


def link(root: Path, setup: Path) -> None:
    """Point each of the checkout's paths at the setup's file: a new link renamed over the path, so the path is never
    missing on the way. Stopped half way, the paths it reached link to the setup and the others keep what they held, the
    same as the setup's files for new --from current: a switch to the same setup finishes it."""
    for file, rel in FILES.items():
        path = root / rel
        tmp = path.with_name(f"{path.name}.setup-tmp")
        if os.path.lexists(tmp):
            tmp.unlink()
        os.symlink(setup / file, tmp)
        os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------- the store ---
def existing(where: Path, name: str) -> Path:
    check_name(name)
    setup = where / name
    if not setup.is_dir():
        raise Refused(
            f"There is no setup {name} in {shown(where)}: just vault list lists them, and just vault new "
            f"{name} makes it"
        )
    return setup


def unused(where: Path, name: str) -> Path:
    check_name(name)
    setup = where / name
    if os.path.lexists(setup):
        raise Refused(f"Setup {name} exists already, in {shown(setup)}: just vault switch {name} uses it")
    return setup


def make_setup(where: Path, name: str, contents: dict[str, bytes]) -> Path:
    """The setup's directory, written whole beside its place and renamed into it, readable by its owner alone."""
    where.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=where))
    try:
        for file, data in contents.items():
            write_new(scratch / file, data)
        # Checked again just before the rename, which would put an empty directory of that name in its place.
        setup = unused(where, name)
        os.rename(scratch, setup)
    except BaseException:
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    fd = os.open(where, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return setup


# ------------------------------------------------------------------------------------------------ commands ---
def switch(root: Path, where: Path, name: str) -> bool:
    """Link setup name in; False when the checkout uses it already."""
    setup = existing(where, name)
    found = problems(setup)
    if found:
        raise Refused(
            f"Setup {name} is not whole: {'; '.join(found)}. A run would go without it, so this checkout keeps the "
            f"setup it has. Restore {shown(setup)} from your password manager."
        )
    try:
        current = active(root)
    except Refused:
        current = None
    if current == resolved(setup):
        return False
    refuse_old_vault(root)
    refuse_own_files(root, setup)
    link(root, setup)
    return True


def new(root: Path, where: Path, name: str, local_text: str) -> Path:
    """A setup of local_text, an empty vault and a random password, which the checkout then uses."""
    unused(where, name)
    refuse_old_vault(root)
    refuse_own_files(root)
    password = secrets.token_hex(32)
    setup = make_setup(
        where,
        name,
        {
            "local.yml": local_text.encode(),
            "vault.yml": encrypt(EMPTY_VAULT, password.encode()),
            "vault_pass": f"{password}\n".encode(),
        },
    )
    link(root, setup)
    return setup


def adoptable(root: Path, opens: bool | None = None) -> None:
    """Refused unless the checkout's three files are its own and whole, so they can become a setup. opens: whether the
    vault opens with .vault_pass, when the caller has read it already."""
    refuse_old_vault(root)
    current = active(root)
    if current is not None:
        raise Refused(f"This checkout links to setup {current.name} already: just vault list lists the setups")
    for file, rel in FILES.items():
        if not (root / rel).is_file():
            raise Refused(f"There is no {rel} to move into a setup: {MAKES[file]}")
    if opens is None:
        opens = vault_keys(root / FILES["vault.yml"], root / FILES["vault_pass"]) is not None
    if not opens:
        raise Refused(
            f"{FILES['vault.yml']} does not open with .vault_pass: restore the .vault_pass that opens it first, so "
            "that the setup is whole"
        )


def adopt(root: Path, where: Path, name: str) -> Path:
    """This checkout's own three files moved into a new setup, and links left in their place. A link of the person's
    own is read through, and the file it leads to is left where it is."""
    unused(where, name)
    adoptable(root)
    setup = make_setup(where, name, {file: (root / rel).read_bytes() for file, rel in FILES.items()})
    link(root, setup)
    return setup


def own_vault(root: Path) -> str:
    """just vault new without a name: what the checkout lacks of a vault of its own, a random .vault_pass readable
    by its owner alone and an empty vault encrypted with it. A vault that opens with .vault_pass is left alone, and one
    that does not is refused, since it may hold the person's secrets with their password lost; so is one at the old,
    committed path, which moves rather than a new one being made beside it, and a setup, whose links this would write
    through. A .vault_pass without a vault is kept, and the vault encrypted with it. Both are encrypted before either
    is written."""
    current = active(root)
    if current is not None:
        raise Refused(
            f"This checkout uses setup {current.name} (just vault list): just vault new <name> makes another."
        )
    vault, password_file = root / FILES["vault.yml"], root / FILES["vault_pass"]
    if vault.is_file() and password_file.is_file() and vault_keys(vault, password_file) is not None:
        return f"{FILES['vault.yml']} already opens with .vault_pass: nothing to do."
    if os.path.lexists(vault):
        raise Refused(
            f"{FILES['vault.yml']} does not open with .vault_pass, so it may hold your secrets: restore the "
            ".vault_pass that opens it rather than replace it. To start afresh all the same, move the file out of the "
            "repository first."
        )
    refuse_old_vault(root)
    if os.path.lexists(password_file):
        password = password_file.read_bytes().strip()
        if not password:
            raise Refused(
                ".vault_pass is empty, and no vault is encrypted with no password: delete it, or restore yours"
            )
        made = None
    else:
        password = secrets.token_hex(32).encode()
        made = password + b"\n"
    data = encrypt(EMPTY_VAULT, password)
    if made:
        write_new(password_file, made)
    write_new(vault, data)
    return f"Made {FILES['vault.yml']}, encrypted with .vault_pass. Keep a copy of both in your password manager."


def edit(where: Path, name: str) -> int:
    """ansible-vault edit on another setup's vault, with that setup's password alone."""
    setup = existing(where, name)
    for file in ("vault.yml", "vault_pass"):
        if not (setup / file).is_file():
            raise Refused(f"Setup {name} has no {file}: restore {shown(setup)} from your password manager")
    ansible_vault = Path(sys.executable).parent / "ansible-vault"
    if not ansible_vault.is_file():
        raise Refused(f"ansible-vault is not in {sys.prefix}: activate the project's virtualenv, or pass VENV=<path>")
    env = {**os.environ, "ANSIBLE_VAULT_PASSWORD_FILE": str(setup / "vault_pass")}
    return subprocess.call([str(ansible_vault), "edit", str(setup / "vault.yml")], env=env)


def check(root: Path) -> str:
    """The line a playbook run starts with; Refused when the links are broken, or when the vault's old, committed copy
    is back beside the vault."""
    setup = active(root)
    if os.path.lexists(root / OLD_VAULT) and os.path.lexists(root / FILES["vault.yml"]):
        raise Refused(
            f"{OLD_VAULT}, where the vault was committed until 2026-10-20, is back beside {FILES['vault.yml']}: a "
            "branch from before then is checked out, and its commits hold that copy. Ansible reads group_vars/all in "
            "name order, so its keys would win over the vault's. Check out a branch from after then, which takes it "
            "away again, or move it out of the checkout (README.md, Setups and the vault)."
        )
    return f"Setup: {setup.name} ({shown(setup)})" if setup else "Setup: this checkout's own files"


def changed(path: Path) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)) if path.is_file() else "-"


def vault_state(setup: Path) -> str:
    missing = [file for file in ("vault.yml", "vault_pass") if not (setup / file).is_file()]
    if missing:
        return f"no {' and no '.join(missing)}"
    keys = vault_keys(setup / "vault.yml", setup / "vault_pass")
    if keys is None:
        return "does not open"
    return f"opens, {keys} key{'' if keys == 1 else 's'}"


def listing(root: Path, where: Path) -> str:
    broken = None
    try:
        current = active(root)
    except Refused as e:
        current, broken = None, str(e)
    found = names(where)
    lines = []
    if found:
        rows = [("", "NAME", "VAULT", "LOCAL.YML", "VAULT.YML", "VAULT_PASS")]
        for name in found:
            setup = where / name
            mark = "*" if current == resolved(setup) else ""
            rows.append((mark, name, vault_state(setup), *(changed(setup / file) for file in FILES)))
        widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
        lines.append(f"Setups in {shown(where)}, * the one this checkout uses:")
        for row in rows:
            lines.append("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)).rstrip())
    else:
        lines.append(f"No setups in {shown(where)}: just vault new <name> makes one.")
    if broken:
        lines.append(broken)
    elif current and not any(current == resolved(where / name) for name in found):
        lines.append(f"This checkout uses setup {current.name}, in {shown(current.parent)}.")
    elif not current and own_files(root):
        lines.append(
            "This checkout uses its own local.yml, vault and .vault_pass, in no setup: just vault new "
            "<name> --from current makes them one."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("list", "check", "name"):
        commands.add_parser(command)
    for command in ("switch", "edit", "new"):
        commands.add_parser(command).add_argument("name", nargs="?", default="")
    commands.choices["new"].add_argument("--from", dest="source", default="")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            print(check(root))
            return 0
        if args.command == "name":
            try:
                setup = active(root)
            except Refused:
                setup = None  # check, which runs next, says why
            if setup and NAME.fullmatch(setup.name):
                print(setup.name)
            return 0
        if args.command == "new" and not args.name and not args.source:
            print(own_vault(root))
            return 0
        where = store(root)
        if args.command == "list":
            print(listing(root, where))
        elif args.command == "edit":
            return edit(where, args.name)
        elif args.command == "switch":
            if not args.name:
                raise Refused("just vault switch needs a setup's name: just vault list lists them")
            if switch(root, where, args.name):
                print(f"This checkout uses setup {args.name} now ({shown(where / args.name)}).")
            else:
                print(f"This checkout uses setup {args.name} already: nothing to do.")
        elif args.source not in ("", "current"):
            raise Refused(f"--from takes current alone, not {args.source}: just vault new <name> --from current")
        elif not args.name:
            raise Refused(
                "--from current makes this checkout's own files a setup, which needs a name: just vault new "
                "<name> --from current"
            )
        elif args.source:
            setup = adopt(root, where, args.name)
            print(
                f"Moved this checkout's local.yml, vault and .vault_pass into setup {args.name} ({shown(setup)}), and "
                "linked them back. Keep a copy of that directory in your password manager."
            )
        else:
            import init

            setup = new(root, where, args.name, init.commented_example(root))
            print(
                f"Made setup {args.name} ({shown(setup)}): local.yml with every setting commented out, an empty vault "
                "and its password, readable by you alone. This checkout uses it now, and just init asks for its "
                "settings. Keep a copy of the directory in your password manager."
            )
    except Refused as e:
        print(e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
