"""The roles keep the conventions CLAUDE.md sets for them.

- Every role default has an entry in meta/argument_specs.yml with the same default, and every entry a default.
- Each custom module's DOCUMENTATION, with its doc fragments merged in, matches the argument spec its main()
  builds: names, types, defaults, choices, required, and the same for suboptions.
- Each custom module documents its check-mode, diff-mode and platform support as `attributes`, and the
  check-mode support it documents is what its AnsibleModule declares.
- The tracked configuration and examples pass the roles' argument specs and input checks: site.yml runs
  with only the always-tagged validation, against inventory.example, in each of the four setups two switches
  make, with and without a NAS and with and without a cluster: local.yml.example (its placeholders filled in),
  the example without its Kubernetes section, and a local.yml that names only the Proxmox host, with and
  without one, where the NAS play must end by itself. Without a NAS, the storage both VM classes use is the
  one local.yml names, which no tracked file overrides. A TrueNAS section that keeps other settings but gives
  no address stops both playbooks, and so does nas_present given on the command line; an incomplete
  Kubernetes section stops them too, with a message per problem. Deleting the truenas host from the
  inventory, which inventory.example advises against, still gives a working run without a NAS.
- Every task that calls a custom module passes only options the module accepts. ansible-lint's args rule
  cannot load these modules, so nothing else checks it.
- Every option whose name looks like a secret is no_log, in the modules and in the roles' argument specs.
  The idempotence tests cannot see a missing no_log: at default verbosity Ansible prints neither a
  module's arguments nor its results.

What a module passes to AnsibleModule is captured by running its main() with AnsibleModule replaced, so
nothing connects anywhere.
"""

import contextlib
import importlib.util
import json
import re
from pathlib import Path
from unittest import mock

import ansible.module_utils
import pytest
import yaml
from ansible.plugins.loader import fragment_loader, init_plugin_loader
from ansible.utils.collection_loader import AnsibleCollectionConfig
from ansible.utils.plugin_docs import get_docstring
from helpers import (
    CLUSTER_ARGS,
    CLUSTER_VARS,
    PROXMOX_ONLY_CLUSTER_LOCAL_YML,
    PROXMOX_ONLY_LOCAL_YML,
    ansible_cli,
    as_local_yml,
    blank_nas_local_yml,
    example,
    example_tree,
    example_without_kubernetes,
    mac,
    resolve_on,
)

ROOT = Path(__file__).resolve().parents[2]
ROLES = sorted(path.parents[1] for path in ROOT.glob("roles/*/meta/argument_specs.yml"))
MODULES = sorted(ROOT.glob("roles/*/library/*.py"))

# ansible-test's no-log-needed pattern: option names that look like secrets.
SECRET_NAME = re.compile(r"pass(?!ive)|secret|token|key", re.IGNORECASE)
# Options whose names match it but hold nothing secret, as (module or role, option): why.
NOT_SECRET = {
    ("truenas_resource", "key"): "the names of the fields that identify an entry",
    ("flux_bootstrap", "flux_bootstrap_age_key_file"): "the key file's path; the role reads the key, never prints it",
    ("talos", "talos_secrets_file"): "the bundle's path; the role reads the bundle under no_log, never prints it",
    ("proxmox_tailscale", "proxmox_tailscale_keyring"): "the path of Tailscale's public signing key",
}

# The modules import their role's module_utils as ansible.module_utils.<name>, as Ansible ships them.
for _utils in sorted(ROOT.glob("roles/*/module_utils")):
    if str(_utils) not in ansible.module_utils.__path__:
        ansible.module_utils.__path__.append(str(_utils))


class _Captured(Exception):
    pass


def module_settings(path):
    """What a custom module's main() passes to AnsibleModule, captured before AnsibleModule would run."""
    spec = importlib.util.spec_from_file_location(f"_test_roles_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    captured = {}

    def capture(*args, **kwargs):
        captured.update(kwargs)
        if args:
            captured["argument_spec"] = args[0]
        raise _Captured

    with mock.patch.object(module, "AnsibleModule", capture), contextlib.suppress(_Captured):
        module.main()
    assert captured.get("argument_spec"), f"{path.name}: main() never built an AnsibleModule"
    return captured


def module_spec(path):
    """The argument spec a custom module's main() builds."""
    return module_settings(path)["argument_spec"]


def module_doc(path):
    """A custom module's DOCUMENTATION, with its role's doc fragments merged in, as ansible-doc shows it."""
    # The ansible CLIs set up the collection loader that resolves ansible.builtin.* fragments; a test has to.
    if AnsibleCollectionConfig.collection_finder is None:
        init_plugin_loader()
    fragments = path.parents[1] / "doc_fragments"
    if fragments.is_dir():
        fragment_loader.add_directory(str(fragments))
    doc = get_docstring(str(path), fragment_loader, is_module=True)[0]
    assert doc, f"{path.name}: no DOCUMENTATION"
    return doc


def mismatches(documented, spec, where=""):
    """How a module's documented options differ from its argument spec."""
    problems = []
    for name in sorted(set(documented) | set(spec)):
        if name not in spec:
            problems.append(f"{where}{name}: documented, but not in the argument spec")
            continue
        if name not in documented:
            problems.append(f"{where}{name}: in the argument spec, but not documented")
            continue
        doc, arg = documented[name] or {}, spec[name]
        for field, doc_value, arg_value in (
            ("type", doc.get("type", "str"), arg.get("type", "str")),
            ("elements", doc.get("elements"), arg.get("elements")),
            ("default", doc.get("default"), arg.get("default")),
            ("required", bool(doc.get("required")), bool(arg.get("required"))),
            ("choices", sorted(doc.get("choices") or [], key=str), sorted(arg.get("choices") or [], key=str)),
            ("aliases", sorted(doc.get("aliases") or []), sorted(arg.get("aliases") or [])),
        ):
            if doc_value != arg_value:
                problems.append(
                    f"{where}{name}: {field} is {doc_value!r} in the documentation, {arg_value!r} in the spec"
                )
        if doc.get("suboptions") or arg.get("options"):
            problems += mismatches(doc.get("suboptions") or {}, arg.get("options") or {}, f"{where}{name}.")
    return problems


def options_in(spec, prefix=""):
    """(dotted name, name, option) for every option in an argument spec, suboptions included."""
    for name, option in spec.items():
        yield f"{prefix}{name}", name, option
        yield from options_in(option.get("options") or {}, f"{prefix}{name}.")


def tasks_in(tasks):
    """Every task in a task list, including those inside blocks."""
    for task in tasks or []:
        if isinstance(task, dict):
            yield task
            for section in ("block", "rescue", "always"):
                yield from tasks_in(task.get(section))


@pytest.mark.parametrize("role", ROLES, ids=lambda role: role.name)
def test_role_defaults_match_argument_specs(role):
    options = yaml.safe_load((role / "meta" / "argument_specs.yml").read_text())["argument_specs"]["main"]["options"]
    defaults = yaml.safe_load((role / "defaults" / "main.yml").read_text()) or {}
    assert sorted(set(defaults) - set(options)) == [], "defaults with no argument-spec entry"
    assert sorted(set(options) - set(defaults)) == [], "argument-spec entries with no default"
    differ = {name: (value, options[name].get("default")) for name, value in defaults.items()}
    assert {name: pair for name, pair in differ.items() if pair[0] != pair[1]} == {}, (
        "defaults/main.yml and the argument spec disagree (defaults, spec)"
    )


@pytest.mark.parametrize("path", MODULES, ids=lambda path: path.stem)
def test_module_docs_match_argspec(path):
    assert mismatches(module_doc(path).get("options") or {}, module_spec(path)) == []


@pytest.mark.parametrize("path", MODULES, ids=lambda path: path.stem)
def test_module_attributes_match_ansible_module(path):
    attributes = module_doc(path).get("attributes") or {}
    assert sorted({"check_mode", "diff_mode", "platform"} - set(attributes)) == [], "attributes not documented"
    for name in ("check_mode", "diff_mode"):
        assert attributes[name].get("support") in {"full", "partial", "none"}, f"{name}: no valid support level"
    supports_check_mode = module_settings(path).get("supports_check_mode", False)
    assert (attributes["check_mode"]["support"] != "none") == supports_check_mode, (
        f"check_mode is documented as {attributes['check_mode']['support']!r}, "
        f"but AnsibleModule has supports_check_mode={supports_check_mode}"
    )


# local.yml.example without its Kubernetes section, and the line that turns on remote access through Tailscale,
# which it leaves commented.
EXAMPLE_LOCAL_YML = example_without_kubernetes()
TAILSCALE_LOCAL_YML = "tailscale_advertise_routes: [192.0.2.0/24]\n"
TAILSCALE_CHECK = "TASK [proxmox_tailscale : Check the role's inputs]"


def argspec_pass(tree, playbook="site.yml", *extra):
    """Run `playbook` with only the tasks tagged `always`: the argument-spec validation Ansible adds to each
    role, and the roles' and playbooks' own input checks. truenas_info, also always, would connect to the NAS."""
    return ansible_cli(
        tree,
        "ansible-playbook",
        *["-i", "inventory.example", playbook, "--tags", "__argspec_only__", "--skip-tags", "truenas_info"],
        *["-e", "ansible_connection=local", "-e", "vault_truenas_api_key=stub"],
        *["-e", "vault_truenas_mail_password=stub", "-e", "vault_haos_owner_password=stub-password", *extra],
    )


NAS_PLAY_RUNS = ["validate | Check the connection settings", "skipping: [truenas]"]
NAS_PLAY_ENDS = ["Skip the NAS when local.yml gives it no address", "ok: [truenas]"]
NAS_ROLE = ["TASK [truenas :", "validate | Check the connection settings"]
# What the Proxmox play says when a VM has no backup: without a NAS, a cluster and no vm_backup_storage.
NO_BACKUP = "are not backed up, because this setup has no NAS"


# Two switches, four setups: a NAS or not (the NAS addresses), a cluster or not (the node addresses). Each
# states how many roles validate their arguments.
@pytest.mark.parametrize(
    ("local_yml", "extra", "validations", "present", "absent"),
    [
        pytest.param(None, CLUSTER_ARGS, 10, [*NAS_PLAY_RUNS, TAILSCALE_CHECK], [NO_BACKUP], id="nas-cluster-haos"),
        pytest.param(EXAMPLE_LOCAL_YML, (), 6, [*NAS_PLAY_RUNS, TAILSCALE_CHECK], [NO_BACKUP], id="nas"),
        pytest.param(
            EXAMPLE_LOCAL_YML + TAILSCALE_LOCAL_YML,
            (),
            6,
            [NAS_PLAY_RUNS[0], TAILSCALE_CHECK],
            [NO_BACKUP],
            id="nas-tailscale",
        ),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML,
            CLUSTER_ARGS,
            9,
            [*NAS_PLAY_ENDS, NO_BACKUP, TAILSCALE_CHECK],
            NAS_ROLE,
            id="proxmox-only-cluster-haos",
        ),
        pytest.param(
            PROXMOX_ONLY_LOCAL_YML, (), 5, [*NAS_PLAY_ENDS, TAILSCALE_CHECK], [*NAS_ROLE, NO_BACKUP], id="proxmox-only"
        ),
        pytest.param(
            PROXMOX_ONLY_LOCAL_YML + f'haos_mac: "{mac(9)}"\n',
            (),
            7,
            [*NAS_PLAY_ENDS, NO_BACKUP, TAILSCALE_CHECK],
            NAS_ROLE,
            id="proxmox-only-haos",
        ),
        pytest.param(
            PROXMOX_ONLY_LOCAL_YML + TAILSCALE_LOCAL_YML,
            (),
            5,
            [NAS_PLAY_ENDS[0], TAILSCALE_CHECK],
            ["TASK [truenas :", NO_BACKUP],
            id="proxmox-only-tailscale",
        ),
        pytest.param(
            blank_nas_local_yml(), (), 5, NAS_PLAY_ENDS, [*NAS_ROLE, NO_BACKUP], id="proxmox-only-blank-values"
        ),
    ],
)
def test_examples_satisfy_argument_specs(tmp_path, local_yml, extra, validations, present, absent):
    """The tracked configuration passes every role's checks in each setup: with local.yml.example for
    local.yml (its MAC and repository placeholders filled in), without its Kubernetes section, and with a
    local.yml that names only the Proxmox host, with a Kubernetes section or without, or leaves every TrueNAS
    value blank, where the NAS play must end before its role runs."""
    rc, out = argspec_pass(example_tree(tmp_path, local_yml), "site.yml", *extra)
    assert rc == 0, out[-3000:]
    assert out.count("Validating arguments against arg spec 'main'") == validations, out[-3000:]
    for text in present:
        assert text in out, f"{text!r} missing\n{out[-3000:]}"
    for text in absent:
        assert text not in out, f"{text!r} present\n{out[-3000:]}"


