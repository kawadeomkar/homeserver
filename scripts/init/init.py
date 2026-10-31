"""just init: ask for the settings a deployment needs, write them, and say what is still missing.

Settings go in group_vars/all/local.yml and secrets in the vault (group_vars/all/local.vault.yml, opened with
.vault_pass), both git-ignored, one top-level key at a time, so the rest of each file and its comments stay as they are.
Each answer is written at once: stopping halfway keeps what was answered. A setting already set is shown, and Enter
keeps it, so just init can be run again to change a setting or fill in one that was skipped. Secrets are never shown,
only whether they are set.

A new local.yml is local.yml.example with every setting commented out, so the playbooks read nothing from it until
a setting is answered. One an older just init copied from the example loses the example's own values the same way,
and keeps whatever was changed.

A plain run asks for the required and recommended settings and the popular optional ones; --full (just init
--full) asks for every one. It ends with a summary: what is still required (settings left unset, what the
playbooks' own checks would refuse, files the cluster needs, tools), then the steps only done by hand.

Its first question is the setup, when there is one to choose (scripts/init/setups.py): the three files of one machine
kept outside the checkout and linked in, which the answers then go into. Enter keeps the setup the checkout uses; a
new name makes one and switches to it. A checkout with its own files is offered to keep them as a setup.

Without a terminal it asks nothing and prints the summary alone.
"""

import argparse
import getpass
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import settings as cfg
import setups
import yaml

ROOT = Path(__file__).resolve().parents[2]
LOCAL, VAULT, VAULT_PASS = (setups.FILES[f] for f in ("local.yml", "vault.yml", "vault_pass"))
EXAMPLE = Path("group_vars/all/local.yml.example")
# Where the vault was committed until 2026-10-20: a checkout from before moves it (README.md, Setups and the vault).
OLD_VAULT = setups.OLD_VAULT
INVENTORY = Path("inventory")
VALIDATE = Path("scripts/init/validate.yml")

KEEP = object()  # an answer that leaves the setting as it is
CLEAR = object()  # an answer that unsets it

_TOP = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*:(?:\s|$)")
_COMMENTED = re.compile(r"#\s?([A-Za-z_][A-Za-z0-9_]*)\s*:(?:\s|$)")


# -------------------------------------------------------------------------------------------------- YAML text ---
def _flow(value: Any) -> str:
    text = yaml.safe_dump(value, default_flow_style=True, width=1 << 16, allow_unicode=True, sort_keys=False)
    return text.strip().removesuffix("...").strip()


def render(key: str, value: Any) -> list[str]:
    """A setting's lines: a mapping one entry per line, anything else on one line, as local.yml.example writes them."""
    if isinstance(value, dict) and value:
        return [f"{key}:", *(f"  {_flow({k: v})[1:-1]}" for k, v in value.items())]
    if value == "":
        return [f'{key}: ""']  # as the examples write it, where YAML's dump would write ''
    return [_flow({key: value})[1:-1]]


class EditError(Exception):
    """A change that would not leave the file as asked, so it was not made."""


class YamlKeys:
    """A YAML mapping's text, changed one top-level key at a time; every other line stays as it was."""

    def __init__(self, text: str):
        self.lines = text.splitlines()

    @property
    def text(self) -> str:
        return "\n".join(self.lines).rstrip("\n") + "\n"

    def values(self) -> dict:
        data = yaml.safe_load(self.text)
        if data is None:
            return {}
        if not isinstance(data, dict):
            raise ValueError("it is not a mapping of settings")
        return data

    def _continues(self, line: str) -> bool:
        return line.startswith((" ", "\t", "- ")) or line == "-"

    def block(self, key: str) -> tuple[int, int] | None:
        """The lines a top-level key spans: its own and every indented one after it, with the blank lines and
        comments between them; a comment after its last indented line belongs to what follows."""
        for i, line in enumerate(self.lines):
            m = _TOP.match(line)
            if m and m.group(1) == key:
                end = j = i + 1
                while j < len(self.lines):
                    if self._continues(self.lines[j]):
                        end = j = j + 1
                    elif not self.lines[j].strip() or self.lines[j].startswith("#"):
                        j += 1
                    else:
                        break
                return i, end
        return None

    def _to_block_style(self) -> None:
        """A mapping written in flow style, such as the {} just vault new encrypted before it wrote the template, as
        one line per key: a key added after a flow mapping would not parse."""
        start = next(
            (i for i, line in enumerate(self.lines) if line.strip() and not line.startswith(("#", "---"))), None
        )
        if start is not None and self.lines[start].startswith("{"):
            values = self.values()
            self.lines = self.lines[:start] + [line for k, v in values.items() for line in render(k, v)]

    def _edit(self, key: str, change, expected: dict) -> None:
        """Make the change, or refuse it when the file would then read as anything but expected: a key written
        twice, say, whose last copy YAML keeps."""
        before = list(self.lines)
        change()
        try:
            after = self.values()
        except (yaml.YAMLError, ValueError):
            after = None
        if after != expected:
            self.lines = before
            raise EditError(f"{key} cannot be changed in place without changing other settings too")

    def commented(self, key: str) -> tuple[int, int] | None:
        """A commented-out example of the key (# key: value), with its indented commented lines."""
        for i, line in enumerate(self.lines):
            m = _COMMENTED.match(line)
            if m and m.group(1) == key:
                end = i + 1
                while end < len(self.lines) and re.match(r"#\s{2,}\S", self.lines[end]):
                    end += 1
                return i, end
        return None

    def set(self, key: str, value: Any, order: list[str]) -> None:
        """In its place: over the key, or over its commented example, or after the key before it in order."""
        self._to_block_style()
        self._edit(key, lambda: self._put(key, render(key, value), order), {**self.values(), key: value})

    def _put(self, key: str, new: list[str], order: list[str]) -> None:
        span = self.block(key) or self.commented(key)
        if span:
            self.lines[span[0] : span[1]] = new
            return
        if key in order:
            for prev in reversed(order[: order.index(key)]):
                span = self.block(prev)
                if span:
                    self.lines[span[1] : span[1]] = new
                    return
        if self.lines and self.lines[-1].strip():
            self.lines.append("")
        self.lines += new

    def comment_out(self, key: str) -> bool:
        """The key's lines as comments (# key: value), as local.yml.example shows a setting it leaves unset."""
        span = self.block(key)
        if span:

            def comment() -> None:
                self.lines[span[0] : span[1]] = [
                    f"# {line}" if line.strip() and not line.startswith("#") else line
                    for line in self.lines[span[0] : span[1]]
                ]

            self._edit(key, comment, {k: v for k, v in self.values().items() if k != key})
        return bool(span)

    def unset(self, key: str) -> bool:
        span = self.block(key)
        if span:

            def drop() -> None:
                del self.lines[span[0] : span[1]]

            self._edit(key, drop, {k: v for k, v in self.values().items() if k != key})
        return bool(span)


