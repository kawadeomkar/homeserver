"""What the policy tests share: copies of the repository with a local.yml of a given setup, and the ansible CLIs run
in them, so a test resolves variables on a host through the real inventory layout, as a run would."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


# A local.yml with only the Proxmox host's address (an RFC 5737 placeholder): no NAS.
PROXMOX_ONLY_LOCAL_YML = "---\nproxmox_address: 192.0.2.20\n"

# local.yml's Kubernetes section, by its variables' prefixes.
KUBERNETES_SETTING = re.compile(r"^(kubernetes_|flux_|sops_|haos_)")

# A Kubernetes section without a NAS: the same two nodes as with one, the worker sized to the host, with a media
# disk.
PROXMOX_ONLY_CLUSTER_LOCAL_YML = PROXMOX_ONLY_LOCAL_YML + (
    "kubernetes_node_addresses: {homeserver-cp-1: 192.0.2.30, homeserver-worker-1: 192.0.2.31}\n"
    "kubernetes_node_sizes:\n"
    "  homeserver-worker-1:\n"
    "    {cores: 4, memory_mib: 12288, system_disk_gib: 32, data_disk_gib: 20, media_disk_gib: 100}\n"
)


def mac(number):
    """A locally administered MAC, built here because gitleaks flags a MAC written out in any tracked file."""
    return ":".join(["02", "00", "00", "00", "00", f"{number:02x}"])


# What local.yml.example's placeholders need before a run accepts them: a MAC for each node, and a repository
# for Flux. The example's own <mac> and <you> are refused on purpose.
CLUSTER_VARS = {
    "kubernetes_node_macs": {"homeserver-cp-1": mac(1), "homeserver-worker-1": mac(2)},
    "flux_sync_url": "https://example.invalid/homeserver.git",
}
CLUSTER_ARGS = ("-e", json.dumps(CLUSTER_VARS))


def example():
    return yaml.safe_load((ROOT / "group_vars" / "all" / "local.yml.example").read_text())


def as_local_yml(settings):
    return "---\n" + yaml.safe_dump(settings, sort_keys=False)


def example_without_kubernetes():
    """local.yml.example with its Kubernetes section deleted, as the example tells a setup without a cluster."""
    return as_local_yml({name: value for name, value in example().items() if not KUBERNETES_SETTING.match(name)})


def blank_nas_local_yml():
    """local.yml.example with every TrueNAS value left blank after its colon, which the example allows,
    and the bootstrap address only a space. Its Kubernetes section is deleted."""
    lines = ["---"]
    for name, value in example().items():
        if name == "truenas_bootstrap_address":
            lines.append(f'{name}: " "')
        elif name.startswith("truenas_"):
            lines.append(f"{name}:")
        elif not KUBERNETES_SETTING.match(name):
            lines.append(yaml.safe_dump({name: value}, default_flow_style=True).strip()[1:-1])
    return "\n".join(lines) + "\n"


def example_tree(tmp_path, local_yml=None):
    """The roles, group_vars and playbooks copied into tmp_path, with local.yml.example or `local_yml` as
    local.yml, and an ansible.cfg that names no vault password file."""
    for name in ["roles", "group_vars"]:
        shutil.copytree(ROOT / name, tmp_path / name, ignore=shutil.ignore_patterns("local.yml", "vault.yml"))
    for playbook in ROOT.glob("*.yml"):
        shutil.copy(playbook, tmp_path)
    shutil.copy(ROOT / "inventory.example", tmp_path)
    local = tmp_path / "group_vars" / "all" / "local.yml"
    if local_yml is None:
        shutil.copy(ROOT / "group_vars" / "all" / "local.yml.example", local)
    else:
        local.write_text(local_yml)
    (tmp_path / "ansible.cfg").write_text("[defaults]\nretry_files_enabled = false\n")
    return tmp_path


def ansible_cli(tree, tool, *args):
    """Run one of the ansible CLIs in `tree`; return its exit code and combined output."""
    env = {**os.environ, "ANSIBLE_CONFIG": str(tree / "ansible.cfg"), "ANSIBLE_NOCOLOR": "1"}
    env.pop("ANSIBLE_FORCE_COLOR", None)
    command = [str(Path(sys.executable).parent / tool), *args]
    result = subprocess.run(command, cwd=tree, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def resolve_on(tree, host, expression, *extra):
    """What a Jinja expression resolves to for `host`, through ansible.builtin.debug: the first JSON object in
    the output, so warnings printed after it do not matter."""
    rc, out = ansible_cli(
        tree,
        "ansible",
        *["-i", "inventory.example", host, "-m", "ansible.builtin.debug", "-a", f"msg={{{{ {expression} }}}}"],
        *["-e", "ansible_connection=local", *extra],
    )
    assert rc == 0, out[-3000:]
    return json.JSONDecoder().raw_decode(out[out.index("{") :])[0]["msg"]