@pytest.mark.parametrize("playbook", ["site.yml", "proxmox.yml"])
def test_nas_settings_without_an_address_are_refused(tmp_path, playbook):
    """A TrueNAS section that keeps its other settings but has no address, as a misspelt address leaves it,
    stops both playbooks before any role runs, instead of passing for a setup without a NAS."""
    example = (ROOT / "group_vars" / "all" / "local.yml.example").read_text()
    misspelt = re.sub(r"^truenas_static_address:", "truenas_static_adress:", example, flags=re.MULTILINE)
    misspelt = re.sub(r"^truenas_bootstrap_address:.*\n", "", misspelt, flags=re.MULTILINE)
    assert "\ntruenas_static_adress:" in misspelt and "\ntruenas_bootstrap_address:" not in misspelt
    rc, out = argspec_pass(example_tree(tmp_path, misspelt), playbook)
    assert rc != 0, out[-3000:]
    assert "but neither truenas_static_address nor truenas_bootstrap_address" in out, out[-3000:]
    assert "truenas_gateway" in out, out[-3000:]
    assert "TASK [truenas :" not in out and "TASK [proxmox_" not in out, out[-3000:]


def test_inventory_without_the_nas_host(tmp_path):
    """inventory.example says to keep the truenas host without a NAS, but a user who deletes it still gets a
    working run: the NAS play matches no host, and the Proxmox play runs as it does without a NAS."""
    tree = example_tree(tmp_path, PROXMOX_ONLY_LOCAL_YML)
    inventory = tree / "inventory.example"
    inventory.write_text(re.sub(r"^truenas\n", "", inventory.read_text(), flags=re.MULTILINE))
    rc, out = argspec_pass(tree)
    assert rc == 0, out[-3000:]
    assert "skipping: no hosts matched" in out, out[-3000:]
    assert out.count("Validating arguments against arg spec 'main'") == 5, out[-3000:]


@pytest.mark.parametrize(
    "playbook", ["site.yml", "talos-upgrade.yml", "etcd-snapshot.yml", "suspend.yml", "resume.yml"]
)
@pytest.mark.parametrize(
    ("local_yml", "extra", "refused"),
    [
        pytest.param(PROXMOX_ONLY_LOCAL_YML, (), False, id="no-cluster"),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML,
            ("-e", json.dumps({k: v for k, v in CLUSTER_VARS.items() if k != "haos_mac"})),
            True,
            id="cluster",
        ),
    ],
)
def test_inventory_without_the_kubernetes_group(tmp_path, playbook, local_yml, extra, refused):
    """An inventory made before the cluster has no kubernetes group. Without a cluster it still works; with one the
    run is refused before any VM is made, rather than creating the VMs and matching no host to build Talos on, and
    an upgrade, a snapshot, make suspend or make resume is refused rather than matching no host and ending without a
    word. (Home Assistant's VM, left out here, needs no such group.)"""
    tree = example_tree(tmp_path, local_yml)
    inventory = tree / "inventory.example"
    inventory.write_text(re.sub(r"(?ms)^# The Kubernetes cluster.*\Z", "", inventory.read_text()))
    assert "kubernetes" not in inventory.read_text()
    rc, out = argspec_pass(tree, playbook, *extra)
    assert (rc != 0) == refused, out[-3000:]
    if refused:
        assert "add [kubernetes] and homeserver-k8s to" in out and "TASK [proxmox_vm" not in out, out[-3000:]


@pytest.mark.parametrize("playbook", ["talos-upgrade.yml", "etcd-snapshot.yml"])
@pytest.mark.parametrize(
    ("local_yml", "extra", "skipped"),
    [
        pytest.param(None, CLUSTER_ARGS, False, id="nas-cluster"),
        pytest.param(PROXMOX_ONLY_CLUSTER_LOCAL_YML, CLUSTER_ARGS, False, id="proxmox-only-cluster"),
        pytest.param(EXAMPLE_LOCAL_YML, (), True, id="no-cluster"),
    ],
)
def test_cluster_playbooks_pass_their_checks(tmp_path, playbook, local_yml, extra, skipped):
    """The upgrade and snapshot playbooks check the same settings as kubernetes.yml, and pass them in each setup;
    without a cluster they say so and end."""
    rc, out = argspec_pass(example_tree(tmp_path, local_yml), playbook, *extra)
    assert rc == 0, out[-3000:]
    assert ("so this setup has no cluster" in out) == skipped, out[-3000:]


@pytest.mark.parametrize("playbook", ["site.yml", "proxmox.yml"])
def test_nas_present_cannot_be_set(tmp_path, playbook):
    """nas_present is worked out from the addresses. Given with -e it is a string, and "false" would count as
    true, so each playbook's guard refuses it rather than run with the NAS it was meant to turn off. In site.yml
    the NAS play's guard stops the whole run, since that play has no host left."""
    rc, out = argspec_pass(example_tree(tmp_path), playbook, "-e", "nas_present=false")
    assert rc != 0, out[-3000:]
    assert "nas_present is worked out from the NAS addresses" in out, out[-3000:]
    assert "TASK [truenas :" not in out and "TASK [proxmox_" not in out, out[-3000:]


@pytest.mark.parametrize(
    ("local_yml", "setting", "storage"),
    [
        pytest.param(PROXMOX_ONLY_LOCAL_YML, "", "local-lvm", id="lvm-default"),
        pytest.param(
            PROXMOX_ONLY_LOCAL_YML, "proxmox_local_vm_storage: local-zfs\n", "local-zfs", id="zfs-from-local-yml"
        ),
        pytest.param(PROXMOX_ONLY_LOCAL_YML, "proxmox_local_vm_storage:\n", "local-lvm", id="blank-is-unset"),
        pytest.param(PROXMOX_ONLY_LOCAL_YML, 'proxmox_local_vm_storage: " "\n', "local-lvm", id="whitespace-is-unset"),
        pytest.param(None, "proxmox_local_vm_storage: local-zfs\n", "local-zfs", id="nas-and-a-cluster-on-zfs"),
        pytest.param(example_without_kubernetes(), "proxmox_local_vm_storage: local-zfs\n", None, id="nas-no-cluster"),
    ],
)
def test_local_yml_names_the_installer_storage(tmp_path, local_yml, setting, storage):
    """The installer's storage is local-lvm, or what local.yml names, a blank value counting as unset. Without a NAS
    every VM uses it, and with a cluster in either setup the nodes' system disks do, so the Proxmox play checks it
    then, and only then. A tracked group_vars file that set it too would win over local.yml, which loads first."""
    base = (ROOT / "group_vars" / "all" / "local.yml.example").read_text() if local_yml is None else local_yml
    tree = example_tree(tmp_path, base + setting)
    checked = resolve_on(tree, "homeserver", "proxmox_storage_local", *CLUSTER_ARGS)
    assert checked == ([storage] if storage else []), checked


@pytest.mark.parametrize(
    ("local_yml", "classes", "exports", "local"),
    [
        pytest.param(
            None,
            ["persistent", "ephemeral"],
            [
                {"id": "homeserver-persistent", "export": "/mnt/persistent/proxmox/vm"},
                {"id": "homeserver-ephemeral", "export": "/mnt/ephemeral/proxmox/vm"},
                {"id": "claude-on-proxmox-ephemeral", "export": "/mnt/ephemeral/proxmox/claude-on-proxmox"},
                {"id": "homeserver-backup", "content": ["backup"], "export": "/mnt/persistent/proxmox/backup"},
            ],
            ["local-lvm"],
            id="nas",
        ),
        pytest.param(PROXMOX_ONLY_LOCAL_YML, ["ephemeral"], [], ["local-lvm"], id="proxmox-only"),
    ],
)
def test_layout_resolves_per_setup(tmp_path, local_yml, classes, exports, local):
    """With a NAS both VM classes exist and each export names a storage for its owner and class, never its
    backend; the cluster's system disks go on the installer's storage, which is checked, and the start at boot
    waits for every export that holds VM disks. Without one only the ephemeral class exists, there is nothing to
    export or wait for, and the installer's storage is the one to check."""
    tree = example_tree(tmp_path, local_yml)
    layout = resolve_on(
        tree,
        "homeserver",
        "{'classes': vm_storage | list, 'nfs': proxmox_storage_nfs, 'local': proxmox_storage_local,"
        " 'wait': proxmox_storage_startall_wait}",
    )
    wait = [e["id"] for e in exports if "content" not in e]
    assert layout == {"classes": classes, "nfs": exports, "local": local, "wait": wait}


NAS_NODES = {"homeserver-cp-1": (300, 32, None, None), "homeserver-worker-1": (301, 64, 200, None)}
LOCAL_NODES = {"homeserver-cp-1": (300, 32, None, None), "homeserver-worker-1": (301, 32, 20, 100)}
ZFS = "proxmox_local_vm_storage: local-zfs\n"


