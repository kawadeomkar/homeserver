"""just init (scripts/init/): the settings file's editing, the answers' parsing, and whole runs with a scripted console.

Addresses and MACs are built at run time: gitleaks flags any written out in a tracked file.
"""

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
import settings as cfg  # noqa: E402
import setups  # noqa: E402

VAULT_PASSWORD = "a-vault-password-for-the-test"


def lan(host: int) -> str:
    return ".".join(str(p) for p in (10, 20, 30, host))


def mac(last: int) -> str:
    return ":".join(["02", "00", "00", "00", "00", f"{last:02x}"])


class Scripted(wizard.Console):
    """Answers by topic, Enter for any other; never says yes to a step that would reach the network."""

    def __init__(self, answers: dict | None = None):
        self.answers = {
            "ssh": "n",
            "read-certificate": "n",
            "bundle": "n",
            "age-key": "n",
            "vault-new": "n",
            "have-password": "n",
            "setup-adopt": "n",
            **(answers or {}),
        }
        self.out: list[str] = []
        self.topics: list[str] = []

    def say(self, text: str = "") -> None:
        self.out.append(text)

    def _answer(self, prompt: str, topic: str) -> str:
        self.out.append(prompt)
        self.topics.append(topic)
        answer = self.answers.get(topic, "")
        if isinstance(answer, list):
            return answer.pop(0) if answer else ""
        return answer

    def ask(self, prompt: str, topic: str) -> str:
        return self._answer(prompt, topic)

    def secret(self, prompt: str, topic: str) -> str:
        return self._answer(prompt, topic)

    @property
    def text(self) -> str:
        return "\n".join(self.out)


