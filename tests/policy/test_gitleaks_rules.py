""".gitleaks.toml catches this network's identifiers, and leaves alone what only looks like them.

The fixtures are written to a temporary directory from split string literals, so this file holds no
identifier and the repository's own scans stay clean. They sit under the paths the path-anchored rules
expect (roles/, k8s/, tests/, ...) and are scanned from that root, as a directory and as a git commit:
the range scan in CI and the pre-push hook use git mode.

Marked `gitleaks`: `make test-gitleaks-rules` runs these with gitleaks from PATH, `make test` does not.
Without gitleaks they are skipped locally and fail in CI.
"""

import json
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / ".gitleaks.toml"

pytestmark = pytest.mark.gitleaks


def dotted(*octets):
    return ".".join(octets)


def colons(*groups):
    return ":".join(groups)


SECRET_KIND = "kind: " + "Secret"

# (path, content, the custom rules that must fire on it). An empty set means nothing may fire.
FIXTURES = [
    # private-ipv4: anywhere
    ("notes/rfc1918.md", "nas " + dotted("10", "20", "30", "40") + "\n", {"private-ipv4"}),
    (
        "notes/rfc1918-b.md",
        "router " + dotted("172", "16", "5", "4") + " and " + dotted("192", "168", "1", "1") + "\n",
        {"private-ipv4"},
    ),
    ("notes/cgnat.md", "tailnet peer " + dotted("100", "100", "1", "2") + "\n", {"private-ipv4"}),
    ("notes/link-local.md", "fallback " + dotted("169", "254", "1", "1") + "\n", {"private-ipv4"}),
    # ipv4-literal: any address in code, tests and manifests, private or not
    ("roles/demo/defaults/main.yml", "demo_dns: " + dotted("8", "8", "8", "8") + "\n", {"ipv4-literal"}),
    ("group_vars/all/net.yml", "demo_nas: " + dotted("192", "168", "1", "10") + "\n", {"private-ipv4", "ipv4-literal"}),
    ("k8s/demo/pv.yaml", "server: " + dotted("10", "0", "0", "64") + "\n", {"private-ipv4", "ipv4-literal"}),
    ("tests/demo/client.py", 'ADDRESS = "' + dotted("1", "2", "3", "4") + '"\n', {"ipv4-literal"}),
    ("molecule/demo/converge.yml", "demo_dns: " + dotted("8", "8", "4", "4") + "\n", {"ipv4-literal"}),
    (".config/molecule/config.yml", "server: " + dotted("1", "1", "1", "1") + "\n", {"ipv4-literal"}),
    ("extra.yml", "dns: " + dotted("9", "9", "9", "9") + "\n", {"ipv4-literal"}),
    # ipv6-private, mac-address, tailnet-name: anywhere
    ("notes/ula.md", "peer fd7a:" + "115c:a1e0::1\n", {"ipv6-private"}),
    ("notes/v6-link-local.md", "gateway fe80:" + ":1:2\n", {"ipv6-private"}),
    ("notes/mac.md", "nic " + colons("52", "54", "00", "12", "34", "56") + "\n", {"mac-address"}),
    ("notes/mac-dash.md", "nic " + "-".join(["52", "54", "00", "ab", "cd", "ef"]) + "\n", {"mac-address"}),
    ("notes/tailnet.md", "ssh nas." + "tail1234" + ".ts" + ".net\n", {"tailnet-name"}),
    # k8s-plaintext-secret: a Secret manifest under k8s/, in any of its spellings
    (
        "k8s/demo/creds.yaml",
        "apiVersion: v1\n" + SECRET_KIND + "\nstringData:\n  password: x\n",
        {"k8s-plaintext-secret"},
    ),
    ("k8s/demo/quoted.yaml", 'kind: "' + "Secret" + '"  # quoted, with a comment\n', {"k8s-plaintext-secret"}),
    ("k8s/demo/list.yml", "kind: List\nitems:\n  - " + SECRET_KIND + "\n", {"k8s-plaintext-secret"}),
    # sensitive-file: by path alone
    (".vault_pass", "x\n", {"sensitive-file"}),
    ("vault_pass.txt", "x\n", {"sensitive-file"}),
    ("group_vars/all/local.yml", "x: 1\n", {"sensitive-file"}),
    ("group_vars/all/local.yml.bak", "x: 1\n", {"sensitive-file"}),
    ("inventory", "[nas]\n", {"sensitive-file"}),
    ("inventory.ini", "[nas]\n", {"sensitive-file"}),
    ("notes/MACHINES.md", "machines\n", {"sensitive-file"}),
    ("talosconfig", "context: x\n", {"sensitive-file"}),
    ("logs/20261006-run.log", "run\n", {"sensitive-file"}),
    # Must not match: documentation addresses, loopback, version strings and image tags
    (
        "roles/demo/vars/main.yml",
        "a: "
        + dotted("192", "0", "2", "10")
        + "\nb: "
        + dotted("198", "51", "100", "7")
        + "\nc: "
        + dotted("203", "0", "113", "9")
        + "\n",
        set(),
    ),
    (
        "tests/demo/loopback.py",
        'NAS = "'
        + dotted("127", "0", "0", "1")
        + '"\nPVE = "'
        + dotted("127", "0", "0", "20")
        + '"\nANY = "'
        + dotted("0", "0", "0", "0")
        + '"\n',
        set(),
    ),
    ("README-public.md", "a public resolver, " + dotted("8", "8", "8", "8") + ", outside code and manifests\n", set()),
    (
        "k8s/demo/deployment.yaml",
        "\n".join(
            "image: " + tag
            for tag in [
                "linuxserver/sonarr:4.0.19.2979-ls320",
                "linuxserver/radarr:6.3.0.10514-ls312",
                "linuxserver/prowlarr:2.5.2.5491-ls155",
                "lscr.io/linuxserver/jellyfin:10.11.11ubu2404-ls42",
                "lscr.io/linuxserver/nextcloud:34.0.2-ls444",
                "linuxserver/mariadb:11.4.12-r0-ls224",
            ]
        )
        + "\n",
        set(),
    ),
    (
        "pyproject.toml",
        'dependencies = ["ansible-core==2.21.4", "trove-classifiers==2026.9.21.13"]\n# kubernetes 1.37.1\n',
        set(),
    ),
    # Must not match: a certificate fingerprint is a longer colon-hex run than a MAC address
    (
        "group_vars/all/local.yml.example",
        "truenas_api_cert_sha256: " + colons(*(["AA", "BB", "CC", "DD"] * 8)) + "\n",
        set(),
    ),
    # Must not match: the files each rule allows
    (
        "k8s/homepage/secret.yaml",
        "apiVersion: v1\n" + SECRET_KIND + "\ntype: kubernetes.io/service-account-token\n",
        set(),
    ),
    ("k8s/demo/creds.sops.yaml", SECRET_KIND + "\n", set()),
    ("k8s/demo/sealed-creds.yaml", SECRET_KIND + "\n", set()),
    ("k8s/demo/creds.example.yaml", SECRET_KIND + "\n", set()),
    ("inventory.example", "[nas]\n", set()),
    # Must not match: an inventory below the root (only the root one is never committed), with loopback
    ("tests/demo/inventory.ini", "[nas]\n", set()),
    ("molecule/demo/inventory/hosts.yml", "ansible_host: " + dotted("127", "0", "0", "20") + "\n", set()),
]


