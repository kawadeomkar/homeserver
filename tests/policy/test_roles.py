"""The roles keep the conventions CLAUDE.md sets for them.

- Every role default has an entry in meta/argument_specs.yml with the same default, and every entry a default.
- Each custom module's DOCUMENTATION, with its doc fragments merged in, matches the argument spec its main()
  builds: names, types, defaults, choices, required, and the same for suboptions.
- Each custom module documents its check-mode, diff-mode and platform support as `attributes`, and the
  check-mode support it documents is what its AnsibleModule declares.
- The tracked configuration and examples pass the roles' argument specs and input checks: site.yml runs
  with only the always-tagged validation, against inventory.example and local.yml.example, and again
  against a local.yml that names only the Proxmox host, so the NAS play must end by itself. Without a NAS,
  the storage both VM classes use is the one local.yml names, which no tracked file overrides. A TrueNAS
  section that keeps other settings but gives no address stops both playbooks, and so does nas_present given
  on the command line. Deleting the truenas host from the inventory, which inventory.example advises
  against, still gives a working run without a NAS.
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
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import ansible.module_utils
import pytest
import yaml
from ansible.plugins.loader import fragment_loader, init_plugin_loader
from ansible.utils.collection_loader import AnsibleCollectionConfig
from ansible.utils.plugin_docs import get_docstring

ROOT = Path(__file__).resolve().parents[2]
ROLES = sorted(path.parents[1] for path in ROOT.glob("roles/*/meta/argument_specs.yml"))
MODULES = sorted(ROOT.glob("roles/*/library/*.py"))

# ansible-test's no-log-needed pattern: option names that look like secrets.
SECRET_NAME = re.compile(r"pass(?!ive)|secret|token|key", re.IGNORECASE)
# Options whose names match it but hold nothing secret, as (module or role, option): why.
NOT_SECRET = {
    ("truenas_resource", "key"): "the names of the fields that identify an entry",
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


# A local.yml with only the Proxmox host's address (an RFC 5737 placeholder): no NAS.
PROXMOX_ONLY_LOCAL_YML = "---\nproxmox_address: 192.0.2.20\n"


def blank_nas_local_yml():
    """local.yml.example with every TrueNAS value left blank after its colon, which the example allows,
    and the bootstrap address only a space."""
    example = yaml.safe_load((ROOT / "group_vars" / "all" / "local.yml.example").read_text())
    lines = ["---"]
    for name, value in example.items():
        if name == "truenas_bootstrap_address":
            lines.append(f'{name}: " "')
        elif name.startswith("truenas_"):
            lines.append(f"{name}:")
        else:
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


def argspec_pass(tree, playbook="site.yml", *extra):
    """Run `playbook` with only the tasks tagged `always`: the argument-spec validation Ansible adds to each
    role, and the roles' and playbooks' own input checks. truenas_info, also always, would connect to the NAS."""
    return ansible_cli(
        tree,
        "ansible-playbook",
        *["-i", "inventory.example", playbook, "--tags", "__argspec_only__", "--skip-tags", "truenas_info"],
        *["-e", "ansible_connection=local", "-e", "vault_truenas_api_key=stub", *extra],
    )


@pytest.mark.parametrize(
    ("local_yml", "validations", "present", "absent"),
    [
        pytest.param(
            None,
            2,
            ["validate | Check the connection settings", "skipping: [truenas]"],
            [],
            id="nas",
        ),
        pytest.param(
            PROXMOX_ONLY_LOCAL_YML,
            1,
            ["Skip the NAS when local.yml gives it no address", "ok: [truenas]"],
            ["TASK [truenas :", "validate | Check the connection settings"],
            id="proxmox-only",
        ),
        pytest.param(
            blank_nas_local_yml(),
            1,
            ["Skip the NAS when local.yml gives it no address", "ok: [truenas]"],
            ["TASK [truenas :", "validate | Check the connection settings"],
            id="proxmox-only-blank-values",
        ),
    ],
)
def test_examples_satisfy_argument_specs(tmp_path, local_yml, validations, present, absent):
    """The tracked configuration passes every role's checks: with local.yml.example for local.yml, and with a
    local.yml that names only the Proxmox host or leaves every TrueNAS value blank, where the NAS play must
    end before its role runs."""
    rc, out = argspec_pass(example_tree(tmp_path, local_yml))
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
    assert "TASK [truenas :" not in out and "TASK [proxmox_storage :" not in out, out[-3000:]


def test_inventory_without_the_nas_host(tmp_path):
    """inventory.example says to keep the truenas host without a NAS, but a user who deletes it still gets a
    working run: the NAS play matches no host, and the Proxmox play runs as it does without a NAS."""
    tree = example_tree(tmp_path, PROXMOX_ONLY_LOCAL_YML)
    inventory = tree / "inventory.example"
    inventory.write_text(re.sub(r"^truenas\n", "", inventory.read_text(), flags=re.MULTILINE))
    rc, out = argspec_pass(tree)
    assert rc == 0, out[-3000:]
    assert "skipping: no hosts matched" in out, out[-3000:]
    assert out.count("Validating arguments against arg spec 'main'") == 1, out[-3000:]


@pytest.mark.parametrize("playbook", ["site.yml", "proxmox.yml"])
def test_nas_present_cannot_be_set(tmp_path, playbook):
    """nas_present is worked out from the addresses. Given with -e it is a string, and "false" would count as
    true, so each playbook's guard refuses it rather than run with the NAS it was meant to turn off. In site.yml
    the NAS play's guard stops the whole run, since that play has no host left."""
    rc, out = argspec_pass(example_tree(tmp_path), playbook, "-e", "nas_present=false")
    assert rc != 0, out[-3000:]
    assert "nas_present is worked out from the NAS addresses" in out, out[-3000:]
    assert "TASK [truenas :" not in out and "TASK [proxmox_storage :" not in out, out[-3000:]


