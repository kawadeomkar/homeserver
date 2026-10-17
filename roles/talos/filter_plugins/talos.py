"""Readers for talosctl's and kubectl's output, for roles/talos. Pure: they run on the controller, never connect
anywhere, and are unit-tested in tests/unit/test_talos_filters.py."""

import json
import re

import yaml


def node_tag(stdout, address):
    """The Talos version a node runs, from `talosctl version`: the Tag that follows `NODE: <address>`. The output
    starts with the client's own block, whose Tag is the workstation's talosctl, never the node's."""
    text = str(stdout or "")
    found = re.search(rf"NODE:\s*{re.escape(str(address))}\s*\n(?:.*\n)*?\s*Tag:\s*(\S+)", text)
    return found.group(1) if found else None


def client_minor(stdout):
    """The workstation talosctl's major and minor (v1.14), from `talosctl version --client --short`."""
    found = re.search(r"(?:Talos|Tag:)\s*(v\d+\.\d+)", str(stdout or ""))
    return found.group(1) if found else None


def minor(tag):
    """v1.14.2 -> v1.14."""
    found = re.match(r"(v?\d+\.\d+)", str(tag or ""))
    return found.group(1) if found else None


def schematic(documents):
    """The schematic id a node booted, from `talosctl get extensions -o yaml`: the extension named schematic."""
    for document in documents or []:
        metadata = ((document or {}).get("spec") or {}).get("metadata") or {}
        if metadata.get("name") == "schematic":
            return metadata.get("version")
    return None


def service_state(stdout):
    """A service's state (Running, Preparing...) from `talosctl service <id>`."""
    found = re.search(r"^\s*STATE\s+(\S+)", str(stdout or ""), re.MULTILINE)
    return found.group(1) if found else None


def install_status(stdout):
    """A node's unattended install, from `talosctl get unattendedinstallstatuses -o yaml`: {phase, error}, each None
    when not reported. The phase is pending (no disk matches the selector), installing, waiting-for-reboot,
    installed or failed. Output that is not YAML, such as nothing from a node that does not answer, reports
    neither."""
    try:
        documents = list(yaml.safe_load_all(str(stdout or "")))
    except yaml.YAMLError:
        documents = []
    for document in documents:
        spec = (document or {}).get("spec") if isinstance(document, dict) else None
        if isinstance(spec, dict) and spec.get("phase"):
            error = next((str(spec[key]) for key in ("error", "message", "reason") if spec.get(key)), None)
            return {"phase": str(spec["phase"]), "error": error}
    return {"phase": None, "error": None}


def volume_phases(stdout):
    """Each volume's phase (ready, waiting...) by its id (u-<name> for a user volume), from
    `talosctl get volumestatus -o yaml`. Output that is not YAML reports none."""
    try:
        documents = list(yaml.safe_load_all(str(stdout or "")))
    except yaml.YAMLError:
        return {}
    out = {}
    for document in documents:
        if not isinstance(document, dict):
            continue
        volume = (document.get("metadata") or {}).get("id")
        if volume:
            out[str(volume)] = str((document.get("spec") or {}).get("phase") or "")
    return out


def user_volumes(patches):
    """The names of the user volumes a node's patches declare (UserVolumeConfig documents)."""
    names = []
    for patch in patches or []:
        try:
            documents = list(yaml.safe_load_all(str(patch)))
        except yaml.YAMLError:
            continue
        for document in documents:
            if isinstance(document, dict) and document.get("kind") == "UserVolumeConfig" and document.get("name"):
                names.append(str(document["name"]))
    return names


def server_version(stdout):
    """The cluster's Kubernetes version (1.37.1), from `kubectl version -o json`."""
    try:
        data = json.loads(str(stdout or ""))
    except ValueError:
        return None
    version = ((data or {}).get("serverVersion") or {}).get("gitVersion")
    return str(version).lstrip("v") if version else None


class FilterModule:
    def filters(self):
        return {
            "talos_node_tag": node_tag,
            "talos_client_minor": client_minor,
            "talos_minor": minor,
            "talos_schematic": schematic,
            "talos_service_state": service_state,
            "talos_install_status": install_status,
            "talos_server_version": server_version,
            "talos_volume_phases": volume_phases,
            "talos_user_volumes": user_volumes,
        }