def custom_rules():
    return {rule["id"] for rule in tomllib.loads(CONFIG.read_text())["rules"]}


def write_fixtures(root):
    for path, content, _ in FIXTURES:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(content)


def scan(gitleaks, root, mode):
    """{path: set of rule ids} for everything gitleaks reports under root."""
    report = root.parent / f"{mode}-report.json"
    args = [gitleaks, mode]
    if mode == "dir":
        args.append(".")
    else:
        git = [
            "git",
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
        ]
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "add", "-A", "-f"], cwd=root, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "fixtures"], cwd=root, check=True)
    args += [
        "--config",
        str(CONFIG),
        "--no-banner",
        "--exit-code",
        "0",
        "--report-format",
        "json",
        "--report-path",
        str(report),
    ]
    subprocess.run(args, cwd=root, check=True, capture_output=True)
    found = {}
    for finding in json.loads(report.read_text()):
        found.setdefault(finding["File"].removeprefix("./"), set()).add(finding["RuleID"])
    return found


@pytest.fixture(scope="module", params=["dir", "git"])
def findings(request, tmp_path_factory, gitleaks):
    root = tmp_path_factory.mktemp(request.param) / "tree"
    root.mkdir()
    write_fixtures(root)
    return scan(gitleaks, root, request.param)


@pytest.mark.parametrize(("path", "expected"), [(p, e) for p, _, e in FIXTURES], ids=[p for p, _, _ in FIXTURES])
def test_rules_fire_exactly_where_expected(findings, path, expected):
    assert findings.get(path, set()) == expected


def test_nothing_fires_outside_the_fixtures(findings):
    assert set(findings) <= {path for path, _, _ in FIXTURES}


def test_every_custom_rule_has_a_fixture_it_must_catch():
    caught = set().union(*(expected for _, _, expected in FIXTURES))
    assert custom_rules() == caught
