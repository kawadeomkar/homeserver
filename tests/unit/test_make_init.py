"""make init (scripts/init/): the settings file's editing, the answers' parsing, and whole runs with a scripted console.

Addresses and MACs are built at run time: gitleaks flags any written out in a tracked file.
"""

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
            "vault-init": "n",
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
    shutil.copy(root / "group_vars/all/local.yml.example", root / "group_vars/all/local.yml")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return root


def make_vault(root: Path, data: dict) -> None:
    (root / ".vault_pass").write_text(VAULT_PASSWORD + "\n")
    (root / ".vault_pass").chmod(0o600)
    lib = VaultLib([("default", VaultSecret(VAULT_PASSWORD.encode()))])
    (root / "group_vars/all/vault.yml").write_bytes(lib.encrypt(yaml.safe_dump(data).encode()))


def read_vault(root: Path) -> dict:
    lib = VaultLib([("default", VaultSecret(VAULT_PASSWORD.encode()))])
    return yaml.safe_load(lib.decrypt((root / "group_vars/all/vault.yml").read_bytes()))


def local(root: Path) -> dict:
    return yaml.safe_load((root / "group_vars/all/local.yml").read_text())


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
    ],
)
def test_a_bad_answer_is_refused(kind, text):
    with pytest.raises(ValueError):
        cfg.PARSERS[kind](fill(text))


def test_a_universally_administered_mac_is_refused():
    with pytest.raises(ValueError, match="locally administered"):
        cfg.PARSERS["mac"](":".join(["00", "11", "22", "33", "44", "55"]))


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
    (repo / "group_vars/all/local.yml").chmod(0o640)
    mode = 0o640
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
    assert "make check-kubernetes && make kubernetes" in summary
    assert "vm_backup_storage" in summary.split("RECOMMENDED")[1]
    assert "TYPE=full also asks" in summary
    assert stat.S_IMODE((repo / "group_vars/all/local.yml").stat().st_mode) == mode, "its mode is kept"


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


def test_without_a_terminal_it_asks_nothing_and_summarises(repo):
    console = Silent()
    init_run(repo, console, interactive=False)
    assert "local.yml.example's placeholder" in console.out[-1]
    assert (repo / "group_vars/all/local.yml").read_text() == (repo / "group_vars/all/local.yml.example").read_text()


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
