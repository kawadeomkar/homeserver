"""roles/talos's readers of talosctl's and kubectl's output.

The idempotence tests run against a fake talosctl; these pin down the parsing itself, on output laid out as
talosctl v1.14 prints it, including what the fake never prints.
"""

import importlib.util
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "roles" / "talos" / "filter_plugins" / "talos.py"
_spec = importlib.util.spec_from_file_location("talos_filters", PLUGIN)
tf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tf)

VERSION = """Client:
\tTag:         v1.14.9
\tSHA:         abcdef
\tOS/Arch:     darwin/arm64
Server:
\tNODE:        192.0.2.30
\tTag:         v1.14.2
\tSHA:         123456
\tOS/Arch:     linux/amd64
\tEnabled:     RBAC
"""


def test_the_node_tag_is_the_nodes_not_the_clients():
    assert tf.node_tag(VERSION, "192.0.2.30") == "v1.14.2"


def test_another_node_has_no_tag_here():
    assert tf.node_tag(VERSION, "192.0.2.31") is None
    assert tf.node_tag("", "192.0.2.30") is None


def test_the_clients_minor_release():
    assert tf.client_minor("Client:\nTalos v1.14.2") == "v1.14"
    assert tf.client_minor("Client:\n\tTag:         v1.13.11") == "v1.13"
    assert tf.client_minor("nothing") is None


def test_minor():
    assert tf.minor("v1.14.2") == "v1.14"
    assert tf.minor(None) is None


def test_the_booted_schematic():
    docs = [
        {"spec": {"metadata": {"name": "qemu-guest-agent", "version": "10.0.0"}}},
        {"spec": {"metadata": {"name": "schematic", "version": "ce4c9805"}}},
    ]
    assert tf.schematic(docs) == "ce4c9805"
    assert tf.schematic([None, {}]) is None


def test_a_service_state():
    assert tf.service_state("NODE     192.0.2.30\nID       etcd\nSTATE    Running\nHEALTH   OK") == "Running"
    assert tf.service_state("") is None


def test_an_install_status():
    pending = (
        "node: 192.0.2.30\nmetadata:\n  id: unattended-install\nspec:\n  phase: pending\n  error: no disk matched\n"
    )
    assert tf.install_status(pending) == {"phase": "pending", "error": "no disk matched"}
    assert tf.install_status("spec:\n  phase: installed\n") == {"phase": "installed", "error": None}
    # As Talos 1.14.2 prints a pending install: the phase, its zero value, left out; metadata.phase is the resource's.
    seen = (
        "metadata:\n  id: unattended-install\n  phase: running\n"
        "spec:\n  image: x\n  error: no disk matched the selector\n"
    )
    assert tf.install_status(seen) == {"phase": "pending", "error": "no disk matched the selector"}
    for nothing in ("", None, "error: rpc error: code = Unavailable", "{not: [yaml", "- a list\n"):
        assert tf.install_status(nothing) == {"phase": None, "error": None}


def test_the_servers_kubernetes_version():
    assert tf.server_version('{"serverVersion": {"gitVersion": "v1.37.1"}}') == "1.37.1"
    assert tf.server_version("not json") is None
    assert tf.server_version('{"clientVersion": {}}') is None


def test_volume_phases():
    out = (
        "metadata:\n  id: EPHEMERAL\nspec:\n  phase: ready\n---\n"
        "metadata:\n  id: u-local-path-provisioner\nspec:\n  phase: waiting\n"
    )
    assert tf.volume_phases(out) == {"EPHEMERAL": "ready", "u-local-path-provisioner": "waiting"}
    assert tf.volume_phases("") == {} and tf.volume_phases("{bad: [") == {}


def test_user_volumes():
    patches = [
        "apiVersion: v1alpha1\nkind: HostnameConfig\nhostname: a\n",
        "apiVersion: v1alpha1\nkind: UserVolumeConfig\nname: local-path-provisioner\n",
        "apiVersion: v1alpha1\nkind: UserVolumeConfig\nname: media\n",
    ]
    assert tf.user_volumes(patches) == ["local-path-provisioner", "media"]
    assert tf.user_volumes([]) == []


def test_the_installer_image_carries_its_pinned_digest():
    digests = {"abc:v1.14.2": "sha256:" + "1" * 64}
    assert tf.installer_image("factory.talos.dev/metal-installer", "abc", "v1.14.2", digests) == (
        "factory.talos.dev/metal-installer/abc:v1.14.2@sha256:" + "1" * 64
    )
    # A pair without a pin, or no pins at all: the tag alone.
    assert tf.installer_image("f/metal-installer", "abc", "v1.14.3", digests) == "f/metal-installer/abc:v1.14.3"
    assert tf.installer_image("f/metal-installer", "abc", "v1.14.2", {}) == "f/metal-installer/abc:v1.14.2"


def test_an_upgrade_names_the_installer_by_its_digest_alone():
    digest = "sha256:" + "1" * 64
    assert tf.image_by_digest(f"factory.talos.dev/metal-installer/abc:v1.14.4@{digest}") == (
        f"factory.talos.dev/metal-installer/abc@{digest}"
    )
    # A registry's port is not a tag; a name without a registry, or already without a tag, keeps its name.
    assert (
        tf.image_by_digest(f"registry.test:5000/installer:v1.14.4@{digest}") == f"registry.test:5000/installer@{digest}"
    )
    assert tf.image_by_digest(f"installer:v1.14.4@{digest}") == f"installer@{digest}"
    assert tf.image_by_digest(f"f/metal-installer/abc@{digest}") == f"f/metal-installer/abc@{digest}"
    # Without a digest there is nothing else to name it by.
    assert tf.image_by_digest("f/metal-installer/abc:v1.14.4") == "f/metal-installer/abc:v1.14.4"