@pytest.mark.parametrize(
    ("local_yml", "nodes", "system"),
    [
        pytest.param(None, NAS_NODES, "local-lvm", id="nas"),
        pytest.param(PROXMOX_ONLY_CLUSTER_LOCAL_YML, LOCAL_NODES, "local-lvm", id="proxmox-only"),
        pytest.param("nas-zfs", NAS_NODES, "local-zfs", id="nas-zfs"),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML + ZFS,
            LOCAL_NODES,
            "local-zfs",
            id="proxmox-only-zfs",
        ),
    ],
)
def test_vms_resolve_on_the_proxmox_host(tmp_path, local_yml, nodes, system):
    """The cluster's VMs and ISO resolve on the Proxmox host through the real inventory layout, from what that host's
    groups see: the version pins live in group_vars/all, since the Proxmox host never sees group_vars/kubernetes.
    Both setups have the same two VMs, and their system disks go on the installer's storage, local-zfs on a ZFS
    install. With a NAS the worker's data disk is on the persistent NAS storage, in qcow2; without one, every disk is on
    the installer's storage and the worker's media disk is never backed up."""
    nas = local_yml in (None, "nas-zfs")
    if local_yml == "nas-zfs":
        local_yml = (ROOT / "group_vars" / "all" / "local.yml.example").read_text() + ZFS
    tree = example_tree(tmp_path, local_yml)
    isos, vms = resolve_on(tree, "homeserver", "[proxmox_vm_isos, proxmox_vm_vms]", *CLUSTER_ARGS)
    assert [iso["filename"] for iso in isos] == ["talos-v1.14.2-ce4c9805-metal-amd64.iso"]
    found = {}
    for vm in vms:
        disks = {d["slot"]: d for d in vm["disks"]}
        found[vm["name"]] = (
            vm["vmid"],
            disks["scsi0"]["size_gib"],
            disks.get("scsi1", {}).get("size_gib"),
            disks.get("scsi2", {}).get("size_gib"),
        )
        assert vm["net"]["mac"] == CLUSTER_VARS["kubernetes_node_macs"][vm["name"]]
        assert vm["iso"] == "local:iso/talos-v1.14.2-ce4c9805-metal-amd64.iso" and vm["hostpci"] == []
        assert disks["scsi0"]["storage"] == system and vm["efidisk_storage"] == system
        assert vm["tags"] == ["talos", "k8s"]
        if "scsi1" in disks:
            assert (disks["scsi1"]["storage"], disks["scsi1"].get("format")) == (
                ("homeserver-persistent", "qcow2") if nas else (system, None)
            )
        if "scsi2" in disks:
            assert disks["scsi2"]["backup"] is False
    assert found == nodes


def test_talos_pins_match_the_makefile_and_ci():
    """The Kubernetes version Talos runs is the one kubeconform validates the manifests against, and the talosctl CI
    validates the Talos configurations with is the pinned release."""
    pins = yaml.safe_load((ROOT / "group_vars" / "all" / "kubernetes.yml").read_text())
    makefile = (ROOT / "Makefile").read_text()
    assert re.search(rf"^KUBERNETES_VERSION \?= {re.escape(str(pins['talos_kubernetes_version']))}$", makefile, re.M)
    assert pins["talos_contract"] == ".".join(pins["talos_version"].split(".")[:2])
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    assert "v" + ci["env"]["TALOSCTL_VERSION"] == pins["talos_version"]


@pytest.mark.parametrize(
    ("local_yml", "nodes"),
    [
        pytest.param(None, {"homeserver-cp-1": ("controlplane", 1), "homeserver-worker-1": ("worker", 2)}, id="nas"),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML,
            {"homeserver-cp-1": ("controlplane", 1), "homeserver-worker-1": ("worker", 3)},
            id="proxmox-only",
        ),
    ],
)
def test_talos_nodes_resolve_on_the_kubernetes_host(tmp_path, local_yml, nodes):
    """The Talos nodes resolve on the kubernetes host through the real inventory layout, from what its groups see:
    the version pins and the NAS address are in group_vars/all, since this host never sees group_vars/proxmox.
    Both setups have the same two nodes, the worker with a media volume without a NAS, and the control plane keeps
    its taint in both."""
    tree = example_tree(tmp_path, local_yml)
    found = resolve_on(
        tree,
        "homeserver-k8s",
        "{'nodes': talos_nodes, 'endpoint': talos_endpoint, 'version': talos_version, "
        "'cp': talos_patches_controlplane | join('\\n'), 'patches': talos_patches_common + talos_patches_controlplane}",
        *CLUSTER_ARGS,
        # What roles/talos learns from talosctl as it renders; a documentation range here.
        "-e",
        json.dumps({"talos_pod_subnets": ["198.51.100.0/24"]}),
    )
    assert {n["name"]: (n["role"], len(n["patches"])) for n in found["nodes"]} == nodes
    assert found["endpoint"] == "https://192.0.2.30:6443" and found["version"] == "v1.14.2"
    assert "$patch: delete" not in found["cp"] and "KubeNodeConfig" not in found["cp"]
    # Without its policy agent, Flannel would let every NetworkPolicy through unenforced.
    assert "kind: KubeFlannelCNIConfig\nkubeNetworkPoliciesEnabled: true" in found["cp"]
    # The firewall: the two APIs open, the rest to the nodes (and the pod network), etcd to the control plane alone.
    docs = [yaml.safe_load(patch) for patch in found["patches"]]
    rules = {doc["name"]: doc for doc in docs if doc.get("kind") == "NetworkRuleConfig"}
    assert {"apiVersion": "v1alpha1", "kind": "NetworkDefaultActionConfig", "ingress": "block"} in docs
    nodes_32 = [{"subnet": f"192.0.2.{30 + i}/32"} for i in range(len(nodes))]
    pods = {"subnet": "198.51.100.0/24"}
    anywhere = [{"subnet": "0.0.0.0/0"}, {"subnet": "::/0"}]
    assert rules["talos-api"]["ingress"] == rules["kubernetes-api"]["ingress"] == anywhere
    assert rules["kubelet"]["ingress"] == rules["node-metrics"]["ingress"] == [*nodes_32, pods]
    assert rules["flannel-vxlan"]["ingress"] == rules["trustd"]["ingress"] == nodes_32
    assert rules["etcd"]["ingress"] == [{"subnet": "192.0.2.30/32"}]


MEDIA_ON_THE_DATA_VOLUME = (
    PROXMOX_ONLY_CLUSTER_LOCAL_YML.replace(", media_disk_gib: 100", "") + "kubernetes_media_on_data_volume: true\n"
)


@pytest.mark.parametrize(
    ("local_yml", "path", "media"),
    [
        pytest.param(None, "k8s/clusters/nas", None, id="nas"),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML,
            "k8s/clusters/local",
            ("/var/mnt/media/library", "/var/mnt/media/frigate"),
            id="proxmox-only-media-disk",
        ),
        pytest.param(
            MEDIA_ON_THE_DATA_VOLUME,
            "k8s/clusters/local",
            ("/var/mnt/local-path-provisioner/static/media", "/var/mnt/local-path-provisioner/static/frigate-media"),
            id="proxmox-only-media-on-the-data-volume",
        ),
    ],
)
def test_flux_settings_resolve_on_the_kubernetes_host(tmp_path, local_yml, path, media):
    """Flux's settings resolve on the kubernetes host through the real inventory layout: the NAS's address is in
    group_vars/all, since this host never sees group_vars/proxmox. Each setup syncs its own cluster directory, which
    exists and builds, and every value is a string, since Flux substitutes text. Without a NAS the media paths follow
    the Talos volume that holds the media: the media disk's, or the data volume's."""
    tree = example_tree(tmp_path, local_yml)
    settings, instance, volumes = resolve_on(
        tree,
        "homeserver-k8s",
        "[flux_bootstrap_settings, flux_bootstrap_instance, talos_nodes | map(attribute='patches') | flatten"
        " | select('search', 'kind: UserVolumeConfig') | map('regex_search', 'name: (\\S+)', '\\1') | flatten]",
        *CLUSTER_ARGS,
    )
    common = {"DOMAIN", "TZ", "LB_RANGE", "LB_TRAEFIK_IP", "LB_FRIGATE_IP", "DATA_NODE"}
    common |= {
        "GPU_I915",
        "FRIGATE_DETECTOR_DEVICE",
        "INGRESS_ALLOWED_RANGE",
        "EGRESS_DENIED_ADDRESS",
        "PROXMOX_ADDRESS",
    }
    common |= {"PROMETHEUS_RETENTION", "PROMETHEUS_RETENTION_SIZE"}
    storage = {"NAS_ADDRESS", "NAS_POOL_PERSISTENT", "NAS_POOL_EPHEMERAL", "MEDIA_DATASET"}
    assert set(settings) == common | (storage if media is None else {"MEDIA_LOCAL_PATH", "FRIGATE_LOCAL_PATH"})
    # Nobody until local.yml names the range: a new cluster's apps are set up through port forwards first.
    assert settings["INGRESS_ALLOWED_RANGE"] == "127.0.0.1/32"
    # The NAS with one, the Proxmox host without: the host no app pod may reach.
    assert settings["EGRESS_DENIED_ADDRESS"] == ("192.0.2.10" if local_yml is None else "192.0.2.20")
    assert settings["PROXMOX_ADDRESS"] == "192.0.2.20"
    if media is None:
        assert settings["NAS_ADDRESS"] == "192.0.2.10" and settings["MEDIA_DATASET"] == "/mnt/ephemeral/jellyfin"
        assert (settings["PROMETHEUS_RETENTION"], settings["PROMETHEUS_RETENTION_SIZE"]) == ("15d", "15GB")
    else:
        assert (settings["PROMETHEUS_RETENTION"], settings["PROMETHEUS_RETENTION_SIZE"]) == ("7d", "5GB")
    assert all(isinstance(value, str) and value for value in settings.values()), settings
    assert instance["path"] == path and instance["url"] == CLUSTER_VARS["flux_sync_url"]
    assert settings["DATA_NODE"] == "homeserver-worker-1"
    if media:
        assert (settings["MEDIA_LOCAL_PATH"], settings["FRIGATE_LOCAL_PATH"]) == media
        # Each path lies on a Talos volume the node has: /var/mnt/<volume>.
        for value in media:
            assert value.split("/")[3] in volumes, (value, volumes)
    # The directory Flux syncs is there, and everything its kustomization.yaml lists exists.
    kustomization = yaml.safe_load((ROOT / path / "kustomization.yaml").read_text())
    assert kustomization["kind"] == "Kustomization"
    assert all((ROOT / path / resource).exists() for resource in kustomization.get("resources") or []), kustomization


def test_nas_gets_a_dataset_and_share_per_vm_storage(tmp_path):
    """With a NAS, the NAS configuration derives one dataset and one share for each VM class and each project
    pool, so an export and the storage that mounts it come from one entry. A project pool gets the ephemeral
    class's properties. Without a cluster, nothing is created or exported for one."""
    tree = example_tree(tmp_path, example_without_kubernetes())
    nas = resolve_on(
        tree,
        "truenas",
        "{'datasets': truenas_datasets | map(attribute='name') | list,"
        " 'pool': (truenas_datasets | selectattr('name', 'equalto', 'ephemeral/proxmox/claude-on-proxmox')"
        " | first).properties,"
        " 'shares': truenas_nfs_shares | map(attribute='path') | list}",
    )
    assert nas["datasets"] == [
        "persistent/proxmox",
        "persistent/proxmox/vm",
        "persistent/proxmox/backup",
        "ephemeral/proxmox",
        "ephemeral/proxmox/vm",
        "ephemeral/proxmox/claude-on-proxmox",
    ]
    assert nas["pool"] == {"sync": "DISABLED", "recordsize": "64K", "quota": 0}
    assert nas["shares"] == [
        "/mnt/persistent/proxmox/vm",
        "/mnt/ephemeral/proxmox/vm",
        "/mnt/persistent/proxmox/backup",
        "/mnt/ephemeral/proxmox/claude-on-proxmox",
    ]


