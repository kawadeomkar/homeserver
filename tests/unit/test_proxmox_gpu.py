"""roles/proxmox_gpu's choices: which GPU it passes, what it refuses, the PCI mapping it keeps and the modprobe.d file.

The idempotence tests run the role against a fake host with a few devices; these cover the choice on its own, over
hosts the fake never builds: several GPUs, shared IOMMU groups, no IOMMU, every form of address. The devices are
made up, in the forms `pvesh get /nodes/<node>/hardware/pci --pci-class-blacklist ""` gives them.
"""

import importlib.util
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "roles" / "proxmox_gpu" / "filter_plugins" / "proxmox_gpu.py"
_spec = importlib.util.spec_from_file_location("proxmox_gpu_filters", PLUGIN)
pg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pg)


def device(address, cls, vendor, dev, group, sub=("0x1458", "0xd000"), name=""):
    out = {
        "id": address,
        "class": cls,
        "vendor": vendor,
        "device": dev,
        "iommugroup": group,
        "vendor_name": {"0x8086": "Intel Corporation", "0x10de": "NVIDIA Corporation"}.get(vendor, "Other"),
        "device_name": name,
    }
    if sub:
        out |= {"subsystem_vendor": sub[0], "subsystem_device": sub[1]}
    return out


IGPU = device("0000:00:02.0", "0x030000", "0x8086", "0xa780", 0, name="Raptor Lake-S GT1 [UHD Graphics 770]")
HOST_BRIDGE = device("0000:00:00.0", "0x060000", "0x8086", "0xa703", 1)
ROOT_PORT = device("0000:00:01.0", "0x060400", "0x8086", "0xa70d", 2)
ARC = device("0000:03:00.0", "0x030000", "0x8086", "0xe20b", 2, ("0x8086", "0x1100"), "Battlemage G21 [Arc B580]")
ARC_AUDIO = device("0000:03:00.1", "0x040300", "0x8086", "0xe2f7", 2, ("0x8086", "0x1100"))
NVIDIA = device("0000:04:00.0", "0x030000", "0x10de", "0x2684", 3, ("0x10de", "0x167c"), "AD102 [GeForce RTX 4090]")
NVIDIA_AUDIO = device("0000:04:00.1", "0x040300", "0x10de", "0x22ba", 3, ("0x10de", "0x167c"))
SATA = device("0000:00:17.0", "0x010601", "0x8086", "0x7a62", 4)
HOST = [HOST_BRIDGE, ROOT_PORT, IGPU, SATA, ARC, ARC_AUDIO]


def sysfs(devices, iommu=True, console="0000:00:02.0", drivers=None):
    """What the role's sysfs script prints for `devices`."""
    drivers = {"0000:00:02.0": "i915", "0000:03:00.0": "xe", "0000:03:00.1": "snd_hda_intel"} | (drivers or {})
    lines = [f"iommu\t{1 if iommu else 0}"]
    for d in devices:
        vga = ("1" if d["id"] == console else "0") if d["class"].startswith("0x03") else "-"
        alias = f"pci:v0000{d['vendor'][2:].upper()}d0000{d['device'][2:].upper()}"
        lines.append(f"device\t{d['id']}\t{vga}\t{drivers.get(d['id'], '-')}\t{alias}")
    return "\n".join(lines) + "\n"


def choose(devices, select, vendors=("8086",), **facts):
    return pg.choose(devices, sysfs(devices, **facts), select, list(vendors), "kubernetes_gpu")


def test_auto_leaves_the_console_alone():
    """The home hosts' case: an iGPU that is the console and nothing else. auto passes nothing, and says how to."""
    result = choose([HOST_BRIDGE, IGPU, SATA], "auto")
    assert result["gpu"] == {} and result["error"] == ""
    assert result["notes"] == [
        "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770] at 0000:00:02.0 is not passed: it is the host's"
        " console, which auto leaves alone: set kubernetes_gpu to 0000:00:02.0 to pass it anyway, and the host's own"
        " screen goes dark."
    ]


def test_auto_takes_the_one_discrete_card_with_every_function():
    """A card behind a root port: the bridge in its IOMMU group is allowed, and the mapping names the slot, so the VM
    gets the audio function too, with function 0's ids."""
    gpu = choose(HOST, "auto")["gpu"]
    assert (gpu["address"], gpu["path"], gpu["functions"]) == (
        "0000:03:00.0",
        "0000:03:00",
        ["0000:03:00.0", "0000:03:00.1"],
    )
    assert (gpu["vendor"], gpu["entry_id"], gpu["subsystem"], gpu["iommugroup"]) == (
        "8086",
        "8086:e20b",
        "8086:1100",
        2,
    )
    assert gpu["vfio_ids"] == ["8086:e20b:8086:1100", "8086:e2f7:8086:1100"]
    assert gpu["boot_vga"] is False and gpu["host_drivers"] == {"0000:03:00.0": "xe", "0000:03:00.1": "snd_hda_intel"}
    assert gpu["name"] == "Intel Corporation Battlemage G21 [Arc B580]"