def temp_copy(path: Path) -> Path:
    """The copy write_file writes before it takes the file's place. Its name keeps the file's own (.vault_pass's
    copy is .vault_pass.init-tmp), so .gitignore and gitleaks keep it out of git as they do the file."""
    return path.with_name(f"{path.name}.init-tmp")


def write_file(path: Path, data: bytes, mode: int) -> None:
    """Written whole or not at all, readable by its owner alone unless it already was by more. The copy is made anew,
    so it never takes an older one's mode, and removed when the write fails or is interrupted. A link is followed, so
    a setup's file is written, beside it, never the checkout's link to it replaced."""
    path = Path(os.path.realpath(path))
    tmp = temp_copy(path)
    tmp.unlink(missing_ok=True)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def commented_example(root: Path) -> str:
    """local.yml.example with every setting commented out, as a new local.yml starts: the playbooks read nothing from
    it until a setting is answered."""
    doc = YamlKeys((root / EXAMPLE).read_text())
    for key in doc.values():
        doc.comment_out(key)
    return doc.text


def has_value(value: Any) -> bool:
    """Set to something, in some part: not empty."""
    if isinstance(value, dict):
        return any(has_value(v) for v in value.values())
    if isinstance(value, list):
        return any(has_value(v) for v in value)
    return cfg.is_set(value)


def show(value: Any) -> str:
    if value is None:
        return "not set"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return ", ".join(f"{k}={show(v) if not isinstance(v, str) else v}" for k, v in value.items())
    if isinstance(value, list):
        return ", ".join(_flow(v) if isinstance(v, dict) else str(v) for v in value)
    return str(value)


# ------------------------------------------------------------------------------------------------------ vault ---
class Vault:
    """group_vars/all/local.vault.yml, decrypted in memory with .vault_pass and encrypted again whenever it changes."""

    def __init__(self, root: Path):
        self.path = root / VAULT
        self.password_file = root / VAULT_PASS
        self.root = root
        self.doc: YamlKeys | None = None
        self.state = "unread"
        self.changed = False

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    def open(self) -> None:
        self.doc = None
        if not self.password_file.is_file():
            self.state = "no password"
            return
        if not self.path.is_file():
            self.state = "not moved" if (self.root / OLD_VAULT).is_file() else "missing"
            return
        from ansible.parsing.vault import parse_vaulttext_envelope

        password = self.password_file.read_bytes().strip()
        raw = self.path.read_bytes()
        plain = setups.decrypt(raw, password)
        if plain is None:
            self.state = "does not open"
            return
        self._password = password
        self._vault_id = parse_vaulttext_envelope(raw)[3]
        self.doc = YamlKeys(plain.decode())
        self.state = "open"

    def opens_with(self, password: str) -> bool:
        return self.path.is_file() and setups.decrypt(self.path.read_bytes(), password.encode()) is not None

    def get(self, key: str) -> Any:
        return self.doc.values().get(key) if self.doc else None

    def set(self, key: str, value: Any, order: list[str]) -> None:
        self.doc.set(key, value, order)
        self._save()

    def _save(self) -> None:
        data = setups.encrypt(self.doc.text, self._password, self._vault_id)
        write_file(self.path, data, stat.S_IMODE(self.path.stat().st_mode))
        self.changed = True


# ---------------------------------------------------------------------------------------------------- console ---
class Console:
    """The terminal. topic names what is asked, for the tests' scripted console; the terminal ignores it."""

    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def ask(self, prompt: str, topic: str) -> str:
        return input(prompt)

    def secret(self, prompt: str, topic: str) -> str:
        return getpass.getpass(prompt)


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False, **kwargs)


def random_mac(taken: set[str]) -> str:
    while True:
        mac = "02:" + ":".join(f"{b:02x}" for b in secrets.token_bytes(5))
        if mac not in taken:
            return mac