def test_nas_gets_a_dataset_and_share_per_cluster_volume(tmp_path):
    """With a NAS and a cluster, the datasets the cluster mounts are created with their properties, apart from
    the media dataset, which exists already and is created only when missing (a lost pool made anew), and each is
    exported to the worker alone."""
    tree = example_tree(tmp_path)
    nas = resolve_on(
        tree,
        "truenas",
        "{'datasets': truenas_datasets_kubernetes, 'shares': truenas_nfs_shares_kubernetes,"
        " 'all': truenas_datasets | map(attribute='name') | list}",
    )
    common = {"compression": "LZ4", "atime": "OFF", "acltype": "POSIX", "aclmode": "DISCARD", "recordsize": "1M"}
    assert nas["datasets"] == [
        {"name": "persistent/nextcloud", "properties": {**common, "sync": "STANDARD", "quota": 0}},
        {"name": "persistent/photoprism", "properties": {**common, "sync": "STANDARD", "quota": 0}},
        {"name": "ephemeral/frigate", "properties": {**common, "sync": "DISABLED", "quota": "500G"}},
        {"name": "ephemeral/jellyfin", "properties": {**common, "sync": "DISABLED", "quota": 0}, "create_only": True},
    ]
    assert "ephemeral/jellyfin" in nas["all"]
    worker = example()["kubernetes_node_addresses"]["homeserver-worker-1"]
    assert [(s["path"], s["hosts"]) for s in nas["shares"]] == [
        ("/mnt/persistent/nextcloud", [worker]),
        ("/mnt/persistent/photoprism", [worker]),
        ("/mnt/ephemeral/frigate", [worker]),
        ("/mnt/ephemeral/jellyfin", [worker]),
    ]


def test_vm_reservation_on_the_ephemeral_pool(tmp_path):
    """The VMs' dataset on the ephemeral pool gets a reservation when one is set, so the media dataset beside it
    cannot fill the pool under them; a reservation counts the descendants, where the VMs' disks are. Unset, it is
    sent as 0, none, so a reservation removed from the configuration is removed on the NAS too."""
    tree = example_tree(tmp_path)
    expression = "(truenas_datasets | selectattr('name', 'equalto', 'ephemeral/proxmox') | first).properties"
    assert resolve_on(tree, "truenas", expression)["reservation"] == 0
    properties = resolve_on(tree, "truenas", expression, "-e", "vm_storage_ephemeral_reservation=200G")
    assert properties["reservation"] == "200G" and "refreservation" not in properties


def test_pool_names_follow_local_yml(tmp_path):
    """The pools are named by class unless local.yml names them otherwise, and every pool, dataset, share and
    Proxmox export follows those names, so no tracked file has to name anyone's hardware."""
    pools = "truenas_pool_persistent: tank\ntruenas_pool_ephemeral: scratch\n"
    tree = example_tree(tmp_path, (ROOT / "group_vars" / "all" / "local.yml.example").read_text() + pools)
    nas = resolve_on(
        tree,
        "truenas",
        "{'pools': truenas_pools | map(attribute='name') | list,"
        " 'datasets': truenas_datasets | map(attribute='name') | list}",
    )
    assert nas == {
        "pools": ["tank", "scratch"],
        "datasets": [
            "tank/proxmox",
            "tank/proxmox/vm",
            "tank/proxmox/backup",
            "scratch/proxmox",
            "scratch/proxmox/vm",
            "scratch/proxmox/claude-on-proxmox",
            "tank/nextcloud",
            "tank/photoprism",
            "scratch/frigate",
            "scratch/jellyfin",
        ],
    }
    exports = resolve_on(tree, "homeserver", "proxmox_storage_nfs | map(attribute='export') | list")
    assert exports == [
        "/mnt/tank/proxmox/vm",
        "/mnt/scratch/proxmox/vm",
        "/mnt/scratch/proxmox/claude-on-proxmox",
        "/mnt/tank/proxmox/backup",
    ]


@pytest.mark.parametrize(
    "setting", ["truenas_pool_persistent: tank", "truenas_mail_server: smtp.example.org", "truenas_alert_email: a@b.c"]
)
def test_nas_settings_given_without_an_address_are_refused(tmp_path, setting):
    """Pool names and the alert mail are TrueNAS settings: given without a NAS address they read as a misspelt
    address."""
    tree = example_tree(tmp_path, PROXMOX_ONLY_LOCAL_YML + setting + "\n")
    rc, out = argspec_pass(tree, "proxmox.yml")
    assert rc != 0, out[-3000:]
    assert "but neither truenas_static_address nor truenas_bootstrap_address" in out, out[-3000:]
    assert setting.split(":")[0] in out, out[-3000:]


@pytest.mark.parametrize(
    ("local_yml", "datasets"),
    [
        pytest.param(None, ["persistent/proxmox/vm", "persistent/nextcloud", "persistent/photoprism"], id="cluster"),
        pytest.param(example_without_kubernetes(), ["persistent/proxmox/vm"], id="no-cluster"),
    ],
)
def test_snapshot_tasks_per_setup(tmp_path, local_yml, datasets):
    """The persistent VMs' disks are snapshotted hourly and daily; with a cluster, its irreplaceable datasets
    hourly, daily and weekly; nothing on the ephemeral pool, ever. Every task that keeps snapshots longer than the
    hourly ones allows empty snapshots: the daily and weekly tasks fire with an hourly one, and TrueNAS destroys a
    batch whose snapshots are all empty unless one of its tasks allows them, so an idle dataset would keep none."""
    tasks = resolve_on(example_tree(tmp_path, local_yml), "truenas", "truenas_snapshot_tasks")
    found = sorted(
        {
            (t["dataset"], t["naming_schema"].split("-")[0], t["lifetime_value"], t["lifetime_unit"], t["allow_empty"])
            for t in tasks
        }
    )
    expected = [
        ("persistent/proxmox/vm", "daily", 7, "DAY", True),
        ("persistent/proxmox/vm", "hourly", 1, "DAY", False),
    ]
    for d in datasets[1:]:
        expected += [(d, "daily", 30, "DAY", True), (d, "hourly", 2, "DAY", False), (d, "weekly", 12, "WEEK", True)]
    assert found == sorted(expected)
    assert not [t for t in tasks if t["dataset"].startswith("ephemeral/")]


@pytest.mark.parametrize(
    ("given", "message"),
    [
        (
            {"truenas_mail_server": "smtp.example.org", "truenas_mail_user": "nas"},
            "sets truenas_mail_server but no sender address",
        ),
        ({"truenas_alert_email": "a@b.c"}, "sets truenas_alert_email but no truenas_mail_server"),
    ],
)
def test_alert_mail_needs_a_sender_and_a_server(tmp_path, given, message):
    """TrueNAS refuses mail settings without a sender address, and an alert address has nothing to go through
    without a server: both are refused before the NAS play runs, and pass once completed."""
    tree = example_tree(tmp_path)
    assert message in resolve_on(tree, "truenas", "nas_section_error", "-e", json.dumps(given))
    completed = given | {"truenas_mail_server": "smtp.example.org", "truenas_mail_from": "nas@example.org"}
    assert resolve_on(tree, "truenas", "nas_section_error", "-e", json.dumps(completed)) == ""


def test_backup_storage_is_for_a_setup_without_a_nas(tmp_path):
    """vm_backup_storage names where the backup goes without a NAS; with one it goes to the NAS, so the setting
    would be ignored: it is refused."""
    tree = example_tree(tmp_path)
    given = {"vm_backup_storage": "usb-backup"}
    assert "sets vm_backup_storage, which is for a setup without a NAS" in resolve_on(
        tree, "truenas", "nas_section_error", "-e", json.dumps(given)
    )
    assert resolve_on(tree, "truenas", "nas_section_error") == ""


def test_alert_mail_follows_local_yml(tmp_path):
    """No mail server in local.yml, no mail settings and no alert service; with one, both, with authentication
    only when a user is given. The password comes from the vault."""
    tree = example_tree(tmp_path, example_without_kubernetes())
    assert resolve_on(tree, "truenas", "[truenas_mail, truenas_alert_services]") == [{}, []]
    given = {
        "truenas_mail_server": "smtp.example.org",
        "truenas_mail_user": "nas@example.org",
        "truenas_alert_email": "a@b.c",
    }
    mail, alerts = resolve_on(tree, "truenas", "[truenas_mail, truenas_alert_services]", "-e", json.dumps(given))
    assert mail == {
        "outgoingserver": "smtp.example.org",
        "port": 587,
        "security": "TLS",
        "smtp": True,
        "user": "nas@example.org",
        "fromemail": "nas@example.org",
    }
    assert alerts == [
        {"name": "Email", "attributes": {"type": "Mail", "email": "a@b.c"}, "level": "WARNING", "enabled": True}
    ]


def without(settings, *names):
    return {name: value for name, value in settings.items() if name not in names}