def test_auto_skips_a_vendor_not_supported_yet():
    result = choose([*HOST, NVIDIA, NVIDIA_AUDIO], "auto")
    assert result["gpu"]["address"] == "0000:03:00.0"
    assert any(
        "RTX 4090] at 0000:04:00.0 is not passed: its vendor (NVIDIA Corporation) is not supported here yet" in n
        for n in result["notes"]
    ), result["notes"]


def test_auto_refuses_to_choose_between_two():
    result = choose([*HOST, NVIDIA, NVIDIA_AUDIO], "auto", vendors=())
    assert result["gpu"] == {}
    assert result["error"].startswith(
        "Several GPUs could be passed: Intel Corporation Battlemage G21 [Arc B580] at"
        " 0000:03:00.0; NVIDIA Corporation AD102 [GeForce RTX 4090] at 0000:04:00.0."
    )
    assert result["error"].endswith("Name the one to pass in kubernetes_gpu.")


def test_a_shared_iommu_group_is_skipped_by_auto_and_refused_by_address():
    """vfio takes every device of the group but bridges from the host: here its SATA controller."""
    shared = [d | {"iommugroup": 2} if d is SATA else d for d in HOST]
    auto = choose(shared, "auto")
    assert auto["gpu"] == {} and any("shares IOMMU group 2 with 0000:00:17.0" in n for n in auto["notes"])
    named = choose(shared, "03:00.0")
    assert named["error"].startswith(
        "Intel Corporation Battlemage G21 [Arc B580] at 0000:03:00.0, which kubernetes_gpu"
        " names, cannot be passed: it shares IOMMU group 2 with 0000:00:17.0"
    )


@pytest.mark.parametrize("devices", [HOST, [d | {"iommugroup": -1} for d in HOST]], ids=["no-unit", "no-group"])
def test_without_an_iommu_nothing_is_passed(devices):
    auto = choose(devices, "auto", iommu=devices is not HOST)
    assert auto["gpu"] == {} and any("turn on VT-d (Intel) or AMD-Vi (AMD)" in n for n in auto["notes"])
    assert "turn on VT-d" in choose(devices, "0000:03:00.0", iommu=devices is not HOST)["error"]


@pytest.mark.parametrize(
    "address", ["0000:03:00.0", "03:00.0", "0000:03:00", "03:00", "0000:03:00.0 ", "03:00.0".upper()]
)
def test_an_address_in_any_form(address):
    assert choose(HOST, address)["gpu"]["address"] == "0000:03:00.0"


def test_the_console_by_address_is_passed_with_a_warning():
    """The iGPU named on purpose: a single function, so the mapping names the function itself."""
    result = choose([HOST_BRIDGE, IGPU, SATA], "0000:00:02.0")
    gpu = result["gpu"]
    assert (gpu["path"], gpu["functions"], gpu["vfio_ids"]) == (
        "0000:00:02.0",
        ["0000:00:02.0"],
        ["8086:a780:1458:d000"],
    )
    assert result["notes"] == [
        "Intel Corporation Raptor Lake-S GT1 [UHD Graphics 770] at 0000:00:02.0 is the host's console: once vfio-pci"
        " takes it, the host's own screen goes dark, and the network is the only way in."
    ]


def test_a_card_taken_out_means_no_gpu():
    """The VM must still start: no GPU, said, never refused."""
    result = choose([HOST_BRIDGE, IGPU, SATA], "03:00.0")
    assert result["gpu"] == {} and result["error"] == ""
    assert result["notes"] == [
        "kubernetes_gpu names 03:00.0, and the host has no device there: the VM gets no GPU until it is back."
    ]


def test_an_address_that_is_not_a_gpu_is_refused():
    assert choose(HOST, "00:17.0")["error"].startswith("kubernetes_gpu names 00:17.0, which is not a GPU")


def test_an_unsupported_vendor_named_is_refused():
    result = choose([*HOST, NVIDIA, NVIDIA_AUDIO], "04:00.0")
    assert "its vendor (NVIDIA Corporation) is not supported here yet" in result["error"]


@pytest.mark.parametrize("select", ["", "none", "None", None])
def test_none_reads_nothing(select):
    assert pg.choose(None, None, select, ["8086"]) == {"gpu": {}, "notes": [], "error": ""}


def test_the_subsystem_is_kept_whatever_it_is():
    """Proxmox expects subsystem-id whenever the host lists one, even 0000:0000, and refuses an entry without it."""
    zero = choose([ARC | {"subsystem_vendor": "0x0000", "subsystem_device": "0x0000"}], "auto", console="")["gpu"]
    assert zero["subsystem"] == "0000:0000" and zero["vfio_ids"] == ["8086:e20b:0000:0000"]
    none = choose([device("0000:03:00.0", "0x030000", "0x8086", "0xe20b", 2, None)], "auto", console="")["gpu"]
    assert none["subsystem"] == "" and none["vfio_ids"] == ["8086:e20b"]