class Silent(Scripted):
    def ask(self, prompt: str, topic: str) -> str:
        raise AssertionError(f"asked {topic} without a terminal")

    secret = ask


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A checkout's files just init reads, with HOME inside it, so the cluster's files are looked for there, and a
    store of setups of its own."""
    root = tmp_path / "repo"
    (root / "group_vars").mkdir(parents=True)
    shutil.copytree(
        ROOT / "group_vars" / "all",
        root / "group_vars" / "all",
        ignore=shutil.ignore_patterns("local.yml", "local.vault.yml", "vault.yml"),
    )
    for name in ("inventory.example", ".sops.yaml", "ansible.cfg"):
        shutil.copy(ROOT / name, root / name)
    shutil.copy(ROOT / "inventory.example", root / "inventory")
    (root / "scripts" / "init").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "init" / "validate.yml", root / "scripts" / "init" / "validate.yml")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SETUPS", str(tmp_path / "setups"))
    return root


def legacy_copy(root: Path) -> Path:
    """local.yml as an older just init left it: a copy of local.yml.example, its values in force."""
    path = root / "group_vars/all/local.yml"
    shutil.copy(root / "group_vars/all/local.yml.example", path)
    return path


def make_vault(root: Path, data: dict, text: str | None = None) -> None:
    """A vault holding data, or exactly text."""
    (root / ".vault_pass").write_text(VAULT_PASSWORD + "\n")
    (root / ".vault_pass").chmod(0o600)
    lib = VaultLib([("default", VaultSecret(VAULT_PASSWORD.encode()))])
    plain = yaml.safe_dump(data) if text is None else text
    (root / wizard.VAULT).write_bytes(lib.encrypt(plain.encode()))


def read_vault(root: Path) -> dict:
    return yaml.safe_load(vault_text(root))


def vault_text(root: Path) -> str:
    lib = VaultLib([("default", VaultSecret(VAULT_PASSWORD.encode()))])
    return lib.decrypt((root / wizard.VAULT).read_bytes()).decode()


def local(root: Path) -> dict:
    return yaml.safe_load((root / "group_vars/all/local.yml").read_text()) or {}


def init_run(root: Path, console: Scripted, *, full: bool = False, interactive: bool = True, checks=None):
    init = wizard.Init(root, console, full=full, interactive=interactive)
    init.checks = lambda: list(checks or [])
    init.run()
    return init


# --------------------------------------------------------------------------------------------- editing ---
def test_a_key_is_changed_in_place_and_every_other_line_stays():
    doc = wizard.YamlKeys(
        "---\n# Its comment.\nfirst: 1\nmapping:\n  a: 1\n\n  b: 2\n# Another comment.\n# later_key: [1]\n"
        "# multi_key:\n#   part: {x: 1}\n# Prose after.\nlast: true\n"
    )
    doc.set("mapping", {"a": 3}, order=[])
    doc.set("later_key", ["x"], order=[])
    doc.set("multi_key", {"part": {"x": 2}}, order=[])
    doc.set("new_key", "v", order=["first", "new_key"])
    assert doc.values() == {
        "first": 1,
        "new_key": "v",
        "mapping": {"a": 3},
        "later_key": ["x"],
        "multi_key": {"part": {"x": 2}},
        "last": True,
    }
    assert doc.lines == [
        "---",
        "# Its comment.",
        "first: 1",
        "new_key: v",
        "mapping:",
        "  a: 3",
        "# Another comment.",
        "later_key: [x]",
        "multi_key:",
        "  part: {x: 2}",
        "# Prose after.",
        "last: true",
    ]
    assert doc.unset("mapping")
    assert not doc.unset("mapping")
    assert "mapping" not in doc.values()
    assert "# Another comment." in doc.lines


def test_a_comment_inside_a_mapping_is_part_of_it():
    doc = wizard.YamlKeys("---\nnodes:\n# The nodes.\n  a: 1\n  b: 2\n# After the nodes.\nlast: 1\n")
    doc.set("nodes", {"a": 3, "b": 2}, order=[])
    doc.set("macs", {"a": "x"}, order=["nodes", "macs"])
    assert doc.values() == {"nodes": {"a": 3, "b": 2}, "macs": {"a": "x"}, "last": 1}
    assert doc.lines[-2:] == ["# After the nodes.", "last: 1"]
    assert doc.unset("nodes")
    assert doc.values() == {"macs": {"a": "x"}, "last": 1}


def test_a_key_is_added_to_a_mapping_written_in_flow_style():
    doc = wizard.YamlKeys("---\n{}\n")  # what just vault new encrypted before the template
    doc.set("vault_key", "value", order=[])
    assert doc.values() == {"vault_key": "value"}
    doc = wizard.YamlKeys("---\n# A comment.\n{a: 1}\n")
    doc.set("b", 2, order=[])
    assert doc.values() == {"a": 1, "b": 2} and doc.lines[:2] == ["---", "# A comment."]


def test_an_edit_that_would_change_another_setting_is_refused():
    doc = wizard.YamlKeys("a: 1\nb: 2\na: 3\n")  # YAML keeps the last a
    before = list(doc.lines)
    with pytest.raises(wizard.EditError):
        doc.set("a", 4, order=[])
    with pytest.raises(wizard.EditError):
        doc.unset("a")
    assert doc.lines == before


def test_a_value_yaml_could_misread_is_quoted():
    digits_only = ":".join(["12"] * 6)  # a MAC YAML 1.1 reads as a base-60 number unless quoted
    lines = wizard.render("macs", {"node": digits_only})
    assert yaml.safe_load("\n".join(lines)) == {"macs": {"node": digits_only}}
    usb = [{"host": "10c4:ea60"}]
    assert yaml.safe_load("\n".join(wizard.render("haos_usb", usb))) == {"haos_usb": usb}
    assert wizard.render("flag", True) == ["flag: true"]


def test_an_interrupted_write_leaves_the_file_as_it_was_and_no_copy_behind(tmp_path, monkeypatch):
    path = tmp_path / ".vault_pass"
    path.write_text("before\n")

    def interrupted(*_):
        raise KeyboardInterrupt

    monkeypatch.setattr(wizard.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        wizard.write_file(path, b"after\n", 0o600)
    assert path.read_text() == "before\n"
    assert not wizard.temp_copy(path).exists(), "the copy holding the new password is gone"


def test_a_copy_an_earlier_run_left_is_made_anew(tmp_path):
    path = tmp_path / ".vault_pass"
    wizard.temp_copy(path).write_text("left behind\n")
    wizard.write_file(path, b"after\n", 0o600)
    assert path.read_text() == "after\n" and not wizard.temp_copy(path).exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("path", [wizard.VAULT_PASS, wizard.LOCAL, wizard.VAULT], ids=str)
def test_git_ignores_the_copies_make_init_writes_through(path):
    copy = wizard.temp_copy(path)
    ignored = subprocess.run(["git", "check-ignore", "--no-index", "-q", str(copy)], cwd=ROOT, check=False)
    assert ignored.returncode == 0, f"git would offer {copy} for a commit"


NET = ".".join(["10", "20", "30"])


def fill(value):
    """{n} in a test's text or value is the network above."""
    if isinstance(value, str):
        return value.replace("{n}", NET)
    if isinstance(value, list):
        return [fill(v) for v in value]
    return value


@pytest.mark.parametrize(
    ("kind", "text", "value"),
    [
        ("ipv4_interface", "{n}.4/24", "{n}.4/24"),
        ("ipv4_list", "{n}.1, {n}.2", ["{n}.1", "{n}.2"]),
        ("lb_range", "{n}.40 - {n}.49", "{n}.40-{n}.49"),
        ("lb_range", "{n}.40/29", "{n}.40/29"),
        ("git_ref", "my-branch", "refs/heads/my-branch"),
        ("usb", "10C4:EA60, mapping:zigbee", [{"host": "10c4:ea60"}, {"mapping": "zigbee"}]),
        ("fingerprint", "ab" * 32, ":".join(["AB"] * 32)),
        ("mail_security", "tls", "TLS"),
        ("vmid_list", "320 330", [320, 330]),
        ("size_or_none", "0", 0),
        ("app_list", "media, Nextcloud", ["media", "nextcloud"]),
        ("app_list", "none", []),
    ],
)
def test_answers_are_read_as_the_playbooks_want_them(kind, text, value):
    assert cfg.PARSERS[kind](fill(text)) == fill(value)