# Each problem with local.yml's Kubernetes section, and what the message says. Most are checked by resolving
# kubernetes_settings_error; the playbooks' assert on it is checked below.
@pytest.mark.parametrize(
    ("local_yml", "extra", "expected"),
    [
        pytest.param(None, CLUSTER_VARS, [], id="example-filled-in"),
        pytest.param(
            as_local_yml(example() | {"kubernetes_node_addresses": {"homeserver-cp-1": "192.0.2.30"}}),
            CLUSTER_VARS,
            [
                "kubernetes_node_addresses must give an address to each node of the cluster, with a NAS or without",
                "It gives none to homeserver-worker-1.",
            ],
            id="nas-without-the-worker",
        ),
        pytest.param(
            as_local_yml(
                {"proxmox_address": "192.0.2.20", "kubernetes_node_addresses": {"homeserver-cp-1": "192.0.2.30"}}
                | {"kubernetes_node_sizes": {"homeserver-cp-1": {"cores": 4, "data_disk_gib": 20}}}
            ),
            CLUSTER_VARS,
            [
                "It gives none to homeserver-worker-1.",
                "homeserver-cp-1 runs the control plane alone and takes no data or media disk",
                "kubernetes_node_sizes must give homeserver-worker-1, which runs every app, cores, memory_mib",
            ],
            id="proxmox-only-single-node-as-before",
        ),
        pytest.param(
            None,
            CLUSTER_VARS
            | {
                "kubernetes_node_macs": {
                    "homeserver-cp-1": mac(0xAB).upper(),
                    "homeserver-worker-1": "00:" + mac(2)[3:],
                }
            },
            [
                "kubernetes_node_macs must give each node a locally administered MAC",
                "Missing or not of that form: homeserver-cp-1, homeserver-worker-1.",
            ],
            id="mac-upper-case-or-universal",
        ),
        pytest.param(
            None,
            without(CLUSTER_VARS, "flux_sync_url"),
            ["flux_sync_url must name the Git repository this cluster syncs"],
            id="flux-url-placeholder",
        ),
        pytest.param(
            as_local_yml(without(example(), "flux_sync_url", "kubernetes_lb_frigate_ip")),
            without(CLUSTER_VARS, "flux_sync_url"),
            ["flux_sync_url must name the Git repository", "set kubernetes_lb_frigate_ip in group_vars/all/local.yml"],
            id="flux-url-and-an-lb-address-missing",
        ),
        pytest.param(PROXMOX_ONLY_CLUSTER_LOCAL_YML, CLUSTER_VARS, [], id="proxmox-only-sized"),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML.replace("kubernetes_node_sizes:", "unused_sizes:"),
            CLUSTER_VARS,
            [
                "kubernetes_node_sizes must give homeserver-worker-1, which runs every app, cores, memory_mib, "
                "system_disk_gib, data_disk_gib, media_disk_gib"
            ],
            id="proxmox-only-unsized",
        ),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML.replace("media_disk_gib: 100", "media_disk_gib: 0"),
            CLUSTER_VARS,
            [
                "set media_disk_gib for homeserver-worker-1 in kubernetes_node_sizes above 0, or set "
                "kubernetes_media_on_data_volume"
            ],
            id="proxmox-only-no-media-disk",
        ),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML.replace("media_disk_gib: 100", "media_disk_gib: 0")
            + "kubernetes_media_on_data_volume: true\n",
            CLUSTER_VARS,
            [],
            id="proxmox-only-media-on-the-data-volume",
        ),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML.replace(
                "homeserver-worker-1: 192.0.2.31}", "homeserver-worker-1: 192.0.2.31, homeserver-worker-2: 192.0.2.32}"
            ).replace("cores: 4", "cores: four"),
            CLUSTER_VARS,
            [
                "It names homeserver-worker-2, which the cluster does not have.",
                "kubernetes_node_sizes: homeserver-worker-1: cores must be a whole number.",
            ],
            id="proxmox-only-a-third-node-and-a-bad-size",
        ),
        pytest.param(
            PROXMOX_ONLY_LOCAL_YML + "kubernetes_node_adresses: {homeserver-cp-1: 192.0.2.30}\n"
            "flux_sync_url: https://example.invalid/homeserver.git\n",
            {},
            ["group_vars/all/local.yml sets flux_sync_url but no kubernetes_node_addresses"],
            id="misspelt-addresses",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_api_sans": "k8s-api.home.arpa"},
            ["kubernetes_api_sans must be a list of names, not a string."],
            id="api-sans-a-string",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_node_sizes": ["homeserver-worker-1"]},
            ["kubernetes_node_sizes must map node names to their sizes."],
            id="sizes-not-a-mapping",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_node_sizes": {"homeserver-worker-1": {"disk_gib": 10, "cores": -1}}},
            [
                "homeserver-worker-1: disk_gib is not a size (cores, memory_mib",
                "homeserver-worker-1: cores must be a whole number",
            ],
            id="unknown-and-negative-sizes",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_node_sizes": {"homeserver-worker-1": {"cores": 0, "memory_mib": 0}}},
            ["homeserver-worker-1: cores must be at least 1; homeserver-worker-1: memory_mib must be at least 1"],
            id="no-cores-or-memory",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_node_sizes": {"homeserver-worker-1": {"data_disk_gib": 5}}},
            ["homeserver-worker-1: data_disk_gib must be 0 or at least 11: its Talos volume takes 10 GiB"],
            id="data-disk-under-the-volume-minimum",
        ),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML.replace("data_disk_gib: 20", "data_disk_gib: 0"),
            CLUSTER_VARS,
            ["homeserver-worker-1 runs the apps, and their data lives on its data disk"],
            id="proxmox-only-no-data-disk",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_node_sizes": {"homeserver-worker-1": {"data_disk_gib": 0}}},
            [
                "homeserver-worker-1 runs the apps, and their data lives on its data disk: its data_disk_gib in "
                "kubernetes_node_sizes must be at least 11, not 0."
            ],
            id="nas-worker-without-a-data-disk",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_node_sizes": {"homeserver-cp-1": {"media_disk_gib": 50}}},
            ["homeserver-cp-1 runs the control plane alone and takes no data or media disk"],
            id="control-plane-with-a-media-disk",
        ),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML,
            CLUSTER_VARS | {"kubernetes_node_sizes": {"homeserver-cp-1": {"memory_mib": 1024, "system_disk_gib": 8}}},
            [
                "homeserver-cp-1 needs at least 2048 MiB of memory",
                "homeserver-cp-1's system_disk_gib in kubernetes_node_sizes must be at least 10",
                "kubernetes_node_sizes must give homeserver-worker-1",
            ],
            id="control-plane-too-small",
        ),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML,
            CLUSTER_VARS
            | {
                "kubernetes_node_sizes": {
                    "homeserver-cp-1": {"cores": 2, "memory_mib": 2048, "system_disk_gib": 10},
                    "homeserver-worker-1": {
                        "cores": 2,
                        "memory_mib": 6144,
                        "system_disk_gib": 32,
                        "data_disk_gib": 11,
                        "media_disk_gib": 11,
                    },
                }
            },
            [],
            id="proxmox-only-smallest-sizes",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_node_sizes": {"homeserver-worker-1": {"data_disk_gib": 769}}},
            ["(vm_storage_persistent_quota_gib in group_vars/all/storage.yml) also holds its snapshots"],
            id="nas-worker-data-disk-above-the-quota",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_node_sizes": {"homeserver-worker-1": {"data_disk_gib": 768}}},
            [],
            id="nas-worker-data-disk-within-the-quota",
        ),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML.replace(", media_disk_gib: 100", "")
            + "kubernetes_media_on_data_volume: true\n",
            CLUSTER_VARS,
            [],
            id="proxmox-only-media-on-the-data-volume-no-media-size",
        ),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML + "kubernetes_media_on_data_volume: true\n",
            CLUSTER_VARS,
            [
                "kubernetes_node_sizes gives homeserver-worker-1 a media disk, and kubernetes_media_on_data_volume "
                "keeps the media on the data volume instead: choose one"
            ],
            id="proxmox-only-media-disk-and-media-on-the-data-volume",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_node_sizes": {"homeserver-worker-1": {"media_disk_gib": 100}}},
            ["With a NAS the media live on the NAS, not on a disk of homeserver-worker-1's"],
            id="nas-worker-with-a-media-disk",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_media_on_data_volume": True},
            ["kubernetes_media_on_data_volume is for a setup without a NAS"],
            id="nas-media-on-the-data-volume",
        ),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML.replace("homeserver-worker-1: 192.0.2.31}", "homeserver-worker-1: }"),
            CLUSTER_VARS,
            ["It gives none to homeserver-worker-1."],
            id="worker-line-left-blank",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_node_addresses": ["192.0.2.30", "192.0.2.31"]},
            ["kubernetes_node_addresses must map each node to its address"],
            id="addresses-a-list",
        ),
        pytest.param(
            None,
            CLUSTER_VARS
            | {"kubernetes_node_addresses": {"homeserver-cp-1": "192.0.2.30", "homeserver-worker-1": "192.0.2.30"}},
            ["kubernetes_node_addresses gives two nodes the same address."],
            id="one-address-twice",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_node_macs": {"homeserver-cp-1": mac(1), "homeserver-worker-1": mac(1)}},
            ["kubernetes_node_macs gives two nodes the same MAC"],
            id="one-mac-twice",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"flux_sync_ref": "small-host"},
            ["flux_sync_ref must be a full reference, such as refs/heads/small-host"],
            id="flux-ref-a-branch-name",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"flux_sync_ref": "refs/heads/small-host"},
            ["flux_sync_ref names refs/heads/small-host, not refs/heads/main", "flux_sync_ref_unprotected: true"],
            id="flux-ref-unprotected",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"flux_sync_ref": "refs/heads/small-host", "flux_sync_ref_unprotected": True},
            [],
            id="flux-ref-unprotected-said-so",
        ),
        pytest.param(None, CLUSTER_VARS | {"kubernetes_ingress_allowed_range": "192.0.2.0/24"}, [], id="ingress-range"),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_ingress_allowed_range": "192.0.2.0"},
            ["kubernetes_ingress_allowed_range must be one IPv4 range in CIDR form"],
            id="ingress-range-an-address",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_ingress_allowed_range": "192.0.256.0/24"},
            ["kubernetes_ingress_allowed_range must be one IPv4 range in CIDR form"],
            id="ingress-range-out-of-bounds",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"vault_talos_secrets": "certs: {}\n"},
            ["The vault holds vault_talos_secrets", "secrets.yaml in the cluster's credentials directory"],
            id="talos-bundle-in-the-vault",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"flux_sync_url": "git@github.com:someone/homeserver.git"},
            ["flux_sync_url must name the Git repository this cluster syncs, over HTTPS"],
            id="flux-url-over-ssh",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"truenas_static_address": ""},
            ["gives only truenas_bootstrap_address, a DHCP lease the NAS play replaces"],
            id="nas-bootstrap-address-only",
        ),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML,
            CLUSTER_VARS | {"proxmox_address": "pve.home.arpa"},
            ["proxmox_address in group_vars/all/local.yml is pve.home.arpa: give the host's IPv4 address there"],
            id="proxmox-only-host-name",
        ),
        pytest.param(
            PROXMOX_ONLY_CLUSTER_LOCAL_YML,
            CLUSTER_VARS | {"proxmox_address": "192.0.2.256"},
            ["proxmox_address in group_vars/all/local.yml is 192.0.2.256"],
            id="proxmox-only-address-out-of-bounds",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"proxmox_address": "pve.home.arpa"},
            ["proxmox_address in group_vars/all/local.yml is pve.home.arpa"],
            id="nas-proxmox-host-name",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_node_sizes": {"homeserver-worker-1": {"system_disk_gib": 20}}},
            ["homeserver-worker-1's system_disk_gib in kubernetes_node_sizes must be at least 32"],
            id="worker-system-disk-too-small",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_lb_traefik_ip": "192.0.2.50"},
            ["kubernetes_lb_traefik_ip 192.0.2.50 is outside kubernetes_lb_range 192.0.2.40-192.0.2.49"],
            id="lb-address-outside-the-range",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_lb_frigate_ip": "192.0.2.40"},
            ["kubernetes_lb_traefik_ip and kubernetes_lb_frigate_ip must differ"],
            id="lb-address-twice",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_lb_range": "192.0.2.24/28"},
            ["kubernetes_lb_range 192.0.2.24/28 includes node addresses 192.0.2.30"],
            id="lb-range-over-a-node-cidr",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_lb_range": "192.0.2.40/29"},
            [],
            id="lb-range-a-cidr",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_lb_range": "pool-a"},
            ["kubernetes_lb_range must be IPv4 addresses as first-last"],
            id="lb-range-not-addresses",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_lb_range": "192.0.2.40-192.0.2.400"},
            ["kubernetes_lb_range must be IPv4 addresses as first-last"],
            id="lb-range-address-out-of-bounds",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_lb_range": "192.0.2.40/33"},
            ["kubernetes_lb_range must be IPv4 addresses as first-last"],
            id="lb-range-prefix-out-of-bounds",
        ),
        pytest.param(
            None,
            CLUSTER_VARS | {"kubernetes_lb_traefik_ip": "192.0.2.400"},
            ["kubernetes_lb_traefik_ip must be an IPv4 address, such as 192.0.2.40, not 192.0.2.400."],
            id="lb-address-out-of-bounds",
        ),
    ],
)
def test_kubernetes_settings_are_checked(tmp_path, local_yml, extra, expected):
    errors = resolve_on(
        example_tree(tmp_path, local_yml), "homeserver", "kubernetes_settings_error", "-e", json.dumps(extra)
    )
    if not expected:
        assert errors == []
    for text in expected:
        assert any(text in error for error in errors), f"{text!r} not in {errors}"