# ------------------------------------------------------------------------------------------------------- init ---
class Init:
    def __init__(self, root: Path, console: Console, *, full: bool, interactive: bool):
        self.root = root
        self.console = console
        self.full = full
        self.interactive = interactive
        self.order = [s.key for s in cfg.SETTINGS]
        self.local_path = root / LOCAL
        self.example = YamlKeys((root / EXAMPLE).read_text()).values()
        self.load()
        self.examples_left: list[str] = []  # local.yml.example's values local.yml still holds on disk
        self.vault = Vault(root)
        self.setup: Path | None = None  # the setup the checkout links to; None for files of its own
        self.broken: str | None = None  # why the setup's links are not whole, which stops everything else
        self.ssh: tuple[bool, str] | None = None
        self.offered_macs: set[str] = set()
        self.chosen: dict[str, bool] = {}  # what the person said the setup has, before its settings exist

    # ---------------------------------------------------------------------------------------------- values ---
    @property
    def values(self) -> dict:
        return self.local.values()

    def current(self, s: cfg.Setting) -> Any:
        return self.vault.get(s.key) if s.secret else self.values.get(s.key)

    def present(self, section: str) -> bool:
        """Whether the section is on, worked out from local.yml as the playbooks work it out."""
        v = self.values
        if section == "proxmox":
            return True
        if section == "nas":
            return has_value(v.get("truenas_static_address")) or has_value(v.get("truenas_bootstrap_address"))
        if section == "remote":
            enabled = v.get("tailscale_enabled")
            return enabled is True if enabled is not None else has_value(v.get("tailscale_advertise_routes"))
        return has_value(v.get(cfg.SWITCHES[section][1]))

    def on(self, section: str) -> bool:
        return self.chosen.get(section, self.present(section))

    def needed(self, s: cfg.Setting) -> str:
        if s.required_without_nas and not self.on("nas"):
            return "required"
        return s.need

    def applies(self, s: cfg.Setting) -> bool:
        """Whether the setting means anything in this setup: with or without a NAS as it needs, its lead set."""
        if s.nas is not None and s.nas != self.on("nas"):
            return False
        if s.after:
            lead = self.values.get(s.after) if not cfg.BY_KEY[s.after].secret else self.vault.get(s.after)
            if s.after == "flux_sync_ref":
                return has_value(lead) and lead != cfg.DEFAULTS["flux_sync_ref"]
            return has_value(lead)
        return True

    def asked(self, s: cfg.Setting) -> bool:
        return self.full or self.needed(s) in ("required", "recommended") or s.popular

    def load(self) -> None:
        """local.yml as it is, or local.yml.example for a new one."""
        source = self.local_path if self.local_path.is_file() else self.root / EXAMPLE
        self.local = YamlKeys(source.read_text())

    def save_local(self) -> None:
        mode = stat.S_IMODE(self.local_path.stat().st_mode) if self.local_path.exists() else 0o600
        self.local.values()  # never write a file YAML cannot read back
        write_file(self.local_path, self.local.text.encode(), mode)

    def apply(self, s: cfg.Setting, answer: Any) -> None:
        if answer is KEEP or (answer is not CLEAR and answer == self.current(s)):
            return  # unchanged: the file keeps its own way of writing it
        try:
            if s.secret:
                if answer is not CLEAR:
                    self.vault.set(s.key, answer, self.order)
                elif has_value(self.current(s)):
                    # Emptied, as just vault new writes it, so its line stays under its comment.
                    self.vault.set(s.key, "", self.order)
                return
            if answer is CLEAR:
                if self.local.unset(s.key):
                    self.save_local()
            else:
                self.local.set(s.key, answer, self.order)
                self.save_local()
        except EditError as e:
            where = "the vault: just vault" if s.secret else str(LOCAL)
            self.console.say(f"  Not saved: {e}. Change it by hand in {where}.")

    def unset_local(self, key: str) -> bool:
        """Unset a key in local.yml, in memory: False, and saying so, when it cannot be done in place."""
        try:
            return self.local.unset(key)
        except EditError as e:
            self.console.say(f"  Not removed: {e}. Remove it by hand from {LOCAL}.")
            return False

    # -------------------------------------------------------------------------------------------- asking ---
    def yes_no(self, question: str, default: bool, topic: str) -> bool:
        hint = "Y/n" if default else "y/N"
        while True:
            answer = self.console.ask(f"{question} [{hint}] ", topic).strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes", "n", "no"):
                return answer.startswith("y")
            self.console.say("  Answer y or n.")

    def header(self, s: cfg.Setting) -> None:
        tag = self.needed(s) + (", kept in the vault" if s.secret else "")
        self.console.say(f"\n{s.key}  ({tag})")
        self.console.say(f"  {s.label}")

    def ask_value(
        self,
        s: cfg.Setting,
        now: Any,
        suggestion: str | None,
        parse,
        *,
        part: str = "",
        label: str = "",
        secret: bool = False,
    ):
        """Ask until the answer is usable: KEEP, CLEAR or the value. part names a part of a composite setting."""
        if secret:
            shown = "set" if has_value(now) else "not set"
        elif not has_value(now) and s.key in cfg.DEFAULTS and not part:
            shown = f"not set (the playbooks use {show(cfg.DEFAULTS[s.key])})"
        else:
            shown = show(now)
        if has_value(now):
            hint = "Enter keeps it, - clears it"
        elif suggestion is not None:
            hint = f"Enter takes {suggestion}, - leaves it unset"
        else:
            hint = "Enter leaves it unset"
        if s.help:
            hint += ", ? explains"
        if secret:
            hint += " (what you type is hidden)"
        lead = f"  {label or part}: " if part else "  "
        prompt = f"{lead}now {shown}. {hint}\n  > "
        topic = f"{s.key}/{part}" if part else s.key
        while True:
            raw = (self.console.secret if secret else self.console.ask)(prompt, topic)
            answer = raw.strip()
            if answer == "?":
                self.console.say(
                    textwrap.fill(s.help or "No more to say.", 110, initial_indent="  ", subsequent_indent="  ")
                )
                continue
            if answer == "-":
                return CLEAR
            if not answer:
                if has_value(now):
                    return KEEP
                return parse(suggestion) if suggestion is not None else KEEP
            try:
                return parse(raw if secret else answer)
            except ValueError as e:
                self.console.say(f"  {e}. Try again, or Enter to leave it.")

    def ask_setting(self, s: cfg.Setting) -> None:
        self.header(s)
        if s.secret:
            if not self.vault.is_open:
                self.console.say(f"  Skipped: the vault {self.vault_problem()}.")
                return
            self.apply(s, self.ask_value(s, self.current(s), None, lambda t: cfg.parse(s, t), secret=True))
        elif s.kind == "bool":
            self.ask_bool(s)
        elif s.kind == "app_list":
            self.ask_app_list(s)
        elif s.kind in ("node_map_ipv4", "node_map_mac"):
            self.ask_node_map(s)
        elif s.kind == "node_sizes":
            self.ask_node_sizes(s)
        elif s.kind == "haos_size":
            self.ask_parts(s, ("cores", "memory_mib", "disk_gib"), cfg.DEFAULTS["haos_size"])
        elif s.kind == "pool_disks":
            self.ask_pool_disks(s)
        elif not (s.key == "truenas_api_cert_sha256" and self.offer_certificate(s)):
            now = self.current(s)
            if s.key == "flux_sync_url" and not has_value(now):
                self.origin_hint()
            suggestion = None if has_value(now) else self.suggest(s)
            self.apply(s, self.ask_value(s, now, suggestion, lambda t: cfg.parse(s, t)))

    def ask_bool(self, s: cfg.Setting) -> None:
        def parse(text: str) -> bool:
            if text.strip().lower() in ("y", "yes", "true"):
                return True
            if text.strip().lower() in ("n", "no", "false"):
                return False
            raise ValueError("answer y or n")

        self.console.say("  Answer y or n.")
        self.apply(s, self.ask_value(s, self.current(s), None, parse))

    def ask_app_list(self, s: cfg.Setting) -> None:
        """kubernetes_apps, checked against the groups the playbooks read."""
        groups = yaml.safe_load((self.root / "group_vars/all/kubernetes.yml").read_text())["kubernetes_app_groups"]
        self.console.say(f"  The groups: {', '.join(groups)}.")

        def parse(text: str) -> list[str]:
            entries = cfg.parse(s, text)
            unknown = [e for e in entries if e not in groups]
            if unknown:
                raise ValueError(f"no such group: {unknown[0]}")
            return entries

        self.apply(s, self.ask_value(s, self.current(s), None, parse))

    def ask_node_map(self, s: cfg.Setting) -> None:
        now = self.current(s) if isinstance(self.current(s), dict) else {}
        kind = "ipv4" if s.kind == "node_map_ipv4" else "mac"
        out = {k: v for k, v in now.items() if k not in s.fields}  # a node the cluster lacks: left for its check
        for node in s.fields:
            value = now.get(node)
            suggestion = None
            if kind == "mac" and not has_value(value):
                suggestion = random_mac(self.taken_macs())
                self.offered_macs.add(suggestion)
            answer = self.ask_value(s, value, suggestion, cfg.PARSERS[kind], part=node)
            if answer is KEEP and has_value(value):
                out[node] = value
            elif answer not in (KEEP, CLEAR):
                out[node] = answer
        self.apply(s, out or CLEAR)

    def ask_parts(
        self, s: cfg.Setting, parts: tuple[str, ...], defaults: dict, now: dict | None = None, node: str = ""
    ) -> dict:
        """A mapping of whole numbers, part by part; parts left unset keep the playbooks' defaults."""
        whole = now is None
        now = (self.current(s) if isinstance(self.current(s), dict) else {}) if whole else now
        out = {k: v for k, v in now.items() if k not in parts}
        for part in parts:
            value = now.get(part)
            shown_default = defaults.get(part)
            label = f"{part} (default {shown_default})" if shown_default is not None else part
            if part in cfg.OPTIONAL_DISKS:
                label += ", 0 for none"
            parse = cfg.PARSERS["size_or_none" if part in cfg.OPTIONAL_DISKS else "positive_int"]
            answer = self.ask_value(s, value, None, parse, part=f"{node}.{part}" if node else part, label=label)
            if answer is KEEP and value is not None:
                out[part] = value
            elif answer not in (KEEP, CLEAR):
                out[part] = answer
        if whole:
            self.apply(s, out or CLEAR)
        return out

    def ask_node_sizes(self, s: cfg.Setting) -> None:
        now = self.current(s) if isinstance(self.current(s), dict) else {}
        shape = {n["name"]: n for n in self.node_shape()}
        nas = self.on("nas")
        if not nas:
            self.console.say("  Without a NAS, give every one of the worker's sizes: the defaults fit the home host.")
        out = {k: v for k, v in now.items() if k not in cfg.NODES}
        for node in (cfg.WORKER, cfg.CONTROL_PLANE):
            parts = tuple(p for p in cfg.NODE_SIZE_FIELDS[node] if not (nas and p == "media_disk_gib"))
            defaults = {} if (node == cfg.WORKER and not nas) else shape.get(node, {})
            self.console.say(f"  {node}:")
            sizes = self.ask_parts(s, parts, defaults, now.get(node) or {}, node=node)
            if sizes:
                out[node] = sizes
        self.apply(s, out or CLEAR)

    def ask_pool_disks(self, s: cfg.Setting) -> None:
        now = self.current(s) if isinstance(self.current(s), dict) else {}
        v = self.values
        pools = [v.get("truenas_pool_persistent") or "persistent", v.get("truenas_pool_ephemeral") or "ephemeral"]
        out = {k: x for k, x in now.items() if k not in pools}
        for pool in pools:
            value = now.get(pool)
            answer = self.ask_value(s, value, None, lambda t: [x for x in re.split(r"[,\s]+", t) if x], part=pool)
            if answer is KEEP and has_value(value):
                out[pool] = value
            elif answer not in (KEEP, CLEAR):
                out[pool] = answer
        self.apply(s, out or CLEAR)

    # ------------------------------------------------------------------------------------------ suggestions ---
    def taken_macs(self) -> set[str]:
        v = self.values
        macs = set(self.offered_macs)
        if isinstance(v.get("kubernetes_node_macs"), dict):
            macs |= {str(m) for m in v["kubernetes_node_macs"].values()}
        if v.get("haos_mac"):
            macs.add(str(v["haos_mac"]))
        return macs

    def suggest(self, s: cfg.Setting) -> str | None:
        v = self.values
        static = v.get("truenas_static_address")
        if s.key == "truenas_gateway" and has_value(static):
            return str(next(ipaddress.IPv4Interface(static).network.hosts()))
        if s.key == "truenas_nameservers" and has_value(v.get("truenas_gateway")):
            return str(v["truenas_gateway"])
        if s.key == "tailscale_advertise_routes":
            if has_value(static):
                return str(ipaddress.IPv4Interface(static).network)
            if has_value(v.get("proxmox_address")):
                return str(ipaddress.IPv4Interface(f"{v['proxmox_address']}/24").network)
        lb = v.get("kubernetes_lb_range")
        if s.key in ("kubernetes_lb_traefik_ip", "kubernetes_lb_frigate_ip") and has_value(lb):
            lb = str(lb)
            first = ipaddress.IPv4Address(lb.split("-")[0]) if "-" in lb else next(ipaddress.IPv4Network(lb).hosts())
            return str(first + (0 if s.key == "kubernetes_lb_traefik_ip" else 1))
        if s.key == "haos_mac":
            mac = random_mac(self.taken_macs())
            self.offered_macs.add(mac)
            return mac
        return None

    def origin_url(self) -> str | None:
        """git's origin as an https:// address, without the user name or token it may carry."""
        url = run(["git", "remote", "get-url", "origin"], cwd=self.root).stdout.strip()
        m = re.fullmatch(r"git@([^:/]+):(.+?)(?:\.git)?", url)
        if m:
            return f"https://{m.group(1)}/{m.group(2)}.git"
        try:
            parts = urlsplit(url)
            host = parts.hostname and parts.hostname + (f":{parts.port}" if parts.port else "")
        except ValueError:
            return None
        if parts.scheme == "ssh" and host:
            return f"https://{parts.hostname}{parts.path.removesuffix('.git')}.git"
        if parts.scheme == "https" and host:
            return urlunsplit(("https", host, parts.path, "", ""))
        return None

    def origin_hint(self) -> None:
        """Say what git's origin is, for the person to type: Enter never takes it, since a clone's origin is the
        repository it was cloned from, which Flux would then apply with cluster-admin rights."""
        url = self.origin_url()
        if url:
            self.console.say(
                f"  git's origin here is {url}. Type it if that repository is yours: a fork syncs its own."
            )

    def offer_certificate(self, s: cfg.Setting) -> bool:
        """Read the certificate the NAS presents, and offer its fingerprint; False when it was not read."""
        if has_value(self.current(s)):
            return False
        v = self.values
        hosts = [
            str(v[k]).split("/")[0]
            for k in ("truenas_static_address", "truenas_bootstrap_address")
            if has_value(v.get(k))
        ]
        if not hosts or not self.yes_no("  Read the fingerprint the NAS presents now?", True, "read-certificate"):
            return False
        for host in hosts:
            try:
                pem = ssl.get_server_certificate((host, 443), timeout=8)
            except OSError as e:
                self.console.say(f"  {host}: {e}")
                continue
            digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest()
            fingerprint = ":".join(f"{b:02X}" for b in digest)
            self.console.say(f"  {host} presents {fingerprint}")
            self.console.say("  Compare it with System -> Certificates in the TrueNAS web UI before taking it.")
            self.apply(s, self.ask_value(s, None, fingerprint, lambda t: cfg.parse(s, t)))
            return True
        return False

    # ---------------------------------------------------------------------------------------------- sections ---
    def switch(self, section: str) -> bool:
        question, key = cfg.SWITCHES[section]
        on = self.yes_no(f"\n{question}", self.present(section), f"switch:{section}")
        if on:
            if cfg.BY_KEY[key].kind == "bool":
                self.apply(cfg.BY_KEY[key], True)
            return True
        keys = [s.key for s in cfg.SETTINGS if s.section == section and not s.secret and s.key in self.values]
        real = [k for k in keys if has_value(self.values[k])]
        if real and not self.yes_no(
            f"  local.yml sets {', '.join(real)}. Delete them, so the playbooks see no {cfg.SECTIONS[section]}?",
            False,
            f"clear:{section}",
        ):
            self.console.say("  Kept as they are, so the playbooks still see it: its settings come next.")
            return True
        removed = [key for key in keys if self.unset_local(key)]
        if removed:
            self.save_local()
        return False

    def ask_section(self, section: str) -> None:
        self.console.say(f"\n{'-' * 8} {cfg.SECTIONS[section]} {'-' * (100 - len(cfg.SECTIONS[section]))}")
        for s in cfg.SETTINGS:
            if s.section != section or (s.kind == "bool" and s.key == cfg.SWITCHES.get(section, ("", ""))[1]):
                continue  # a yes/no switch was answered already
            if self.applies(s) and self.asked(s):
                self.ask_setting(s)
                if s.key == "proxmox_address" and has_value(self.current(s)):
                    self.offer_ssh()
            elif not s.secret and not self.applies(s):  # a secret no setting reads does no harm in the vault
                self.offer_removal(s)
        if section == "cluster":
            self.cluster_files()

    def unread(self, s: cfg.Setting) -> str:
        """Why this setup does not read the setting, which applies() says."""
        if s.nas is not None and s.nas != self.on("nas"):
            return f"only a setup {'with' if s.nas else 'without'} a NAS reads it"
        if s.after == "flux_sync_ref":
            return "it is read only with flux_sync_ref naming a branch other than main"
        return f"it is read only with {s.after} set"

    def offer_removal(self, s: cfg.Setting) -> None:
        """A setting this setup does not read: the playbooks refuse some (vm_backup_storage with a NAS, an alert
        address without a mail server), and just init would never ask for it again, so it offers to delete it."""
        if s.key not in self.values:
            return
        value = self.values[s.key]
        if has_value(value) and not self.yes_no(
            f"\n{s.key} is set ({show(value)}), but {self.unread(s)}. Delete it?", True, f"remove:{s.key}"
        ):
            return
        if self.unset_local(s.key):
            self.save_local()

    def offer_ssh(self) -> None:
        address = self.values["proxmox_address"]
        if self.yes_no(f"  Check that this machine logs in to {address} as root over SSH now?", True, "ssh"):
            result = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", f"root@{address}", "true"])
            reason = (result.stderr.strip().splitlines() or ["no reason given"])[-1]
            self.ssh = (result.returncode == 0, reason)
            self.console.say("  Root SSH works." if result.returncode == 0 else f"  It does not: {reason}")

    def talos_dir(self) -> Path:
        return Path(
            os.path.expanduser(
                self.values.get("kubernetes_talos_config_dir") or cfg.DEFAULTS["kubernetes_talos_config_dir"]
            )
        )

    def age_key(self) -> Path:
        return Path(os.path.expanduser(self.values.get("sops_age_key_file") or cfg.DEFAULTS["sops_age_key_file"]))

    def cluster_files(self) -> None:
        inventory = self.root / INVENTORY
        lacks_group = inventory.is_file() and not re.search(r"^\[kubernetes\]", inventory.read_text(), re.M)
        if lacks_group and self.yes_no(
            "\nThe inventory has no [kubernetes] group, which the cluster's playbooks need. Add it?", True, "inventory"
        ):
            with inventory.open("a") as f:
                f.write("\n[kubernetes]\nhomeserver-k8s\n")
        bundle = self.talos_dir() / "secrets.yaml"
        if not bundle.exists():
            self.console.say(
                f"\nThe cluster's secrets bundle, {bundle}, is missing. If the cluster was ever built, "
                "restore it there from your password manager instead: a new one makes a new cluster."
            )
            if shutil.which("talosctl") and self.yes_no("Make a new one with talosctl gen secrets?", False, "bundle"):
                bundle.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                result = run(["talosctl", "gen", "secrets", "-o", str(bundle)])
                if result.returncode == 0:
                    bundle.chmod(0o600)
                    self.console.say("  Made. Keep a copy in your password manager.")
                else:
                    self.console.say(f"  talosctl failed: {result.stderr.strip()}")
        key = self.age_key()
        if not key.exists():
            self.console.say(
                f"\nThe age key Flux decrypts the cluster's Secrets with, {key}, is missing. If the "
                "cluster's Secrets were encrypted for a key you have, restore it there instead."
            )
            if shutil.which("age-keygen") and self.yes_no("Make a new one with age-keygen?", False, "age-key"):
                key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                result = run(["age-keygen", "-o", str(key)])
                if result.returncode == 0:
                    key.chmod(0o600)
                    self.console.say("  Made. Keep a copy in your password manager.")
                else:
                    self.console.say(f"  age-keygen failed: {result.stderr.strip()}")
        for path in (bundle, key):
            loose = path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077
            if loose and self.yes_no(
                f"\n{path} is readable by others than you. Make it 0600?", True, f"chmod:{path.name}"
            ):
                path.chmod(0o600)

    # ---------------------------------------------------------------------------------------------- setup ---
    def choose_setup(self) -> None:
        """The setup the answers go into, asked before anything is read: Enter keeps the one the checkout uses, a name
        switches to that setup or makes it. A checkout with files of its own is offered to keep them as a setup; one
        with neither files nor setups is asked nothing. A local.yml that sets nothing, as the first just init writes it,
        gives way to a setup; files that hold more are refused by name, and Enter keeps them."""
        try:
            where = setups.store(self.root)
        except setups.Refused as e:
            self.console.say(f"\n{e}")
            return
        found = setups.names(where)
        broken = False
        try:
            current = setups.active(self.root)
        except setups.Refused as e:
            current, broken = None, True
            self.console.say(f"\n{e}")
        if current is None and not broken:
            if self.adoptable():
                self.offer_setup(where, found)
                return
            if not found:
                return
        keep = current.name if current else ""
        self.console.say(
            f"\nSetups, in {setups.shown(where)}: {', '.join(found) or 'none yet'}. Each holds one machine's "
            "local.yml, vault and .vault_pass, and the answers below go into the one chosen; a new name makes one. "
            + (f"Enter keeps {keep}, which this checkout uses." if keep else "Enter keeps this checkout as it is.")
        )
        while True:
            name = self.console.ask(f"Which setup? [{keep}] ", "setup").strip()
            if not name or name == keep:
                return
            try:
                setups.check_name(name)
                if name in found:
                    setups.switch(self.root, where, name)
                elif self.yes_no(
                    f"There is no setup {name}. Make it, with an empty vault and a new password, and use it?",
                    True,
                    "setup-new",
                ):
                    setups.new(self.root, where, name, commented_example(self.root))
                else:
                    continue
            except setups.Refused as e:
                self.console.say(f"  {e}")
                continue
            self.console.say(f"  This checkout uses setup {name} now: the answers below go into it.")
            return

    def adoptable(self, opens: bool | None = None) -> bool:
        """Whether this checkout's own files can become a setup; opens, once the vault is read."""
        try:
            setups.adoptable(self.root, opens)
        except setups.Refused:
            return False
        return True

    def offer_setup(self, where: Path, found: list[str]) -> None:
        """This checkout's own three files kept as a setup, if the person agrees, and linked back."""
        self.console.say(
            "\nThis checkout keeps its own local.yml, vault and .vault_pass. Kept as a named setup instead, in "
            f"{setups.shown(where)}, they can be switched with another machine's"
            + (f" ({', '.join(found)})" if found else "")
            + ": just vault switch."
        )
        if not self.yes_no("Keep them as a setup?", False, "setup-adopt"):
            return
        default = "" if "default" in found else "default"
        while True:
            name = self.console.ask(f"Its name? [{default}] ", "setup-name").strip() or default
            if not name:
                return
            try:
                setup = setups.adopt(self.root, where, name)
            except setups.Refused as e:
                self.console.say(f"  {e}")
                default = ""  # Enter now gives up
                continue
            self.console.say(f"  Moved into setup {name}, {setups.shown(setup)}, and linked back.")
            return

    # ---------------------------------------------------------------------------------------------- vault ---
    def vault_problem(self) -> str:
        return {
            "no password": "has no password: .vault_pass is missing",
            "missing": f"is missing: {VAULT}",
            "not moved": f"is still at {OLD_VAULT}, where it was committed until 2026-10-20: move it to {VAULT} "
            "(README.md, Setups and the vault)",
            "does not open": "does not open with .vault_pass",
        }.get(self.vault.state, self.vault.state)

    def offer_vault(self) -> None:
        if self.vault.is_open:
            return
        self.console.say(
            f"\nThe vault {self.vault_problem()}. It holds the secrets below: the TrueNAS API key, "
            "Tailscale's auth key, Home Assistant's owner password."
        )
        if self.vault.state in ("no password", "does not open") and self.yes_no(
            "Do you have the vault's password, from your password manager?", True, "have-password"
        ):
            while True:
                password = self.console.secret("  The vault's password (hidden; Enter gives up): ", "vault-password")
                if not password:
                    break
                if self.vault.opens_with(password.strip()):
                    write_file(self.vault.password_file, password.strip().encode() + b"\n", 0o600)
                    self.vault.open()
                    self.console.say("  It opens: .vault_pass written, readable by you alone.")
                    return
                self.console.say("  That password does not open the vault.")
        if self.vault.state == "not moved" or self.setup:
            return  # a vault to move, or a setup's, which is restored rather than replaced
        if self.yes_no(
            "Make a new vault instead, empty, with a new password (just vault new)?",
            False,
            "vault-new",
        ):
            # What just vault new runs, in this process: no task runner needed, and a refusal is said, not raised.
            try:
                said = setups.own_vault(self.root)
            except (setups.Refused, OSError) as e:
                said = str(e)
            self.console.say(textwrap.indent(said, "  "))
            self.vault.open()

    # ------------------------------------------------------------------------------------------------ run ---
    def start_local(self) -> None:
        """local.yml without local.yml.example's own values: written when it is new or a terminal runs just init;
        otherwise only read so, and the summary says what the file still holds."""
        new = not self.local_path.exists()
        dropped = self.drop_example_values()
        if new or (self.interactive and dropped):
            self.save_local()
            if dropped and not new:
                self.console.say(
                    f"local.yml held local.yml.example's own values for {', '.join(dropped)}: they are commented out "
                    "now, and asked for below."
                )
        else:
            self.examples_left = dropped

    def drop_example_values(self) -> list[str]:
        """Comment out each setting still at local.yml.example's value, and drop the example's entries from one that
        holds real ones beside them; the settings changed so."""
        dropped = []
        for key, value in self.values.items():
            if key not in self.example:
                continue
            kept = cfg.without_example(value, self.example[key])
            if kept == value:
                continue
            try:
                if kept is None:
                    self.local.comment_out(key)
                else:
                    self.local.set(key, kept, self.order)
            except EditError as e:
                self.console.say(f"{key} holds local.yml.example's value, and {e}: change it by hand in {LOCAL}.")
                continue
            dropped.append(key)
        return dropped

    def read(self) -> bool:
        """The setup the checkout uses, then local.yml and the vault read; False when its links are not whole, which
        stops everything else."""
        try:
            self.setup = setups.active(self.root)
        except setups.Refused as e:
            self.broken = str(e)
            return False
        self.load()
        self.vault.open()
        return True

    def run(self) -> None:
        if self.interactive:
            self.choose_setup()
        if not self.read():
            self.console.say(self.summary())
            return
        self.start_local()
        if self.interactive:
            self.console.say(
                "Each setting shows its value: Enter keeps it, - clears it, ? explains it. Every answer is saved at "
                "once, so you can stop at any time and run just init again later."
                + (
                    ""
                    if self.full
                    else " This asks for the required settings and the popular optional ones; "
                    "just init --full asks for every one."
                )
            )
            self.offer_vault()
            self.console.say("\nFirst, what the setup has. The Proxmox host is always part of it.")
            for section in cfg.SWITCHES:
                self.chosen[section] = self.switch(section)
            for section in cfg.SECTIONS:
                if self.on(section):
                    self.ask_section(section)
        self.console.say(self.summary())

    # -------------------------------------------------------------------------------------------- summary ---
    def node_shape(self) -> list[dict]:
        data = yaml.safe_load((self.root / "group_vars/all/kubernetes.yml").read_text())
        return data.get("kubernetes_node_shape", [])

    def checks(self) -> list[str]:
        """What the playbooks' own settings checks say: validate.yml, run on this machine alone. A check is a message
        (nas_section_error) or a list of them (the others), empty when it passes."""
        playbook = Path(sys.executable).parent / "ansible-playbook"
        with tempfile.TemporaryDirectory(prefix="init-") as scratch:
            out = Path(scratch) / "checks.json"
            result = run(
                [str(playbook), str(VALIDATE), "-e", json.dumps({"init_checks_file": str(out)})],
                cwd=self.root,
                # Ansible's own output, which a failure quotes, and no run log: nobody watches this run.
                env={**os.environ, "ANSIBLE_NOCOLOR": "1", "ANSIBLE_STDOUT_CALLBACK": "default"},
                timeout=300,
            )
            if result.returncode or not out.is_file():
                tail = (result.stdout.strip() + "\n" + result.stderr.strip()).strip().splitlines()[-4:]
                return ["The settings checks could not run: " + " ".join(line.strip() for line in tail)]
            checks = json.loads(out.read_text())
        messages = []
        for check in checks.values():
            messages += [str(m).strip() for m in (check if isinstance(check, list) else [check]) if str(m).strip()]
        return messages

    def tools(self) -> list[str]:
        missing = []
        for tool, brew in (
            ("talosctl", "siderolabs/tap/talosctl"),
            ("kubectl", "kubectl"),
            ("sops", "sops"),
            ("age-keygen", "age"),
        ):
            if not shutil.which(tool):
                missing.append(f"{tool} is not on PATH: brew install {brew}")
        if shutil.which("talosctl"):
            contract = yaml.safe_load((self.root / "group_vars/all/kubernetes.yml").read_text()).get("talos_contract")
            tag = re.search(r"Tag:\s*(v\d+\.\d+)", run(["talosctl", "version", "--client"]).stdout)
            if contract and tag and tag.group(1) != contract:
                missing.append(
                    f"talosctl is {tag.group(1)}, and the cluster needs {contract}: the role refuses "
                    "another minor release"
                )
        return missing

    def summary(self) -> str:
        if self.vault.state == "unread" and not self.broken:
            self.read()  # stopped at the setup question, before anything was read
        if self.broken:
            return banner(
                "Setup: its links are not whole",
                "Nothing was read or written.",
                [("STILL REQUIRED", [self.broken], "x")],
            )
        v = self.values
        on = {section: self.on(section) for section in cfg.SECTIONS}
        required: list[str] = []
        recommended: list[str] = []
        manual: list[str] = []
        notes: list[str] = []

        if not (self.root / INVENTORY).is_file():
            required.append("inventory is missing: just init copies it from inventory.example")
        if not self.vault.is_open:
            required.append(
                f"The vault {self.vault_problem()}: "
                + (
                    f"restore the setup's files, {setups.shown(self.setup)}, from your password manager"
                    if self.setup
                    else "restore it and .vault_pass from your password manager, or make new ones with just vault "
                    "new (README.md, Setups and the vault)"
                )
            )
        for s in cfg.SETTINGS:
            if not on[s.section] or not self.applies(s):
                continue
            need = self.needed(s)
            if s.secret and not self.vault.is_open:
                continue
            value = self.current(s)
            if has_value(value):
                continue
            text = f"{s.key}: {s.label}"
            if need == "required":
                required.append(text)
            elif need == "recommended":
                recommended.append(text)
        if self.examples_left:
            required.append(
                f"local.yml still holds local.yml.example's own values for {', '.join(self.examples_left)}, which the "
                "playbooks would read as real ones: just init in a terminal comments them out and asks for them"
            )

        if on["cluster"]:
            if not re.search(
                r"^\[kubernetes\]",
                (self.root / INVENTORY).read_text() if (self.root / INVENTORY).is_file() else "",
                re.M,
            ):
                required.append(
                    "The inventory has no [kubernetes] group with homeserver-k8s, which the cluster's "
                    "playbooks need (inventory.example has it)"
                )
            bundle = self.talos_dir() / "secrets.yaml"
            if not bundle.is_file():
                required.append(
                    f"The cluster's secrets bundle {bundle}: restore it from your password manager, or "
                    "for a cluster never built, talosctl gen secrets (scripts/runbooks/talos.md)"
                )
            key = self.age_key()
            if not key.is_file():
                required.append(
                    f"The age key {key}: restore it, or for new Secrets, age-keygen (scripts/runbooks/talos.md)"
                )
            else:
                recipient = run(["age-keygen", "-y", str(key)]).stdout.strip() if shutil.which("age-keygen") else ""
                sops = (self.root / ".sops.yaml").read_text() if (self.root / ".sops.yaml").is_file() else ""
                if recipient and recipient not in sops:
                    required.append(
                        f"k8s's SOPS files are encrypted for the age recipient in .sops.yaml, not for "
                        f"{key} ({recipient}): Flux could decrypt none of them. A fork puts its own "
                        "recipient there and encrypts every Secret again (k8s/README-secrets.md)"
                    )
            for path in (bundle, key):
                if path.is_file() and stat.S_IMODE(path.stat().st_mode) & 0o077:
                    required.append(f"{path} is readable by others than you: chmod 600 {path}")
            required += self.tools()

        if self.ssh and not self.ssh[0]:
            required.append(
                f"Root SSH to {v.get('proxmox_address')} fails ({self.ssh[1]}). A key with a passphrase "
                "must be in the agent (ssh-add); otherwise scripts/runbooks/REBUILD.md, section 3"
            )

        refused = []
        if self.vault.is_open and (self.root / INVENTORY).is_file():
            refused = self.checks()
            # A setting a check names is said better by the check.
            required = [r for r in required if not any(r.split(":")[0] in m for m in refused)]
        for s in cfg.SETTINGS:
            unread = on[s.section] and not s.secret and not self.applies(s) and has_value(v.get(s.key))
            if unread and not any(s.key in m for m in refused):
                notes.append(f"local.yml sets {s.key}, but {self.unread(s)}: just init offers to delete it")

        if not self.ssh:
            manual.append(
                "Root SSH from this machine to the Proxmox host, with its key named in a Host block of "
                "~/.ssh/config (scripts/runbooks/REBUILD.md, section 3)"
            )
        if on["remote"]:
            routes = has_value(v.get("tailscale_advertise_routes"))
            manual.append(
                "The tailnet's policy"
                + (" and DNS" if routes else "")
                + " in Tailscale's admin console, before the first run (scripts/runbooks/tailscale.md, "
                + ("sections 1 and 2)" if routes else "section 1)")
            )
        if on["cluster"]:
            macs = v.get("kubernetes_node_macs") if isinstance(v.get("kubernetes_node_macs"), dict) else {}
            addresses = (
                v.get("kubernetes_node_addresses") if isinstance(v.get("kubernetes_node_addresses"), dict) else {}
            )
            pairs = ", ".join(f"{macs.get(n, '?')} -> {addresses.get(n, '?')} ({n})" for n in cfg.NODES)
            manual.append(
                f"In the router's DHCP, reserve each node's address for its MAC: {pairs}; and keep "
                f"MetalLB's range ({v.get('kubernetes_lb_range', '?')}) out of the dynamic range"
            )
            domain = v.get("kubernetes_domain") or cfg.DEFAULTS["kubernetes_domain"]
            manual.append(f"In DNS, a wildcard record *.{domain} -> {v.get('kubernetes_lb_traefik_ip', '?')}")
            if not has_value(v.get("kubernetes_ingress_allowed_range")):
                manual.append(
                    "After the first build, each app's first visit through a port forward, then "
                    "kubernetes_ingress_allowed_range and just deploy kubernetes (scripts/runbooks/first-run.md)"
                )
            manual.append("Trust the cluster's CA on each device (scripts/runbooks/tls.md)")
            if has_value(v.get("kubernetes_gpu_mapping")):
                manual.append(
                    "The host's part of scripts/runbooks/gpu.md: VT-d, the vfio-pci binding and the PCI "
                    f"resource mapping {v['kubernetes_gpu_mapping']}"
                )
        if on["homeassistant"]:
            manual.append(
                f"In the router's DHCP, reserve an address for {v.get('haos_mac', '?')} with the hostname "
                "homeassistant (scripts/runbooks/haos.md)"
            )
            if not on["nas"]:
                storage = v.get("haos_import_storage") or cfg.DEFAULTS["haos_import_storage"]
                manual.append(
                    f"On the Proxmox host, once: {storage} must hold import and images content, beside what it holds "
                    f"now (--content replaces the whole list). Read the list with pvesh get /storage/{storage}, then "
                    f"pvesm set {storage} --content <that list, plus whichever of import and images it lacks> "
                    "(scripts/runbooks/haos.md, step 5)"
                )
        keep = (
            [f"the setup's directory, {setups.shown(self.setup)}"]
            if self.setup
            else ["local.yml", "the vault", ".vault_pass"]
        ) + (["the cluster's credentials directory", "its age key"] if on["cluster"] else [])
        listed = keep[0] if len(keep) == 1 else f"{', '.join(keep[:-1])} and {keep[-1]}"
        manual.append(f"Keep copies of {listed} in your password manager, and update them when they change")
        if not self.setup and self.adoptable(self.vault.is_open):
            notes.append(
                "just vault new <name> --from current keeps local.yml, the vault and .vault_pass as a named "
                "setup outside the checkout, to switch between machines (README.md, Setups and the vault)"
            )

        if not self.full:
            more = [s for s in cfg.SETTINGS if on[s.section] and self.applies(s) and not self.asked(s)]
            if more:
                notes.append(
                    f"just init --full also asks for {len(more)} more optional setting{'s' if len(more) > 1 else ''}"
                )

        nexts = []
        if on["nas"]:
            nexts.append("just check truenas && just deploy truenas")
        nexts.append("just check proxmox && just deploy proxmox")
        if on["homeassistant"]:
            nexts.append("just check homeassistant && just deploy homeassistant")
        if on["cluster"]:
            nexts.append("just check kubernetes && just deploy kubernetes")

        parts = [cfg.SECTIONS[s] for s in cfg.SECTIONS if on[s]]
        name = f" {self.setup.name}" if self.setup else ""
        title = f"Setup{name}: " + ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1]
        status = (
            "Nothing required is missing."
            if not (required or refused)
            else f"{len(required) + len(refused)} thing(s) still required before the playbooks will run."
        )
        blocks = [
            ("STILL REQUIRED", required, "x"),
            ("THE PLAYBOOKS' CHECKS WOULD STOP WITH", refused, "x"),
            ("RECOMMENDED", recommended, "-"),
            ("BY HAND, WHICH just init CANNOT CHECK", manual, "-"),
            ("NOTES", notes, "-"),
            ("THEN, IN THIS ORDER", nexts, " "),
        ]
        return banner(title, status, blocks)


def banner(title: str, status: str, blocks: list[tuple[str, list[str], str]], width: int = 110) -> str:
    rule = "=" * width
    lines = ["", rule, f"  {title}", f"  {status}", rule]
    for heading, items, mark in blocks:
        if not items:
            continue
        lines.append(f"  {heading}")
        for item in items:
            wrapped = textwrap.wrap(item, width - 8, break_on_hyphens=False, break_long_words=False) or [""]
            lines.append(f"    {mark} {wrapped[0]}")
            lines += [f"      {w}" for w in wrapped[1:]]
        lines.append("")
    lines.append(rule)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="ask for every optional setting too")
    args = parser.parse_args(argv)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    init = Init(ROOT, Console(), full=args.full, interactive=interactive)
    try:
        init.run()
    except (KeyboardInterrupt, EOFError):
        init.console.say("\n\nStopped. Every answer given so far is saved.")
        init.console.say(init.summary())
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