@pytest.mark.parametrize(
    ("kind", "text"),
    [
        ("ipv4", "{n}.300"),
        ("ipv4_interface", "{n}.4"),
        ("cidr", "{n}.4/24"),
        ("mac", "00:11:22:33:44"),
        ("lb_range", "{n}.49-{n}.40"),
        ("https_url", "http://example.invalid/repo.git"),
        ("owner_password", "short"),
        ("storage_id", "Local ZFS"),
        ("vmid", "99"),
        ("positive_int", "0"),
        ("size_or_none", "-1"),
        ("app_list", ","),
    ],
)
def test_a_bad_answer_is_refused(kind, text):
    with pytest.raises(ValueError):
        cfg.PARSERS[kind](fill(text))


def test_a_universally_administered_mac_is_refused():
    with pytest.raises(ValueError, match="locally administered"):
        cfg.PARSERS["mac"](":".join(["00", "11", "22", "33", "44", "55"]))


def test_a_repository_address_with_credentials_is_refused():
    url = "https://" + ":".join(["someone", "not-a-token"]) + "@git.example.invalid/me/homeserver.git"
    with pytest.raises(ValueError, match="without a user name or token"):
        cfg.PARSERS["https_url"](url)


def test_only_what_a_value_shares_with_the_example_goes():
    example = {"persistent": ["SERIAL-0001", "SERIAL-0002"], "ephemeral": ["SERIAL-0006"]}
    value = {"persistent": ["disk-1", "SERIAL-0002"], "ephemeral": ["SERIAL-0006"]}
    assert cfg.without_example(value, example) == {"persistent": ["disk-1"]}
    assert cfg.without_example(example, example) is None
    assert cfg.without_example("smtp.example.org", "<server>") == "smtp.example.org", "never judged by its look"


# ------------------------------------------------------------------------------------------------ runs ---
def first_run_answers() -> dict:
    return {
        "switch:nas": "n",
        "switch:remote": "n",
        "switch:cluster": "y",
        "switch:homeassistant": "y",
        "proxmox_address": lan(10),
        "proxmox_local_vm_storage": "local-zfs",
        "kubernetes_node_addresses/homeserver-cp-1": lan(30),
        "kubernetes_node_addresses/homeserver-worker-1": lan(31),
        "kubernetes_lb_range": f"{lan(40)}-{lan(49)}",
        "flux_sync_url": "https://git.example.invalid/me/homeserver.git",
        "kubernetes_node_sizes/homeserver-worker-1.cores": "4",
        "kubernetes_node_sizes/homeserver-worker-1.memory_mib": "8192",
        "kubernetes_node_sizes/homeserver-worker-1.system_disk_gib": "32",
        "kubernetes_node_sizes/homeserver-worker-1.data_disk_gib": "100",
        "kubernetes_node_sizes/homeserver-worker-1.media_disk_gib": "0",
        "kubernetes_media_on_data_volume": "y",
    }


def test_a_first_run_writes_each_answer_and_says_what_is_missing(repo):
    console = Scripted(first_run_answers())
    init_run(repo, console)
    v = local(repo)
    assert not [k for k in v if k.startswith("truenas_")], "the example's NAS placeholders are gone"
    assert v["proxmox_address"] == lan(10)
    assert v["proxmox_local_vm_storage"] == "local-zfs"
    assert v["kubernetes_node_addresses"] == {"homeserver-cp-1": lan(30), "homeserver-worker-1": lan(31)}
    macs = [*v["kubernetes_node_macs"].values(), v["haos_mac"]]
    assert len(set(macs)) == 3 and all(cfg.PARSERS["mac"](m) == m for m in macs)
    assert (v["kubernetes_lb_traefik_ip"], v["kubernetes_lb_frigate_ip"]) == (lan(40), lan(41))
    assert v["kubernetes_node_sizes"] == {
        "homeserver-worker-1": {
            "cores": 4,
            "memory_mib": 8192,
            "system_disk_gib": 32,
            "data_disk_gib": 100,
            "media_disk_gib": 0,
        }
    }
    assert v["kubernetes_media_on_data_volume"] is True
    assert "kubernetes_api_sans" not in console.topics, "a plain run leaves out the less used settings"
    assert "vm_backup_storage" in console.topics, "without a NAS the backup storage is asked for"
    summary = console.out[-1]
    assert "STILL REQUIRED" in summary
    assert "The vault has no password" in summary
    assert "vault_haos_owner_password" not in summary, "a secret is not reported while the vault does not open"
    assert "secrets bundle" in summary and "age key" in summary
    assert "just check kubernetes && just deploy kubernetes" in summary
    assert "vm_backup_storage" in summary.split("RECOMMENDED")[1]
    assert "just init --full also asks" in summary
    assert stat.S_IMODE((repo / "group_vars/all/local.yml").stat().st_mode) == 0o600, "readable by its owner alone"