@pytest.mark.parametrize("playbook", ["site.yml", "truenas.yml", "proxmox.yml"])
def test_example_left_unchanged_is_refused(tmp_path, playbook):
    """local.yml.example copied as it is: its <mac> and <you> placeholders stop every playbook before any role
    runs, naming the variables, and never reach a VM's configuration."""
    rc, out = argspec_pass(example_tree(tmp_path), playbook)
    assert rc != 0, out[-3000:]
    assert "kubernetes_node_macs must give each node" in out and "flux_sync_url must name" in out, out[-3000:]
    assert "TASK [truenas :" not in out and "TASK [proxmox_" not in out, out[-3000:]


@pytest.mark.parametrize("playbook", ["site.yml", "proxmox.yml"])
def test_kubernetes_present_cannot_be_set(tmp_path, playbook):
    """kubernetes_present is worked out from the node addresses; given with -e, "false" would count as true."""
    tree = example_tree(tmp_path, example_without_kubernetes())
    rc, out = argspec_pass(tree, playbook, "-e", "kubernetes_present=false")
    assert rc != 0, out[-3000:]
    assert "kubernetes_present is worked out from kubernetes_node_addresses" in out, out[-3000:]
    assert "TASK [truenas :" not in out and "TASK [proxmox_" not in out, out[-3000:]


def test_task_arguments_match_module_specs():
    specs = {path.stem: module_spec(path) for path in MODULES}
    accepted = {
        name: set(spec) | {alias for option in spec.values() for alias in option.get("aliases") or []}
        for name, spec in specs.items()
    }
    problems, called = [], set()
    for tasks_file in sorted(ROOT.glob("roles/*/tasks/*.yml")):
        where = tasks_file.relative_to(ROOT)
        for task in tasks_in(yaml.safe_load(tasks_file.read_text())):
            # module_defaults supplies arguments as well.
            calls = [(name, args, "module_defaults") for name, args in (task.get("module_defaults") or {}).items()]
            calls += [(name, args, task.get("name", "?")) for name, args in task.items()]
            for name, args, context in calls:
                module = name.removeprefix("ansible.legacy.")
                if module not in specs:
                    continue
                if context != "module_defaults":
                    called.add(module)
                if not isinstance(args, dict):
                    problems.append(f"{where}: {context}: {module} takes its options as a mapping")
                    continue
                for option in sorted(set(args) - accepted[module]):
                    problems.append(f"{where}: {context}: {module} has no option {option!r}")
    assert problems == []
    assert called == set(specs), f"modules no task calls: {sorted(set(specs) - called)}"


def test_secret_options_are_no_log():
    """An option that looks like a secret is no_log, so Ansible masks its value wherever it would print it."""
    specs = [(path.stem, module_spec(path)) for path in MODULES]
    for role in ROLES:
        argument_specs = yaml.safe_load((role / "meta" / "argument_specs.yml").read_text())["argument_specs"]
        specs += [(role.name, entry.get("options") or {}) for entry in argument_specs.values()]
    flagged, excepted = [], set()
    for owner, spec in specs:
        for dotted, name, option in options_in(spec):
            if not SECRET_NAME.search(name) or option.get("no_log") is True:
                continue
            if (owner, dotted) in NOT_SECRET:
                excepted.add((owner, dotted))
            else:
                flagged.append(f"{owner}: {dotted}")
    assert flagged == [], "options that look like secrets but are not no_log"
    assert excepted == set(NOT_SECRET), "NOT_SECRET names an option that no longer exists or is now no_log"


def test_home_assistant_url_is_a_setting_only_when_given(tmp_path):
    """Homepage's tile reads ${HAOS_URL:=}: the setting exists only when local.yml names Home Assistant OS's address,
    which is never tracked."""
    tree = example_tree(tmp_path)
    assert "HAOS_URL" not in resolve_on(tree, "homeserver-k8s", "flux_bootstrap_settings", *CLUSTER_ARGS)
    given = json.dumps({**CLUSTER_VARS, "haos_url": "http://homeassistant.home.arpa:8123"})
    settings = resolve_on(tree, "homeserver-k8s", "flux_bootstrap_settings", "-e", given)
    assert settings["HAOS_URL"] == "http://homeassistant.home.arpa:8123"


@pytest.mark.parametrize("local_yml", [None, PROXMOX_ONLY_CLUSTER_LOCAL_YML], ids=["nas", "proxmox-only"])
@pytest.mark.parametrize("mapping", ["", "igpu"], ids=["no-gpu", "gpu"])
def test_gpu_follows_local_yml(tmp_path, local_yml, mapping):
    """Whether the host passes an iGPU through is per host: local.yml's kubernetes_gpu_mapping. The worker alone gets
    the PCI device, the GPU schematic and its label, in both setups, and the apps' settings ask for the device or for
    none; the manifests stay the same either way."""
    tree = example_tree(tmp_path, local_yml)
    given = json.dumps({**CLUSTER_VARS, "kubernetes_gpu_mapping": mapping})
    vms = resolve_on(tree, "homeserver", "proxmox_vm_vms", "-e", given)
    worker = "homeserver-worker-1"
    assert {vm["name"]: vm["hostpci"] for vm in vms} == {
        vm["name"]: ([{"mapping": "igpu", "pcie": True}] if mapping and vm["name"] == worker else []) for vm in vms
    }
    nodes = resolve_on(tree, "homeserver-k8s", "talos_nodes", "-e", given)
    assert {n["name"]: n["schematic"] for n in nodes} == {
        n["name"]: ("gpu" if mapping and n["name"] == worker else "base") for n in nodes
    }
    labelled = [n["name"] for n in nodes if any("homeserver.internal/gpu: intel" in p for p in n["patches"])]
    assert labelled == ([worker] if mapping else [])
    settings = resolve_on(tree, "homeserver-k8s", "flux_bootstrap_settings", "-e", given)
    assert (settings["GPU_I915"], settings["FRIGATE_DETECTOR_DEVICE"]) == (("1", "GPU") if mapping else ("0", "CPU"))


HAOS_ONLY_ARGS = ("-e", json.dumps({"haos_mac": mac(9)}))
# make's ALL=1.
ALL_ARGS = ("-e", json.dumps({"cluster_vms": True}))


@pytest.mark.parametrize("playbook", ["suspend.yml", "resume.yml"])
@pytest.mark.parametrize(
    ("local_yml", "extra", "only", "validations", "says"),
    [
        pytest.param(None, CLUSTER_ARGS, "", 2, None, id="cluster-and-haos"),
        # Apps' names leave Home Assistant alone, and its name alone leaves the cluster unasked.
        pytest.param(None, CLUSTER_ARGS, "jellyfin, media", 1, "Leave Home Assistant alone", id="apps-only"),
        pytest.param(None, CLUSTER_ARGS, "homeassistant", 1, None, id="haos-only"),
        pytest.param(example_without_kubernetes(), HAOS_ONLY_ARGS, "", 1, "so this setup has no cluster", id="haos"),
        pytest.param(example_without_kubernetes(), (), "", 0, "so this setup has no Home Assistant VM", id="neither"),
        # ALL=1 adds the cluster's VMs, through the power entry point once more.
        pytest.param(None, (*CLUSTER_ARGS, *ALL_ARGS), "", 3, None, id="all"),
        pytest.param(
            example_without_kubernetes(), (*HAOS_ONLY_ARGS, *ALL_ARGS), "", 1, "no cluster", id="all-no-cluster"
        ),
    ],
)
def test_down_and_up_pass_the_argument_specs(tmp_path, playbook, local_yml, extra, only, validations, says):
    """make suspend and make resume pass roles/app_scale's and the proxmox_vm role's power entry point's checks with the
    tracked configuration and make's ONLY, and each play ends by itself in a setup without its part."""
    tree = example_tree(tmp_path, local_yml)
    rc, out = argspec_pass(tree, playbook, *extra, "-e", json.dumps({"only": only}))
    assert rc == 0, out[-3000:]
    assert out.count("Validating arguments against arg spec") == validations, out[-3000:]
    assert says is None or says in out, out[-3000:]


@pytest.mark.parametrize(
    ("extra", "only", "refused"),
    [
        pytest.param(HAOS_ONLY_ARGS, "jellyfin", "make suspend knows no jellyfin: only homeassistant", id="haos"),
        pytest.param((), "homeassistant", "knows no homeassistant, nor any other name", id="neither"),
    ],
)
def test_down_without_a_cluster_refuses_an_app(tmp_path, extra, only, refused):
    """Without a cluster no play checks app names, so Home Assistant's refuses every name but its own."""
    tree = example_tree(tmp_path, example_without_kubernetes())
    rc, out = argspec_pass(tree, "suspend.yml", *extra, "-e", json.dumps({"only": only}))
    assert rc != 0 and refused in out, out[-3000:]


@pytest.mark.parametrize("playbook", ["suspend.yml", "resume.yml"])
def test_all_takes_no_only(tmp_path, playbook):
    """ALL=1 stops or starts everything, so it refuses ONLY before any play changes anything."""
    tree = example_tree(tmp_path)
    rc, out = argspec_pass(tree, playbook, *CLUSTER_ARGS, *ALL_ARGS, "-e", json.dumps({"only": "jellyfin"}))
    assert rc != 0 and "so it takes no ONLY" in out, out[-3000:]
    assert out.count("Validating arguments against arg spec") == 0, out[-3000:]


@pytest.mark.parametrize(
    ("local_yml", "order"),
    [
        pytest.param(None, [301, 300], id="nas"),
        pytest.param(PROXMOX_ONLY_CLUSTER_LOCAL_YML, [301, 300], id="proxmox-only"),
    ],
)
def test_the_cluster_goes_down_worker_first(tmp_path, local_yml, order):
    """make suspend ALL=1 shuts the worker's VM down before the control plane's, whose API answers until the end, in
    both setups; make resume ALL=1 starts them the other way round."""
    tree = example_tree(tmp_path, local_yml)
    assert resolve_on(tree, "homeserver", "kubernetes_power_order", *CLUSTER_ARGS) == order


def test_down_and_up_read_only_alike(tmp_path):
    """Every play of both playbooks, and the test playbooks, read ONLY the same way: names, blanks and spaces
    dropped, and without it no names (everything)."""
    tree = example_tree(tmp_path)
    expressions = set()
    for playbook in ("suspend.yml", "resume.yml"):
        plays = yaml.safe_load((tree / playbook).read_text())
        apps = next(play for play in plays if play.get("roles"))
        haos = next(play for play in plays if "Home Assistant" in play["name"])
        expressions |= {play["vars"]["only_names"] for play in plays if "only_names" in play.get("vars", {})}
        role = apps["roles"][0]
        assert role["vars"]["app_scale_state"] == {"suspend.yml": "down", "resume.yml": "up"}[playbook]
        assert role["vars"]["app_scale_only"] == "{{ only_names }}"
        expressions |= {apps["vars"]["only_names"], haos["vars"]["only_names"]}
        power = haos["tasks"][0]["vars"]
        assert power["proxmox_vm_power"] == {"suspend.yml": "stopped", "resume.yml": "running"}[playbook]
        tests = yaml.safe_load((ROOT / "tests" / "integration" / "haos_power.yml").read_text())[0]["tasks"][0]
        assert tests["vars"]["proxmox_vm_power_vmids"] == power["proxmox_vm_power_vmids"]
    (expression,) = expressions
    tests = yaml.safe_load((ROOT / "tests" / "integration" / "apps.yml").read_text())[0]["roles"][0]["vars"]
    assert tests["app_scale_only"] == expression, "tests/integration/apps.yml reads ONLY as the playbooks do"
    inner = expression.strip().removeprefix("{{").removesuffix("}}")
    assert resolve_on(tree, "homeserver-k8s", inner, "-e", json.dumps({"only": " jellyfin,, media "})) == [
        "jellyfin",
        "media",
    ]
    assert resolve_on(tree, "homeserver-k8s", inner) == []