@pytest.mark.parametrize(
    ("select", "vendors", "mapping", "expected"),
    [
        ("auto", ["8086"], "homeserver-gpu", []),
        ("none", "8086", "", ['proxmox_gpu_vendors must be a list of PCI vendor ids, such as ["8086"], not a string.']),
        ("", ["0x10DE"], "", []),
        (
            "",
            ["intel"],
            "",
            ["proxmox_gpu_vendors must hold PCI vendor ids of four hex digits, such as 8086, not ['intel']."],
        ),
        (
            "igpu",
            [],
            "gpu",
            [
                "kubernetes_gpu must be auto, none, or a PCI address as lspci or Proxmox's PCI list shows it,"
                " such as 0000:01:00.0 or 01:00.0, not 'igpu'."
            ],
        ),
        (
            "01:00.0",
            [],
            "",
            [
                "proxmox_gpu_mapping must name the PCI resource mapping the GPU is kept under (letters,"
                " digits, - and _, starting with a letter), not '': a VM is given the GPU through it."
            ],
        ),
    ],
)
def test_select_errors(select, vendors, mapping, expected):
    assert pg.select_errors(select, vendors, mapping, "kubernetes_gpu") == expected


ARC_ENTRY = "node=pve,path=0000:03:00,id=8086:e20b,subsystem-id=8086:1100,iommugroup=2"


def test_a_missing_mapping_is_created():
    gpu = choose(HOST, "auto")["gpu"]
    plan = pg.mapping_plan([], "homeserver-gpu", "pve", gpu, "The worker's GPU")
    assert plan == {
        "action": "create",
        "argv": [
            "pvesh",
            "create",
            "/cluster/mapping/pci",
            "--id",
            "homeserver-gpu",
            "--map",
            ARC_ENTRY,
            "--description",
            "The worker's GPU",
        ],
        "entry": ARC_ENTRY,
        "checks": [],
    }


def test_a_mapping_in_proxmox_forms_is_left_alone():
    """Proxmox may give the entry's fields in another order and its ids with 0x: the same mapping. It compares case
    as it is, so an id in upper case would stop the VM starting, and is set again."""
    gpu = choose(HOST, "auto")["gpu"]
    entry = "iommugroup=2,id=0x8086:0xe20b,path=0000:03:00,node=pve,subsystem-id=0x8086:0x1100"
    plan = pg.mapping_plan([{"id": "homeserver-gpu", "map": [entry], "digest": "d1"}], "homeserver-gpu", "pve", gpu)
    assert plan["action"] == "ok" and plan["argv"] == []
    upper = [{"id": "homeserver-gpu", "map": [entry.replace("e20b", "E20B")], "digest": "d1"}]
    assert pg.mapping_plan(upper, "homeserver-gpu", "pve", gpu)["action"] == "set"


def test_a_changed_card_corrects_this_nodes_entry_and_keeps_the_others():
    """The IOMMU group renumbered (another card added): this node's entry is set again, another node's sent back as
    it was, under the digest read."""
    gpu = choose(HOST, "auto")["gpu"]
    other = "node=pve2,path=0000:01:00,id=10de:2684"
    stale = ARC_ENTRY.replace("iommugroup=2", "iommugroup=7")
    mappings = [
        {
            "id": "homeserver-gpu",
            "map": [other, stale],
            "digest": "d1",
            "checks": [{"severity": "error", "message": "Invalid configuration: 'iommugroup' does not match"}],
        }
    ]
    plan = pg.mapping_plan(mappings, "homeserver-gpu", "pve", gpu)
    assert plan["action"] == "set"
    assert plan["argv"] == [
        "pvesh",
        "set",
        "/cluster/mapping/pci/homeserver-gpu",
        "--map",
        other,
        "--map",
        ARC_ENTRY,
        "--digest",
        "d1",
    ]
    assert plan["checks"] == ["Invalid configuration: 'iommugroup' does not match"]


def test_no_gpu_or_no_mapping_plans_nothing():
    assert pg.mapping_plan([], "homeserver-gpu", "pve", {})["action"] == "none"
    assert pg.mapping_plan([], "", "pve", choose(HOST, "auto")["gpu"])["action"] == "none"


def test_the_modprobe_file_claims_every_function_first():
    gpu = choose(HOST, "auto")["gpu"]
    drivers = {"0000:03:00.0": ["xe"], "0000:03:00.1": ["snd_hda_intel", "vfio_pci"]}
    assert pg.modprobe_conf(gpu, drivers, "Written by Ansible") == (
        "# Written by Ansible\n"
        "options vfio-pci ids=8086:e20b:8086:1100,8086:e2f7:8086:1100\n"
        "softdep snd_hda_intel pre: vfio-pci\n"
        "softdep xe pre: vfio-pci\n"
    )
    assert pg.modprobe_conf({}, drivers) == ""


def test_held_by_host_names_the_drivers_but_vfio():
    gpu = choose(HOST, "auto")["gpu"]
    assert pg.held_by_host(gpu) == ["0000:03:00.0 (xe)", "0000:03:00.1 (snd_hda_intel)"]
    gpu["host_drivers"] = {"0000:03:00.0": "vfio-pci", "0000:03:00.1": ""}
    assert pg.held_by_host(gpu) == [] and pg.held_by_host({}) == []