def test_a_second_run_of_enters_changes_nothing(repo):
    init_run(repo, Scripted(first_run_answers()))
    before = (repo / "group_vars/all/local.yml").read_text()
    console = Scripted()
    init_run(repo, console, full=True)
    assert (repo / "group_vars/all/local.yml").read_text() == before
    assert "kubernetes_api_sans" in console.topics, "--full asks for every setting"
    assert any(f"now {lan(10)}" in line for line in console.out), "each setting shows its value"


def test_a_section_turned_off_loses_its_settings_only_when_the_person_agrees(repo):
    init_run(repo, Scripted(first_run_answers()))
    init_run(repo, Scripted({"switch:cluster": "n", "clear:cluster": "n"}))
    assert "kubernetes_node_addresses" in local(repo)
    init_run(repo, Scripted({"switch:cluster": "n", "clear:cluster": "y"}))
    assert not [k for k in local(repo) if k.startswith(("kubernetes_", "flux_"))]
    assert "haos_mac" in local(repo), "another section is left alone"


@pytest.mark.parametrize(
    ("text", "on"),
    [
        pytest.param("tailscale_enabled: true\n", True, id="enabled"),
        pytest.param("tailscale_enabled: false\n", False, id="disabled"),
        pytest.param("tailscale_advertise_routes: [{prefix}]\n", True, id="routes-alone"),
        pytest.param("", False, id="neither"),
    ],
)
def test_remote_access_is_on_as_the_playbooks_work_it_out(repo, text, on):
    """As proxmox_tailscale_enabled: tailscale_enabled when it is set, otherwise any routes, which a local.yml from
    before the switch lists alone."""
    (repo / "group_vars/all/local.yml").write_text("---\n" + text.format(prefix=f"{lan(0)}/24"))
    assert wizard.Init(repo, Silent(), full=False, interactive=False).present("remote") is on


def test_remote_access_is_switched_on_by_its_question_and_the_routes_are_an_option(repo):
    """The answer to the switch is tailscale_enabled itself, never asked again. The routes are optional: without them
    the host joins the tailnet alone, and only the tailnet's policy is left to do by hand."""
    console = Scripted(
        {
            "switch:nas": "n",
            "switch:remote": "y",
            "switch:cluster": "n",
            "switch:homeassistant": "n",
            "proxmox_address": lan(10),
            "tailscale_advertise_routes": "-",
        }
    )
    init_run(repo, console)
    v = local(repo)
    assert v["tailscale_enabled"] is True and "tailscale_advertise_routes" not in v
    assert "tailscale_enabled" not in console.topics and "tailscale_advertise_routes" in console.topics
    summary = console.out[-1]
    assert "tailscale_advertise_routes" not in summary, "neither required nor recommended"
    assert "The tailnet's policy in Tailscale's admin console, before the first run" in summary
    assert "(scripts/runbooks/tailscale.md, section 1)" in summary


def test_routes_alone_keep_remote_access_on_until_it_is_turned_off(repo):
    """A subnet router set up before the switch: Enter keeps it on and writes the switch beside its routes; turned
    off, and agreed to, both go, so the playbooks see no remote access."""
    prefix = f"{lan(0)}/24"
    (repo / "group_vars/all/local.yml").write_text(
        f"---\nproxmox_address: {lan(10)}\ntailscale_advertise_routes: [{prefix}]\n"
    )
    console = Scripted()
    init_run(repo, console)
    assert any("join your tailnet" in line and "[Y/n]" in line for line in console.out)
    assert (local(repo)["tailscale_enabled"], local(repo)["tailscale_advertise_routes"]) == (True, [prefix])
    assert "sections 1 and 2" in console.out[-1], "with routes, the tailnet's DNS too"
    init_run(repo, Scripted({"switch:remote": "n", "clear:remote": "y"}))
    assert not [k for k in local(repo) if k.startswith("tailscale_")]


def test_secrets_go_to_the_vault_and_never_to_the_screen(repo):
    make_vault(repo, {"vault_other": "left-alone"})
    api_key = "-".join(["made", "up", "for", "the", "test"])  # built: gitleaks flags a key written out
    answers = {
        "switch:nas": "y",
        "switch:remote": "n",
        "switch:cluster": "n",
        "switch:homeassistant": "n",
        "proxmox_address": lan(10),
        "truenas_static_address": f"{lan(5)}/24",
        "truenas_bootstrap_address": "-",
        "truenas_api_cert_sha256": "-",
        "truenas_pool_disks/persistent": "-",
        "truenas_pool_disks/ephemeral": "-",
        "vault_truenas_api_key": api_key,
    }
    console = Scripted(answers)
    init_run(repo, console)
    assert read_vault(repo) == {"vault_other": "left-alone", "vault_truenas_api_key": api_key}
    assert (repo / wizard.VAULT).read_text().startswith("$ANSIBLE_VAULT;1.1;AES256")
    v = local(repo)
    assert v["truenas_gateway"] == lan(1) and v["truenas_nameservers"] == [lan(1)], "the gateway is offered"
    assert "truenas_bootstrap_address" not in v and "truenas_api_cert_sha256" not in v
    assert api_key not in console.text

    console = Scripted({"vault_truenas_api_key": "-"})
    init_run(repo, console)
    assert any("now set." in line for line in console.out)
    assert read_vault(repo) == {"vault_other": "left-alone", "vault_truenas_api_key": ""}, "emptied, which is unset"
    assert "vault_truenas_api_key" in console.out[-1].split("STILL REQUIRED")[1]