def test_flux_is_new_enough_to_substitute_strictly(tmp_path):
    """Strict substitution, which fails a manifest naming a setting that does not exist rather than giving it an empty
    string, is kustomize-controller's default from Flux 2.9; roles/flux_bootstrap sets no feature gate for it."""
    tree = example_tree(tmp_path)
    version = resolve_on(tree, "homeserver-k8s", "flux_bootstrap_instance.version", *CLUSTER_ARGS)
    major, minor = (int(part) for part in str(version).split(".")[:2])
    assert (major, minor) >= (2, 9), version


@pytest.mark.parametrize(("where", "refused"), [("homeserver/.talos", True), ("homeserver-talos", False)])
def test_talos_config_dir_must_be_outside_the_repository(tmp_path, where, refused):
    """talosconfig and kubeconfig never go inside the repository; a sibling directory whose name merely starts with
    the repository's is outside it."""
    tree = tmp_path / "homeserver"
    tree.mkdir()
    example_tree(tree)
    rc, out = argspec_pass(
        tree, "kubernetes.yml", *CLUSTER_ARGS, "-e", f"kubernetes_talos_config_dir={tmp_path / where}"
    )
    assert ("talos_config_dir must be outside the repository" in out) == refused, out[-3000:]
    assert (rc != 0) == refused, out[-3000:]


@pytest.mark.parametrize(("given", "expected"), [(None, ".talos/homeserver"), ("~/.talos/test", ".talos/test")])
def test_talos_config_dir_follows_local_yml(tmp_path, given, expected):
    """Where the cluster's credentials live comes from local.yml's kubernetes_talos_config_dir, with ~ expanded, and
    is ~/.talos/homeserver otherwise. A talos_config_dir written in local.yml would lose to group_vars/kubernetes,
    which outranks group_vars/all, hence a setting of its own: a test host must never share the home cluster's."""
    local_yml = (ROOT / "group_vars" / "all" / "local.yml.example").read_text()
    if given:
        local_yml += f"kubernetes_talos_config_dir: {given}\n"
    tree = example_tree(tmp_path, local_yml)
    assert resolve_on(tree, "homeserver-k8s", "talos_config_dir", *CLUSTER_ARGS) == str(Path.home() / expected)
    assert resolve_on(tree, "homeserver-k8s", "flux_bootstrap_kubeconfig", *CLUSTER_ARGS) == str(
        Path.home() / expected / "kubeconfig"
    )


@pytest.mark.parametrize(
    ("local_yml", "guarded"),
    [
        pytest.param(EXAMPLE_LOCAL_YML + TAILSCALE_LOCAL_YML, ["192.0.2.10", "192.0.2.50"], id="nas"),
        pytest.param(
            re.sub(r"^truenas_static_address:.*\n", "", EXAMPLE_LOCAL_YML, flags=re.MULTILINE) + TAILSCALE_LOCAL_YML,
            ["192.0.2.50"],
            id="nas-bootstrap-only",
        ),
        pytest.param(PROXMOX_ONLY_LOCAL_YML + TAILSCALE_LOCAL_YML, [], id="proxmox-only"),
    ],
)
def test_tailscale_guard_follows_the_setup(tmp_path, local_yml, guarded):
    """The guard protects the hosts that trust the Proxmox host's address: with a NAS, every address local.yml gives
    it, since NFS answers on each, including a NAS known only by its bootstrap address, whose shares are exported though
    no storage mounts them; without one, nothing. The routes come from local.yml."""
    tree = example_tree(tmp_path, local_yml)
    assert resolve_on(tree, "homeserver", "proxmox_tailscale_guard_hosts") == guarded
    assert resolve_on(tree, "homeserver", "proxmox_tailscale_advertise_routes") == ["192.0.2.0/24"]


@pytest.mark.parametrize(
    ("local_yml", "enabled", "routes"),
    [
        pytest.param(PROXMOX_ONLY_LOCAL_YML, False, [], id="neither"),
        pytest.param(PROXMOX_ONLY_LOCAL_YML + "tailscale_enabled: true\n", True, [], id="flag-alone"),
        pytest.param(PROXMOX_ONLY_LOCAL_YML + TAILSCALE_LOCAL_YML, True, ["192.0.2.0/24"], id="routes-alone"),
    ],
)
def test_tailscale_turns_on_with_its_flag_or_routes(tmp_path, local_yml, enabled, routes):
    """tailscale_enabled in local.yml turns Tailscale on, the host then joining the tailnet alone, and routes without
    it turn it on too, the host then also the subnet router; with neither, it stays off."""
    tree = example_tree(tmp_path, local_yml)
    assert resolve_on(tree, "homeserver", "proxmox_tailscale_enabled") is enabled
    assert resolve_on(tree, "homeserver", "proxmox_tailscale_advertise_routes") == routes


@pytest.mark.parametrize(
    ("given", "refusal"),
    [
        pytest.param(
            {"tailscale_enabled": "false"},
            'proxmox_tailscale_enabled must be true or false, not "false"',
            id="enabled-string",
        ),
        pytest.param(
            {"tailscale_enabled": False},
            "proxmox_tailscale_advertise_routes lists routes, but proxmox_tailscale_enabled is false",
            id="routes-while-off",
        ),
        pytest.param(
            {"tailscale_enabled": True, "tailscale_advertise_routes": [], "proxmox_tailscale_tags": []},
            "proxmox_tailscale_tags must name at least one tag",
            id="no-tag-without-routes",
        ),
        pytest.param(
            {"tailscale_advertise_routes": "192.0.2.0/24"},
            "proxmox_tailscale_advertise_routes must be a list, not a string.",
            id="routes-string",
        ),
        pytest.param(
            {"tailscale_advertise_routes": ["2001:db8::/32"]},
            "IPv6 routes are not advertised",
            id="routes-ipv6",
        ),
        pytest.param(
            {"tailscale_advertise_routes": ["192.0.2.0"]},
            "proxmox_tailscale_advertise_routes takes IPv4 networks in CIDR form",
            id="routes-not-cidr",
        ),
        pytest.param(
            {"proxmox_tailscale_tags": []},
            "proxmox_tailscale_tags must name at least one tag",
            id="no-tag",
        ),
        pytest.param(
            {"proxmox_tailscale_tags": ["homeserver"]},
            "Each of proxmox_tailscale_tags is tag:<name>",
            id="tag-without-prefix",
        ),
        pytest.param(
            {"proxmox_tailscale_settings": {"advertise-tags": "tag:other"}},
            "proxmox_tailscale_settings must not hold advertise-tags",
            id="settings-tags",
        ),
        pytest.param(
            {"proxmox_tailscale_settings": {"accept-dns": ["no"]}},
            "proxmox_tailscale_settings takes tailscale set flag names, each with a boolean or a string",
            id="settings-value",
        ),
        pytest.param(
            {"proxmox_tailscale_guard_hosts": ["nas.home.arpa"]},
            "proxmox_tailscale_guard_hosts takes IPv4 addresses",
            id="guard-host-name",
        ),
        pytest.param(
            {"proxmox_tailscale_guard_allow": ["https", "tcp/70000"]},
            "Each of proxmox_tailscale_guard_allow is tcp/<port> or udp/<port>",
            id="guard-port",
        ),
    ],
)
def test_tailscale_settings_are_checked(tmp_path, given, refusal):
    """Each setting the Tailscale role cannot use stops proxmox.yml with its own sentence, before the role does
    anything."""
    tree = example_tree(tmp_path, EXAMPLE_LOCAL_YML + TAILSCALE_LOCAL_YML)
    rc, out = argspec_pass(tree, "proxmox.yml", "-e", json.dumps(given))
    assert rc != 0, out[-3000:]
    assert refusal in out, out[-3000:]


# ------------------------------------------------------------------------------------------ Home Assistant ---


# Each problem with local.yml's Home Assistant section, and what the message says.
@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        pytest.param({}, [], id="example-filled-in"),
        pytest.param({"haos_mac": "<mac>"}, ["haos_mac must be a locally administered MAC"], id="placeholder"),
        pytest.param(
            {"haos_mac": "00:" + mac(9)[3:]}, ["haos_mac must be a locally administered MAC"], id="universal-mac"
        ),
        pytest.param({"haos_mac": mac(1)}, ["haos_mac is a cluster node's MAC too"], id="a-node-mac"),
        pytest.param({"haos_vmid": 301}, ["haos_vmid 301 is a cluster node's VMID"], id="a-node-vmid"),
        pytest.param({"haos_vmid": 50}, ["haos_vmid must be a number from 100"], id="vmid-too-low"),
        pytest.param({"haos_size": {"disk_gib": 16}}, ["disk_gib must be at least 32"], id="disk-below-the-image"),
        pytest.param({"haos_size": {"gpus": 1}}, ["haos_size: gpus is not a size"], id="unknown-size"),
        pytest.param({"haos_size": {"cores": "two"}}, ["cores must be a whole number"], id="size-not-a-number"),
        pytest.param({"haos_size": [2]}, ["haos_size must map cores"], id="size-a-list"),
        pytest.param({"haos_usb": "10c4:ea60"}, ["haos_usb must be a list of devices"], id="usb-a-string"),
        pytest.param({"haos_usb": [{"port": 1}]}, ["haos_usb must be a list of devices"], id="usb-unknown-key"),
        pytest.param({"haos_usb": [{"host": "10c4:ea60"}, {"mapping": "zwave"}]}, [], id="usb-by-id-and-mapping"),
        pytest.param({"haos_owner_username": "Owner"}, ["haos_owner_username must be lower case"], id="username"),
        pytest.param(
            {"vm_backup_extra_vmids": [320, 310]},
            ["vm_backup_extra_vmids names 310, Home Assistant's VM"],
            id="extra-backup-names-haos",
        ),
        pytest.param({"vm_backup_extra_vmids": [320]}, [], id="extra-backup-other-vm"),
    ],
)
def test_haos_settings_are_checked(tmp_path, extra, expected):
    errors = resolve_on(
        example_tree(tmp_path), "homeserver", "haos_settings_error", "-e", json.dumps(CLUSTER_VARS | extra)
    )
    if not expected:
        assert errors == []
    for text in expected:
        assert any(text in error for error in errors), f"{text!r} not in {errors}"


def test_haos_settings_without_a_mac_are_refused(tmp_path):
    """A section that sets the VM's size but no MAC: what a misspelt haos_mac looks like."""
    local_yml = as_local_yml(without(example(), "haos_mac") | {"haos_size": {"cores": 4}})
    errors = resolve_on(
        example_tree(tmp_path, local_yml),
        "homeserver",
        "haos_settings_error",
        "-e",
        json.dumps(without(CLUSTER_VARS, "haos_mac")),
    )
    assert any("sets haos_size but no haos_mac" in error for error in errors), errors


