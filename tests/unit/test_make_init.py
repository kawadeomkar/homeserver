"""make init (scripts/init/): the settings file's editing, the answers' parsing, and whole runs with a scripted console.

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
    """A checkout's files make init reads, with HOME inside it, so the cluster's files are looked for there."""
    root = tmp_path / "repo"
    (root / "group_vars").mkdir(parents=True)
    shutil.copytree(
        ROOT / "group_vars" / "all",
        root / "group_vars" / "all",
        ignore=shutil.ignore_patterns("local.yml", "vault.yml"),
    )
    for name in ("inventory.example", ".sops.yaml", "ansible.cfg"):
        shutil.copy(ROOT / name, root / name)
    shutil.copy(ROOT / "inventory.example", root / "inventory")
    (root / "scripts" / "init").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "init" / "validate.yml", root / "scripts" / "init" / "validate.yml")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return root


def legacy_copy(root: Path) -> Path:
    """local.yml as an older make init left it: a copy of local.yml.example, its values in force."""
    path = root / "group_vars/all/local.yml"
    shutil.copy(root / "group_vars/all/local.yml.example", path)
    return path


def make_vault(root: Path, data: dict, text: str | None = None) -> None:
    """A vault holding data, or exactly text."""
    (root / ".vault_pass").write_text(VAULT_PASSWORD + "\n")
    (root / ".vault_pass").chmod(0o600)
    lib = VaultLib([("default", VaultSecret(VAULT_PASSWORD.encode()))])
    plain = yaml.safe_dump(data) if text is None else text
    (root / "group_vars/all/vault.yml").write_bytes(lib.encrypt(plain.encode()))


def read_vault(root: Path) -> dict:
    lib = VaultLib([("default", VaultSecret(VAULT_PASSWORD.encode()))])
    return yaml.safe_load(lib.decrypt((root / "group_vars/all/vault.yml").read_bytes()))


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
    doc = wizard.YamlKeys("---\n{}\n")  # what make vault TYPE=new encrypts
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
    assert "make check TYPE=kubernetes && make apply TYPE=kubernetes" in summary
    assert "vm_backup_storage" in summary.split("RECOMMENDED")[1]
    assert "TYPE=full also asks" in summary
    assert stat.S_IMODE((repo / "group_vars/all/local.yml").stat().st_mode) == 0o600, "readable by its owner alone"


def test_a_second_run_of_enters_changes_nothing(repo):
    init_run(repo, Scripted(first_run_answers()))
    before = (repo / "group_vars/all/local.yml").read_text()
    console = Scripted()
    init_run(repo, console, full=True)
    assert (repo / "group_vars/all/local.yml").read_text() == before
    assert "kubernetes_api_sans" in console.topics, "TYPE=full asks for every setting"
    assert any(f"now {lan(10)}" in line for line in console.out), "each setting shows its value"


def test_a_section_turned_off_loses_its_settings_only_when_the_person_agrees(repo):
    init_run(repo, Scripted(first_run_answers()))
    init_run(repo, Scripted({"switch:cluster": "n", "clear:cluster": "n"}))
    assert "kubernetes_node_addresses" in local(repo)
    init_run(repo, Scripted({"switch:cluster": "n", "clear:cluster": "y"}))
    assert not [k for k in local(repo) if k.startswith(("kubernetes_", "flux_"))]
    assert "haos_mac" in local(repo), "another section is left alone"


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
    assert (repo / "group_vars/all/vault.yml").read_text().startswith("$ANSIBLE_VAULT;1.1;AES256")
    v = local(repo)
    assert v["truenas_gateway"] == lan(1) and v["truenas_nameservers"] == [lan(1)], "the gateway is offered"
    assert "truenas_bootstrap_address" not in v and "truenas_api_cert_sha256" not in v
    assert api_key not in console.text

    console = Scripted({"vault_truenas_api_key": "-"})
    init_run(repo, console)
    assert any("now set." in line for line in console.out)
    assert read_vault(repo) == {"vault_other": "left-alone"}
    assert "vault_truenas_api_key" in console.out[-1].split("STILL REQUIRED")[1]


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


def test_a_secret_is_written_into_the_empty_vault_make_vault_init_makes(repo):
    make_vault(repo, {}, text="---\n{}\n")
    password = "-".join(["a", "long", "owner", "password"])
    answers = {
        "switch:nas": "n",
        "switch:remote": "n",
        "switch:cluster": "n",
        "switch:homeassistant": "y",
        "proxmox_address": lan(10),
        "vault_haos_owner_password": password,
    }
    init_run(repo, Scripted(answers))
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