@pytest.mark.parametrize("routes", [True, False], ids=["subnet-router", "host-only"])
def test_remote_access_requires_the_auth_key_for_the_hosts_first_login(repo, routes):
    # proxmox.yml refuses a host that is not logged in without the key, after changing it, and a dry run cannot tell;
    # a host that joins without routes logs in all the same.
    make_vault(repo, {})
    answers = {
        "switch:nas": "n",
        "switch:remote": "y",
        "switch:cluster": "n",
        "switch:homeassistant": "n",
        "proxmox_address": lan(10),
        "tailscale_advertise_routes": f"{lan(0)}/24" if routes else "-",
    }
    console = Scripted(answers)
    init_run(repo, console)
    assert local(repo).get("tailscale_advertise_routes") == ([f"{lan(0)}/24"] if routes else None)
    required = console.out[-1].split("STILL REQUIRED")[1].split("RECOMMENDED")[0]
    assert "vault_tailscale_auth_key" in required


def test_a_vault_at_its_old_committed_path_is_moved_not_replaced(repo):
    make_vault(repo, {"vault_other": "x"})
    (repo / wizard.VAULT).rename(repo / wizard.OLD_VAULT)
    console = Scripted({"switch:nas": "n", "switch:remote": "n", "switch:cluster": "n", "switch:homeassistant": "n"})
    init_run(repo, console)
    assert "vault-new" not in console.topics, "a new vault would be made beside the one to move"
    assert f"is still at {wizard.OLD_VAULT}" in console.out[-1].split("STILL REQUIRED")[1]


def test_a_vault_password_that_does_not_open_the_vault_is_not_kept(repo):
    make_vault(repo, {"vault_other": "x"})
    (repo / ".vault_pass").unlink()
    console = Scripted(
        {
            "have-password": "y",
            "vault-password": ["wrong-password", VAULT_PASSWORD],
            "switch:nas": "n",
            "switch:remote": "n",
            "switch:cluster": "n",
            "switch:homeassistant": "n",
        }
    )
    init_run(repo, console)
    assert (repo / ".vault_pass").read_text().strip() == VAULT_PASSWORD
    assert stat.S_IMODE((repo / ".vault_pass").stat().st_mode) == 0o600
    assert "That password does not open the vault." in console.text


NO_SECTIONS = {"switch:nas": "n", "switch:remote": "n", "switch:cluster": "n", "switch:homeassistant": "n"}


def test_a_new_vault_is_made_when_the_person_asks_for_one(repo):
    """just vault new's work, done in this process: the checkout's tree has no justfile, and init needs no task
    runner to make the vault, the template with every key empty, which then opens for the secrets asked after it."""
    console = Scripted({"vault-new": "y", **NO_SECTIONS})
    init = init_run(repo, console)
    for path in (repo / ".vault_pass", repo / wizard.VAULT):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert f"Made {wizard.VAULT}, every key empty, encrypted with .vault_pass" in console.text
    made = setups.decrypt((repo / wizard.VAULT).read_bytes(), (repo / ".vault_pass").read_bytes().strip())
    assert init.vault.is_open and made.decode() == (repo / setups.VAULT_TEMPLATE).read_text()
    assert "vault is missing" not in console.out[-1] and "has no password" not in console.out[-1]


def test_a_new_vault_never_replaces_one_that_may_hold_secrets(repo):
    """A vault that no .vault_pass opens is refused, as just vault new refuses it: said, and nothing written."""
    make_vault(repo, {"vault_other": "x"})
    (repo / ".vault_pass").unlink()
    before = (repo / wizard.VAULT).read_bytes()
    console = Scripted({"vault-new": "y", **NO_SECTIONS})
    init_run(repo, console)
    assert "does not open with .vault_pass, so it may hold your secrets" in console.text
    assert (repo / wizard.VAULT).read_bytes() == before and not (repo / ".vault_pass").exists()


def test_a_new_local_yml_is_the_example_with_every_setting_commented_out(repo):
    console = Silent()
    init_run(repo, console, interactive=False)
    path = repo / "group_vars/all/local.yml"
    assert local(repo) == {}, "the playbooks read nothing from it yet"
    example = yaml.safe_load((repo / "group_vars/all/local.yml.example").read_text())
    assert all(f"# {key}:" in path.read_text() for key in example)
    assert "proxmox_address: The Proxmox host's address" in console.out[-1].split("STILL REQUIRED")[1]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_without_a_terminal_it_asks_nothing_and_changes_nothing(repo):
    path = legacy_copy(repo)
    console = Silent()
    init_run(repo, console, interactive=False)
    assert path.read_text() == (repo / "group_vars/all/local.yml.example").read_text()
    assert "local.yml still holds local.yml.example's own values for truenas_static_address" in console.out[-1]