@pytest.mark.parametrize("playbook", ["homeassistant.yml", "proxmox.yml", "site.yml"])
def test_haos_present_cannot_be_set(tmp_path, playbook):
    rc, out = argspec_pass(example_tree(tmp_path), playbook, *CLUSTER_ARGS, "-e", "haos_present=false")
    assert rc != 0 and "haos_present is worked out from haos_mac" in out, out[-3000:]


@pytest.mark.parametrize(
    ("local_yml", "storage", "fmt"),
    [
        pytest.param(None, "homeserver-persistent", "qcow2", id="nas"),
        pytest.param(PROXMOX_ONLY_LOCAL_YML + f'haos_mac: "{mac(9)}"\n', "local-lvm", None, id="proxmox-only"),
    ],
)
def test_haos_vm_resolves_on_the_proxmox_host(tmp_path, local_yml, storage, fmt):
    """The VM, the OVA it is imported from and the backup that covers it, through the real inventory layout: its disk
    on the NAS's persistent storage with a NAS (qcow2), on local-lvm without one; the OVA always checked."""
    vms, imports, backed_up = resolve_on(
        example_tree(tmp_path, local_yml), "homeserver", "[haos_vms, haos_imports, vm_backup_vmids]", *CLUSTER_ARGS
    )
    (vm,) = vms
    (disk,) = vm["disks"]
    assert (vm["vmid"], vm["name"], vm["bios"], vm["boot"]) == (310, "homeassistant", "ovmf", "order=scsi0")
    assert (vm["net"]["mac"], vm["usb"], vm["watchdog"]["model"]) == (mac(9), [], "i6300esb")
    assert (disk["storage"], disk.get("format"), disk["size_gib"]) == (storage, fmt, 32)
    assert disk["import_from"] == "local:import/haos_ova-18.3.ova/home-assistant.vmdk"
    (image,) = imports
    assert image["filename"] == "haos_ova-18.3.ova" and re.fullmatch(r"[0-9a-f]{64}", image["sha256"])
    assert image["url"] == "https://github.com/home-assistant/operating-system/releases/download/18.3/haos_ova-18.3.ova"
    assert 310 in backed_up


def test_haos_placeholder_stops_the_plays_that_read_it(tmp_path):
    """local.yml.example's <mac> for Home Assistant stops proxmox.yml and homeassistant.yml before any role runs."""
    for playbook in ("proxmox.yml", "homeassistant.yml"):
        rc, out = argspec_pass(
            example_tree(tmp_path / playbook), playbook, "-e", json.dumps(without(CLUSTER_VARS, "haos_mac"))
        )
        assert rc != 0 and "haos_mac must be a locally administered MAC" in out, out[-3000:]
        assert "TASK [proxmox_" not in out and "TASK [haos_onboard" not in out, out[-3000:]


def test_haos_test_playbook_runs_the_real_plays_roles():
    """tests/integration/homeassistant.yml runs homeassistant.yml's roles with homeassistant.yml's own vars, and its
    pre-tasks are homeassistant.yml's, so the idempotence tests run what make deploy TYPE=homeassistant does."""
    (real,) = yaml.safe_load((ROOT / "homeassistant.yml").read_text())
    (test,) = yaml.safe_load((ROOT / "tests" / "integration" / "homeassistant.yml").read_text())
    assert test["vars"] == real["vars"] and test["roles"] == real["roles"]
    by_name = {task["name"]: task for task in real["pre_tasks"]}
    assert all(by_name.get(task["name"]) == task for task in test["pre_tasks"]), "a pre-task differs"


# make status, in each setup the two switches make, with and without Home Assistant.
STATUS_SETUPS = [
    pytest.param(None, CLUSTER_ARGS, id="nas-cluster"),
    pytest.param(example_without_kubernetes(), HAOS_ONLY_ARGS, id="nas"),
    pytest.param(PROXMOX_ONLY_CLUSTER_LOCAL_YML, CLUSTER_ARGS, id="proxmox-only-cluster"),
    pytest.param(PROXMOX_ONLY_LOCAL_YML, (), id="proxmox-only"),
]


@pytest.mark.parametrize(("local_yml", "extra"), STATUS_SETUPS)
def test_status_passes_the_argument_specs(tmp_path, local_yml, extra):
    """make status passes roles/status's checks with the tracked configuration and make's TYPE, in each setup: the
    main entry point's on the Proxmox host and on the cluster's host (which reads nothing without a cluster), the
    report's on the controller."""
    types = ("-e", json.dumps({"types": " vms,, storage,apps "}))
    rc, out = argspec_pass(example_tree(tmp_path, local_yml), "status.yml", *extra, *types)
    assert rc == 0, out[-3000:]
    validated = {
        host
        for section in out.split("Validating arguments against arg spec 'main'")[1:]
        for host in re.findall(r"^ok: \[([^\]]+)\]", section.split("TASK [", 1)[0], re.M)
    }
    assert validated == {"homeserver", "homeserver-k8s"}, out[-3000:]
    assert out.count("Validating arguments against arg spec 'report'") == 1, out[-3000:]


def test_status_refuses_a_section_it_does_not_have(tmp_path):
    rc, out = argspec_pass(example_tree(tmp_path), "status.yml", *CLUSTER_ARGS, "-e", json.dumps({"types": "vms,nas"}))
    assert rc != 0, out[-3000:]
    message = "make status has no section nas: TYPE takes apps, admin, vms, storage, cluster, comma-separated."
    assert message in out, out[-3000:]


def test_status_reads_the_cluster_as_it_is_built(tmp_path):
    """make status reads the cluster with roles/talos's credentials and endpoint, finds the apps as make suspend does,
    shows every Talos user volume the nodes declare by its mount, and reads Alertmanager's pod by the name
    kube-prometheus-stack gives it."""
    tree = example_tree(tmp_path, PROXMOX_ONLY_CLUSTER_LOCAL_YML)
    names = [
        "status_source",
        "status_cluster_kubeconfig",
        "status_cluster_server",
        "status_cluster_nodes",
        "status_cluster_apps_path",
        "status_cluster_annotation",
        "status_cluster_database_label",
        "status_cluster_volumes",
        "status_cluster_settings",
        "status_cluster_alertmanager",
        "status_cluster_pins",
        "talos_config_dir",
        "talos_endpoint",
        "app_scale_path",
        "app_scale_annotation",
        "app_scale_late_label",
        "talos_nodes",
        "kubernetes_nodes",
        "flux_bootstrap_settings",
        "talos_version",
        "talos_kubernetes_version",
    ]
    found = resolve_on(tree, "homeserver-k8s", "{" + ", ".join(f"'{n}': {n}" for n in names) + "}", *CLUSTER_ARGS)
    assert found["status_source"] == "cluster"
    assert found["status_cluster_kubeconfig"] == found["talos_config_dir"] + "/kubeconfig"
    assert found["status_cluster_server"] == found["talos_endpoint"]
    assert found["status_cluster_nodes"] == [{"name": n["name"], "role": n["role"]} for n in found["kubernetes_nodes"]]
    assert [found[f"status_cluster_{k}"] for k in ("apps_path", "annotation", "database_label")] == [
        found["app_scale_path"],
        found["app_scale_annotation"],
        found["app_scale_late_label"],
    ]
    declared = {
        re.search(r"^name: (\S+)$", patch, re.M).group(1)
        for n in found["talos_nodes"]
        for patch in n["patches"]
        if "kind: UserVolumeConfig" in patch
    }
    assert declared == {"local-path-provisioner", "media"}
    assert set(found["status_cluster_volumes"]) == {f"/var/mnt/{name}" for name in declared}
    assert found["status_cluster_settings"]["allowlist"] in found["flux_bootstrap_settings"]
    assert found["status_cluster_pins"] == {
        "talos": found["talos_version"],
        "kubernetes": found["talos_kubernetes_version"],
    }
    release = yaml.safe_load((ROOT / "k8s" / "infrastructure" / "monitoring" / "release.yaml").read_text())
    values = release["spec"].get("values") or {}
    assert not values.get("fullnameOverride") and not values.get("nameOverride")
    alertmanager = found["status_cluster_alertmanager"]
    assert alertmanager == {
        "namespace": release["metadata"]["namespace"],
        "pod": f"alertmanager-{release['metadata']['name']}-alertmanager-0",
    }
    without = resolve_on(
        example_tree(tmp_path / "local", PROXMOX_ONLY_LOCAL_YML),
        "homeserver-k8s",
        "[status_source, status_cluster_server]",
    )
    assert without == ["", ""]


@pytest.mark.parametrize(
    ("local_yml", "extra"),
    [
        pytest.param(None, CLUSTER_ARGS, id="cluster-and-home-assistant"),
        pytest.param(
            None, ("-e", json.dumps({**CLUSTER_VARS, "vm_backup_extra_vmids": [320, 301]})), id="with-extra-vms"
        ),
        pytest.param(PROXMOX_ONLY_LOCAL_YML, ("-e", json.dumps({"vm_backup_extra_vmids": [320]})), id="extra-alone"),
        pytest.param(PROXMOX_ONLY_LOCAL_YML, (), id="none"),
    ],
)
def test_status_reports_on_the_vms_the_backup_covers(tmp_path, local_yml, extra):
    """The VMs make status names are this repo's own, the same the nightly backup covers; any other VM on the host is
    only counted."""
    tree = example_tree(tmp_path, local_yml)
    reported = resolve_on(tree, "homeserver", "status_vms | map(attribute='vmid') | list", *extra)
    assert reported == resolve_on(tree, "homeserver", "vm_backup_vmids", *extra)


# What a task may name besides its module.
TASK_KEYWORDS = {
    "name",
    "when",
    "vars",
    "register",
    "tags",
    "block",
    "rescue",
    "always",
    "loop",
    "loop_control",
    "check_mode",
    "changed_when",
    "failed_when",
    "ignore_errors",
    "ignore_unreachable",
    "no_log",
    "until",
    "retries",
    "delay",
}


def test_status_reads_only_through_status_read():
    """No task of roles/status runs a command, or anything that could change a host, but through status_read, whose
    read list refuses everything else; it writes only its own report, on the controller."""
    allowed = {
        "status_read",
        # The report entry point's questions to the apps, from this machine: a GET each, no certificate checked.
        "status_probe",
        "ansible.builtin.assert",
        "ansible.builtin.set_fact",
        "ansible.builtin.include_tasks",
        "ansible.builtin.debug",
        # The report entry point's own directory and file.
        "ansible.builtin.file",
        "ansible.builtin.copy",
    }
    used = {}
    for tasks_file in sorted((ROOT / "roles" / "status" / "tasks").glob("*.yml")):
        for task in tasks_in(yaml.safe_load(tasks_file.read_text())):
            for module in set(task) - TASK_KEYWORDS:
                used.setdefault(module, []).append(f"{tasks_file.name}: {task.get('name')}")
    assert {module: where for module, where in used.items() if module not in allowed} == {}
    report = yaml.safe_load((ROOT / "roles" / "status" / "tasks" / "report.yml").read_text())
    writes = [t for t in tasks_in(report) if {"ansible.builtin.file", "ansible.builtin.copy"} & set(t)]
    assert all("status_report_file" in json.dumps(t) for t in writes), "the report entry point writes elsewhere"