@pytest.mark.parametrize(
    ("setting", "storage"),
    [
        pytest.param("", "local-lvm", id="lvm-default"),
        pytest.param("proxmox_local_vm_storage: local-zfs\n", "local-zfs", id="zfs-from-local-yml"),
    ],
)
def test_local_yml_names_the_installer_storage(tmp_path, setting, storage):
    """Without a NAS, both VM classes use the installer's storage: local-lvm, or what local.yml names. A
    tracked group_vars file that set it too would win over local.yml, which loads first."""
    tree = example_tree(tmp_path, PROXMOX_ONLY_LOCAL_YML + setting)
    rc, out = ansible_cli(
        tree,
        "ansible",
        *["-i", "inventory.example", "homeserver", "-m", "ansible.builtin.debug", "-a", "var=proxmox_storage_local"],
        *["-e", "ansible_connection=local"],
    )
    assert rc == 0, out[-3000:]
    assert re.search(rf'"proxmox_storage_local": \[\s*"{storage}"\s*\]', out), out[-3000:]


def resolve_on(tree, host, expression):
    """What a Jinja expression resolves to for `host`, through ansible.builtin.debug: the first JSON object in
    the output, so warnings printed after it do not matter."""
    rc, out = ansible_cli(
        tree,
        "ansible",
        *["-i", "inventory.example", host, "-m", "ansible.builtin.debug", "-a", f"msg={{{{ {expression} }}}}"],
        *["-e", "ansible_connection=local"],
    )
    assert rc == 0, out[-3000:]
    return json.JSONDecoder().raw_decode(out[out.index("{") :])[0]["msg"]


@pytest.mark.parametrize(
    ("local_yml", "classes", "exports", "local"),
    [
        pytest.param(
            None,
            ["persistent", "ephemeral"],
            [
                {"id": "homeserver-persistent", "export": "/mnt/nvme_gen3/proxmox/vm"},
                {"id": "homeserver-ephemeral", "export": "/mnt/ephemeral/proxmox/vm"},
            ],
            [],
            id="nas",
        ),
        pytest.param(PROXMOX_ONLY_LOCAL_YML, ["ephemeral"], [], ["local-lvm"], id="proxmox-only"),
    ],
)
def test_layout_resolves_per_setup(tmp_path, local_yml, classes, exports, local):
    """With a NAS both VM classes exist and each export names a storage for its owner and class, never its
    backend. Without one only the ephemeral class exists, there is nothing to export, and the installer's
    storage is the one to check."""
    tree = example_tree(tmp_path, local_yml)
    layout = resolve_on(
        tree, "homeserver", "{'classes': vm_storage | list, 'nfs': proxmox_storage_nfs, 'local': proxmox_storage_local}"
    )
    assert layout == {"classes": classes, "nfs": exports, "local": local}


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