def test_an_older_copy_of_the_example_loses_its_values_and_keeps_its_changes(repo):
    path = legacy_copy(repo)
    path.write_text(path.read_text().replace("proxmox_address: 192.0.2.20", f"proxmox_address: {lan(10)}"))
    console = Scripted({"switch:nas": "n", "switch:remote": "n", "switch:cluster": "n", "switch:homeassistant": "n"})
    init_run(repo, console)
    assert local(repo) == {"proxmox_address": lan(10)}
    assert "# kubernetes_node_macs:" in path.read_text(), "commented out where it was"
    assert "they are commented out now" in console.text


def test_the_summary_carries_the_playbooks_own_refusals(repo):
    make_vault(repo, {})
    init_run(repo, Scripted(first_run_answers()))
    init = wizard.Init(repo, Silent(), full=False, interactive=False)
    init.vault.open()
    assert init.checks() == [], "the answers above pass the playbooks' checks"
    text = (repo / "group_vars/all/local.yml").read_text()
    (repo / "group_vars/all/local.yml").write_text(text.replace("kubernetes_lb_frigate_ip", "kubernetes_lb_frigate_x"))
    messages = init.checks()
    assert len(messages) == 1 and "kubernetes_lb_frigate_ip" in messages[0]


def owner_password_answers(password: str) -> dict:
    return {
        "switch:nas": "n",
        "switch:remote": "n",
        "switch:cluster": "n",
        "switch:homeassistant": "y",
        "proxmox_address": lan(10),
        "vault_haos_owner_password": password,
    }


def test_a_secret_is_written_into_the_vault_template_on_its_own_line(repo):
    """just vault new's vault: the answer takes the key's empty line, and every comment stays; cleared, the key
    is empty again, and the vault is the template once more."""
    template = (repo / setups.VAULT_TEMPLATE).read_text()
    make_vault(repo, {}, text=template)
    password = "-".join(["a", "long", "owner", "password"])
    init_run(repo, Scripted(owner_password_answers(password)))
    empty = 'vault_haos_owner_password: ""'
    assert vault_text(repo) == template.replace(empty, f"vault_haos_owner_password: {password}")
    init_run(repo, Scripted({"vault_haos_owner_password": "-"}))
    assert vault_text(repo) == template


def test_a_secret_is_written_into_the_empty_vault_an_older_just_vault_new_made(repo):
    make_vault(repo, {}, text="---\n{}\n")
    password = "-".join(["a", "long", "owner", "password"])
    init_run(repo, Scripted(owner_password_answers(password)))
    assert read_vault(repo) == {"vault_haos_owner_password": password}


def test_a_comment_inside_a_setting_never_mixes_two_settings(repo):
    init_run(repo, Scripted(first_run_answers()))
    path = repo / "group_vars/all/local.yml"
    macs = local(repo)["kubernetes_node_macs"]
    path.write_text(
        path.read_text().replace("kubernetes_node_addresses:\n", "kubernetes_node_addresses:\n# The nodes.\n")
    )
    path.chmod(0o640)
    init_run(repo, Scripted({"kubernetes_node_addresses/homeserver-cp-1": lan(32)}))
    assert stat.S_IMODE(path.stat().st_mode) == 0o640, "its mode is kept"
    v = local(repo)
    assert v["kubernetes_node_addresses"] == {"homeserver-cp-1": lan(32), "homeserver-worker-1": lan(31)}
    assert v["kubernetes_node_macs"] == macs


def test_a_change_that_cannot_be_made_in_place_leaves_the_file_as_it_was(repo):
    path = repo / "group_vars/all/local.yml"
    path.write_text(f"---\nproxmox_address: {lan(10)}\nproxmox_address: {lan(11)}\n")  # twice: YAML keeps the last
    before = path.read_text()
    console = Scripted({"switch:nas": "n", "switch:remote": "n", "switch:cluster": "n", "switch:homeassistant": "n"})
    console.answers["proxmox_address"] = lan(12)
    init_run(repo, console)
    assert "Not saved: proxmox_address cannot be changed in place" in console.text
    assert local(repo)["proxmox_address"] == lan(11)
    assert [line for line in path.read_text().splitlines() if "proxmox_address:" in line] == [
        line for line in before.splitlines() if "proxmox_address:" in line
    ]


def test_git_origin_is_shown_without_its_credentials_and_never_taken(repo):
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    token = "-".join(["not", "a", "real", "token"])
    origin = f"https://someone:{token}@git.example.invalid/me/homeserver.git"
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
    answers = first_run_answers()
    del answers["flux_sync_url"]
    console = Scripted(answers)
    init_run(repo, console)
    assert "flux_sync_url" not in local(repo), "Enter never takes the origin"
    assert "git's origin here is https://git.example.invalid/me/homeserver.git." in console.text
    assert token not in console.text


@pytest.mark.parametrize(
    ("origin", "shown"),
    [
        ("git@github.com:me/homeserver.git", "https://github.com/me/homeserver.git"),
        ("ssh://git@git.example.invalid:2222/me/homeserver", "https://git.example.invalid/me/homeserver.git"),
        ("https://me@git.example.invalid:8443/me/homeserver.git", "https://git.example.invalid:8443/me/homeserver.git"),
        ("file:///srv/homeserver.git", None),
    ],
)
def test_git_origin_is_shown_as_an_https_address(repo, monkeypatch, origin, shown):
    init = wizard.Init(repo, Silent(), full=False, interactive=False)
    monkeypatch.setattr(wizard, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, origin + "\n", ""))
    assert init.origin_url() == shown


def test_the_examples_values_go_and_the_real_values_beside_them_stay(repo):
    path = legacy_copy(repo)
    text = path.read_text().replace("SERIAL-0001", "disk-1").replace("SERIAL-0002", "disk-2")
    path.write_text(text.replace("homeserver-cp-1: 192.0.2.30", f"homeserver-cp-1: {lan(30)}"))
    console = Scripted(
        {
            "switch:nas": "y",
            "switch:remote": "n",
            "switch:cluster": "n",
            "clear:cluster": "n",
            "switch:homeassistant": "n",
            "proxmox_address": lan(10),
            "truenas_static_address": f"{lan(5)}/24",
        }
    )
    init_run(repo, console)
    v = local(repo)
    assert v["truenas_pool_disks"] == {"persistent": ["disk-1", "disk-2"]}, "a setting this run does not ask for"
    assert "clear:cluster" in console.topics, "a node address set by hand is asked about before it goes"
    assert v["kubernetes_node_addresses"] == {"homeserver-cp-1": lan(30)}


def nas_on(**answers) -> Scripted:
    return Scripted({"switch:nas": "y", "truenas_static_address": f"{lan(5)}/24", **answers})


def test_a_setting_the_setup_no_longer_reads_is_offered_for_removal(repo):
    init_run(repo, Scripted({**first_run_answers(), "vm_backup_storage": "usb-backup"}))
    assert local(repo)["vm_backup_storage"] == "usb-backup"
    console = nas_on(**{"remove:vm_backup_storage": "n"})
    init_run(repo, console)
    v = local(repo)
    assert v["vm_backup_storage"] == "usb-backup", "kept when the person says so"
    assert "kubernetes_media_on_data_volume" not in v, "removed on Enter: a NAS holds the media"
    assert "local.yml sets vm_backup_storage, but only a setup without a NAS reads it" in console.out[-1]
    console = Scripted()
    init_run(repo, console)
    assert "remove:vm_backup_storage" in console.topics
    assert "vm_backup_storage" not in local(repo)


def test_a_setting_whose_lead_is_cleared_is_offered_for_removal(repo):
    server = "smtp.myexample.com"  # holds example.com, and is no placeholder
    answers = {
        "switch:remote": "n",
        "switch:cluster": "n",
        "switch:homeassistant": "n",
        "proxmox_address": lan(10),
        "truenas_mail_server": server,
        "truenas_alert_email": "me@mail.invalid",
    }
    init_run(repo, nas_on(**answers))
    init_run(repo, Scripted())
    assert (local(repo)["truenas_mail_server"], local(repo)["truenas_alert_email"]) == (server, "me@mail.invalid")
    console = Scripted({"truenas_mail_server": "-"})
    init_run(repo, console)
    assert "remove:truenas_alert_email" in console.topics
    assert not [k for k in local(repo) if k.startswith(("truenas_mail_", "truenas_alert_"))]


def test_the_home_assistant_step_names_its_storage_and_keeps_its_content(repo):
    path = repo / "group_vars/all/local.yml"
    path.write_text(f"---\nproxmox_address: {lan(10)}\nhaos_mac: '{mac(9)}'\nhaos_import_storage: local-btrfs\n")
    console = Silent()
    init_run(repo, console, interactive=False)
    summary = " ".join(console.out[-1].split())
    assert "pvesh get /storage/local-btrfs" in summary and "pvesm set local-btrfs --content" in summary
    assert "--content replaces the whole list" in summary


def test_the_apps_a_host_runs_are_named_from_the_groups(repo):
    """kubernetes_apps lists the groups and refuses a name that is none, so a typo is caught at once rather than by
    the playbooks' check."""
    console = Scripted({**first_run_answers(), "kubernetes_apps": ["media, medias", "media homepage"]})
    init_run(repo, console)
    assert local(repo)["kubernetes_apps"] == ["media", "homepage"]
    assert "no such group: medias" in console.text
    assert any("The groups: media, nextcloud" in line for line in console.out), "the choices are shown"


def test_a_size_is_never_zero_but_a_disk_may_be_none(repo):
    answers = {
        **first_run_answers(),
        "kubernetes_node_sizes/homeserver-worker-1.cores": ["0", "4"],
        "kubernetes_node_sizes/homeserver-worker-1.media_disk_gib": "0",
    }
    console = Scripted(answers)
    init_run(repo, console)
    assert "give a whole number of at least 1" in console.text
    assert local(repo)["kubernetes_node_sizes"]["homeserver-worker-1"]["cores"] == 4
    assert local(repo)["kubernetes_node_sizes"]["homeserver-worker-1"]["media_disk_gib"] == 0


# --------------------------------------------------------------------------------------------- setups ---
def test_a_write_through_a_link_changes_the_file_it_links_to(tmp_path):
    stored = tmp_path / "setup" / "local.yml"
    stored.parent.mkdir()
    stored.write_text("---\n")
    link = tmp_path / "local.yml"
    link.symlink_to(stored)
    wizard.write_file(link, b"---\nproxmox_address: x\n", 0o600)
    assert link.is_symlink(), "the link stays"
    assert stored.read_text() == "---\nproxmox_address: x\n"


def flat(text: str) -> str:
    """The summary without its line breaks, which can fall inside a path."""
    return " ".join(text.split())


def test_the_answers_go_into_the_setup_the_checkout_uses(repo):
    setup = setups.new(repo, setups.store(), "home", wizard.commented_example(repo))
    console = Scripted(first_run_answers())
    init_run(repo, console)
    assert console.topics[0] == "setup", "the setup is asked for first"
    assert (repo / "group_vars/all/local.yml").is_symlink()
    assert yaml.safe_load((setup / "local.yml").read_text())["proxmox_address"] == lan(10)
    summary = flat(console.out[-1])
    assert "Setup home: " in summary
    assert f"Keep copies of the setup's directory, {setup}, the cluster's credentials directory" in summary
    assert "--from current" not in summary


def test_the_first_answer_switches_to_another_setup_or_makes_one(repo):
    where = setups.store()
    home = setups.new(repo, where, "home", wizard.commented_example(repo))
    lab = setups.new(repo, where, "lab", wizard.commented_example(repo))
    init_run(repo, Scripted({**first_run_answers(), "setup": "home"}))
    assert setups.active(repo) == home
    assert yaml.safe_load((home / "local.yml").read_text())["proxmox_address"] == lan(10)
    assert yaml.safe_load((lab / "local.yml").read_text()) is None, "the other setup is left alone"
    console = Scripted({"setup": ["Not A Name", "spare"], "setup-new": "y"})
    init_run(repo, console)
    assert "is no setup's name" in console.text
    assert setups.active(repo) == where / "spare" and (where / "spare" / "vault_pass").is_file()


def test_a_checkout_with_its_own_files_is_offered_to_keep_them_as_a_setup(repo):
    init_run(repo, Scripted(first_run_answers()))
    make_vault(repo, {"vault_haos_owner_password": "kept"})
    before = (repo / "group_vars/all/local.yml").read_text()
    declined = Scripted()
    init_run(repo, declined)
    assert "setup-adopt" in declined.topics and setups.active(repo) is None, "No is the default"
    assert "--from current keeps local.yml" in flat(declined.out[-1])
    init_run(repo, Scripted({"setup-adopt": "y", "setup-name": "home"}))
    setup = setups.store() / "home"
    assert setups.active(repo) == setup
    assert (setup / "local.yml").read_text() == before
    assert read_vault(repo) == {"vault_haos_owner_password": "kept"}, "read through its link"


def test_a_setup_restored_after_the_first_make_init_is_taken_at_its_first_question(repo):
    """A first run before the setups came back leaves local.yml setting nothing, which gives way to the setup."""
    init_run(repo, Silent(), interactive=False)
    password = b"a-password-of-the-restored-setup"
    home = setups.make_setup(
        setups.store(),
        "home",
        {
            "local.yml": b"---\n",
            "vault.yml": VaultLib([("default", VaultSecret(password))]).encrypt(b"---\n{}\n"),
            "vault_pass": password + b"\n",
        },
    )
    answers = {"switch:nas": "n", "switch:remote": "n", "switch:cluster": "n", "switch:homeassistant": "n"}
    console = Scripted({"setup": "home", **answers})
    init_run(repo, console)
    assert console.topics[0] == "setup" and setups.active(repo) == home
    assert "This checkout uses setup home now" in console.text


def test_stopped_at_the_setup_question_the_summary_says_what_is_there(repo):
    setup = setups.new(repo, setups.store(), "home", wizard.commented_example(repo))

    class Stopped(Scripted):
        def ask(self, prompt: str, topic: str) -> str:
            raise EOFError

    init = wizard.Init(repo, Stopped(), full=False, interactive=True)
    init.checks = list
    with pytest.raises(EOFError):
        init.run()
    summary = flat(init.summary())
    assert "Setup home: " in summary and "unread" not in summary and "just vault new" not in summary
    assert f"Keep copies of the setup's directory, {setup} in your password manager" in summary


def test_a_setup_whose_links_are_not_whole_stops_make_init_before_it_writes(repo):
    setup = setups.new(repo, setups.store(), "home", wizard.commented_example(repo))
    (setup / "local.yml").unlink()
    console = Silent()
    init_run(repo, console, interactive=False)
    assert not (setup / "local.yml").exists(), "never made anew through the link"
    assert "home, is not whole" in flat(console.out[-1])
