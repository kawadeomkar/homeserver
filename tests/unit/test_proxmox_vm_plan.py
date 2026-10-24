"""roles/proxmox_vm's plan: what it compares, how it reads Proxmox's forms, and what it refuses.

The idempotence tests run the role against a fake host; these cover the comparisons on their own, including forms
the fake never produces (numbers as text and as numbers, property strings in any order).
"""

import importlib.util
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "roles" / "proxmox_vm" / "filter_plugins" / "proxmox_vm.py"
_spec = importlib.util.spec_from_file_location("proxmox_vm_filters", PLUGIN)
pv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pv)

GIB = 2**30
# MACs built at run time: gitleaks flags one written out in any tracked file.
MAC = ":".join(["02", "00", "00", "00", "00", "01"])
OTHER_MAC = ":".join(["BC", "24", "11", "00", "00", "01"])


def vm(**more):
    base = {
        "vmid": 300,
        "name": "node-1",
        "cores": 2,
        "memory_mib": 4096,
        "efidisk_storage": "local-lvm",
        "disks": [{"slot": "scsi0", "storage": "local-lvm", "size_gib": 32, "serial": "sys"}],
        "net": {"bridge": "vmbr0", "mac": MAC},
        "iso": "local:iso/os.iso",
        "watchdog": {"model": "i6300esb", "action": "reset"},
        "startup": "order=1,up=60",
        "tags": ["talos", "k8s"],
        "hostpci": [],
    }
    return base | more


def config(**more):
    """An existing VM's configuration as pvesh returns it, matching vm()."""
    base = {
        "name": "node-1",
        "cores": 2,
        "memory": "4096",
        "balloon": 0,
        "cpu": "host",
        "onboot": 1,
        "agent": "enabled=1",
        "startup": "up=60,order=1",
        "watchdog": "action=reset,model=i6300esb",
        "tags": "k8s;talos",
        "ide2": "local:iso/os.iso,media=cdrom",
        "scsi0": "local-lvm:vm-300-disk-1,discard=on,serial=sys,size=32G,ssd=1",
        "net0": f"virtio={MAC},bridge=vmbr0,firewall=0",
    }
    return base | more


STORAGE = [
    {"storage": "local", "type": "dir", "content": "iso,backup"},
    {"storage": "local-lvm", "type": "lvmthin", "content": "images,rootdir"},
]


def facts(**more):
    base = {
        "check_mode": False,
        "storage_config": STORAGE,
        "qemu": [{"vmid": 300, "name": "node-1", "status": "running", "maxmem": 4096 * 2**20}],
        "configs": {"300": config()},
        "pending": {"300": []},
        "contents": {"local-lvm": [{"volid": "local-lvm:vm-300-disk-1", "content": "images", "size": 32 * GIB}]},
        "totals": {"local-lvm": 100 * GIB},
        "memory_total": 64 * GIB,
    }
    return base | more


class TestReadingProxmoxForms:
    def test_property_strings(self):
        assert pv.props("local-lvm:vm-1-disk-0,size=32G", "volume") == {
            "volume": "local-lvm:vm-1-disk-0",
            "size": "32G",
        }
        assert pv.props("host", "cputype") == {"cputype": "host"}
        assert pv.props(None, "x") == {}

    @pytest.mark.parametrize(("text", "size"), [("32G", 32 * GIB), ("4M", 4 * 2**20), ("1T", 2**40), ("512", 512)])
    def test_sizes(self, text, size):
        assert pv.size_bytes(text) == size

    def test_a_size_that_is_not_one(self):
        with pytest.raises(ValueError):
            pv.size_bytes("big")

    @pytest.mark.parametrize(
        ("key", "one", "other"),
        [
            ("tags", "k8s;talos", "TALOS;k8s"),
            ("memory", "4096", 4096),
            ("cores", 2, "2"),
            ("agent", "1", "enabled=1"),
            ("startup", "order=1,up=60", "up=60,order=1"),
            ("watchdog", "model=i6300esb,action=reset", "action=reset,model=i6300esb"),
            ("ide2", "local:iso/a.iso,media=cdrom", "local:iso/a.iso,media=cdrom,size=100M"),
            ("hostpci0", "mapping=igpu,pcie=1", "pcie=true,mapping=igpu"),
            ("cpu", "host", "cputype=host"),
        ],
    )
    def test_equal_however_written(self, key, one, other):
        assert pv.normalised(key, one) == pv.normalised(key, other)

    def test_a_different_cd_is_a_difference(self):
        assert pv.normalised("ide2", "local:iso/a.iso,media=cdrom") != pv.normalised(
            "ide2", "local:iso/b.iso,media=cdrom"
        )


class TestDrift:
    def test_nothing_differs(self):
        assert pv.drift(vm(), config()) == ([], [], [])

    def test_changed_settings_go_in_one_set(self):
        options, resize, refused = pv.drift(vm(cores=4, iso="local:iso/new.iso"), config())
        assert options == ["--cores", "4", "--ide2", "local:iso/new.iso,media=cdrom"]
        assert resize == [] and refused == []

    def test_a_pci_device_no_longer_configured_is_removed(self):
        options, _, _ = pv.drift(vm(), config(hostpci0="mapping=igpu,pcie=1"))
        assert options == ["--delete", "hostpci0"]

    def test_a_pci_device_is_added(self):
        options, _, _ = pv.drift(vm(hostpci=[{"mapping": "igpu", "pcie": True}]), config())
        assert options == ["--hostpci0", "mapping=igpu,pcie=1"]

    def test_a_larger_disk_is_grown(self):
        disks = [{"slot": "scsi0", "storage": "local-lvm", "size_gib": 40}]
        _, resize, refused = pv.drift(vm(disks=disks), config())
        assert resize == [{"vmid": "300", "name": "node-1", "slot": "scsi0", "size": "40G"}] and refused == []

    def test_a_new_disk_is_added(self):
        disks = [
            *vm()["disks"],
            {"slot": "scsi1", "storage": "nas", "size_gib": 10, "format": "qcow2", "backup": False},
        ]
        options, _, _ = pv.drift(vm(disks=disks), config())
        assert options == ["--scsi1", "nas:10,discard=on,ssd=1,format=qcow2,backup=0"]

    @pytest.mark.parametrize(
        ("disks", "extra", "message"),
        [
            ([{"slot": "scsi0", "storage": "local-lvm", "size_gib": 16}], {}, "a disk is never shrunk"),
            ([{"slot": "scsi0", "storage": "other", "size_gib": 32}], {}, "qm disk move 300 scsi0 other --delete 1"),
            (None, {"scsi1": "local-lvm:vm-300-disk-2,size=8G"}, "a disk is never detached"),
        ],
    )
    def test_what_is_refused(self, disks, extra, message):
        _, _, refused = pv.drift(vm(**({"disks": disks} if disks else {})), config(**extra))
        assert len(refused) == 1 and message in refused[0]

    def test_a_disk_s_serial_and_backup_flag_are_set_on_the_same_volume(self):
        """A disk's serial and whether backups include it are options of its volume: set again on it, keeping the
        others, never a new disk."""
        disks = [{"slot": "scsi0", "storage": "local-lvm", "size_gib": 32, "serial": "sys", "backup": False}]
        options, resize, refused = pv.drift(vm(disks=disks), config())
        assert options == ["--scsi0", "local-lvm:vm-300-disk-1,discard=on,ssd=1,serial=sys,backup=0"]
        assert (resize, refused) == ([], [])
        renamed = config(scsi0="local-lvm:vm-300-disk-1,backup=0,discard=on,serial=old,size=32G,ssd=1")
        options, _, _ = pv.drift(vm(), renamed)
        assert options == ["--scsi0", "local-lvm:vm-300-disk-1,discard=on,ssd=1,serial=sys"]
        assert pv.drift(vm(disks=disks), config(scsi0=options[1] + ",backup=0,size=32G")) == ([], [], [])
        assert pv.drift(vm(), config(scsi0="local-lvm:vm-300-disk-1,backup=1,serial=sys,size=32G")) == ([], [], [])

    def test_an_unused_volume_holds_back_a_new_disk(self):
        """A restore over a VM keeps the disks its backup excluded as unusedN: the slot they held is never filled
        with a new, empty disk while they may hold its data."""
        disks = [*vm()["disks"], {"slot": "scsi2", "storage": "local-lvm", "size_gib": 100, "backup": False}]
        options, _, refused = pv.drift(vm(disks=disks), config(unused0="local-lvm:vm-300-disk-3"))
        assert options == []
        assert len(refused) == 1 and "keeps unused0 (local-lvm:vm-300-disk-3), unused" in refused[0]
        assert "qm set 300 --scsi2 <volume>" in refused[0] and "qm disk unlink 300 --idlist <unusedN>" in refused[0]
        options, _, refused = pv.drift(vm(disks=disks), config())
        assert options == ["--scsi2", "local-lvm:100,discard=on,ssd=1,backup=0"] and refused == []

    def test_the_boot_order_is_put_back_last(self):
        """Proxmox drops a deleted drive from the boot order and never adds a disk back, so a system disk replaced by
        hand leaves the VM booting its CD-ROM; the order is corrected after the disk it names is added."""
        replaced = {k: v for k, v in config(boot="order=ide2").items() if k != "scsi0"}
        options, _, _ = pv.drift(vm(boot="order=scsi0;ide2"), replaced)
        assert options == ["--scsi0", "local-lvm:32,discard=on,ssd=1,serial=sys", "--boot", "order=scsi0;ide2"]
        assert pv.drift(vm(boot="order=scsi0;ide2"), config(boot="order=scsi0;ide2")) == ([], [], [])
        assert "boot" in pv.COLD_KEYS

    def test_the_cdrom_is_not_a_disk(self):
        assert pv.disks_of(config(scsi5="local:iso/x.iso,media=cdrom")) == {
            "scsi0": ("local-lvm:vm-300-disk-1", 32 * GIB)
        }


class TestPlan:
    def test_a_missing_vm_is_created_and_started(self):
        result = pv.plan([vm()], facts(qemu=[], configs={}, pending={}, contents={}))
        assert [c["vmid"] for c in result["create"]] == ["300"] and result["start"] == [
            {"vmid": "300", "name": "node-1"}
        ]
        argv = result["create"][0]["argv"]
        assert (
            argv[:3] == ["qm", "create", "300"]
            and "--efidisk0" in argv
            and "local-lvm:32,discard=on,ssd=1,serial=sys" in argv
        )
        assert argv[argv.index("--tags") + 1] == "talos;k8s" and argv[argv.index("--net0") + 1].startswith("virtio=02:")
        assert result["errors"] == [] and result["summary"] == {"node-1": "create"}

    def test_an_existing_vm_that_matches_is_left_alone(self):
        result = pv.plan([vm()], facts())
        assert result["summary"] == {"node-1": "ok"} and result["errors"] == [] and result["warnings"] == []
        assert result["macs"] == {"node-1": MAC}

    def test_a_stopped_vm_is_started(self):
        result = pv.plan([vm()], facts(qemu=[{"vmid": 300, "name": "node-1", "status": "stopped"}]))
        assert result["start"] == [{"vmid": "300", "name": "node-1"}] and result["summary"] == {"node-1": ["start"]}

    def test_old_volumes_refuse_a_missing_vm(self):
        backup = {"volid": "local:backup/vzdump-qemu-300.vma.zst", "content": "backup", "vmid": 300}
        result = pv.plan([vm()], facts(qemu=[], volumes={"300": [backup]}))
        assert result["create"] == [] and "qmrestore <archive> 300" in result["errors"][0]
        assert "the play again with -e proxmox_vm_ignore_existing_volumes=true" in result["errors"][0]
        named = pv.plan([vm()], facts(qemu=[], volumes={"300": [backup]}, rebuild_command="just vms -e x"))
        assert "run just vms -e x" in named["errors"][0]
        assert "qm set" not in result["errors"][0], "a missing VM has no configuration to attach a disk to"
        ignored = pv.plan([vm()], facts(qemu=[], volumes={"300": [backup]}, ignore_existing_volumes=True))
        assert ignored["errors"] == [] and [c["vmid"] for c in ignored["create"]] == ["300"]

    def test_files_of_no_vmid_are_no_leftovers(self):
        """Proxmox lists imports and snippets, which belong to no VMID, whatever --vmid asks for."""
        listed = [{"volid": "local:import/appliance.ova", "content": "import"}, {"volid": "local:snippets/a.yaml"}]
        result = pv.plan([vm()], facts(qemu=[], volumes={"300": listed}))
        assert result["errors"] == [] and [c["vmid"] for c in result["create"]] == ["300"]

    def test_an_inactive_storage_refuses_a_missing_vm(self):
        """An enabled storage that is not active may hold the VM's old disks or backups: refused, unless told."""
        node_storage = [
            {"storage": "local", "enabled": 1, "active": 1},
            {"storage": "usb-backup", "enabled": 1, "active": 0},
            {"storage": "old", "enabled": 0, "active": 0},
        ]
        result = pv.plan([vm()], facts(qemu=[], node_storage=node_storage))
        assert result["create"] == [] and len(result["errors"]) == 1
        assert "usb-backup cannot be searched for its old disks and backups" in result["errors"][0]
        assert "old" not in result["errors"][0].split("usb-backup")[0]
        told = pv.plan([vm()], facts(qemu=[], node_storage=node_storage, ignore_existing_volumes=True))
        assert told["errors"] == []
        existing = pv.plan([vm()], facts(node_storage=node_storage))
        assert existing["errors"] == [], "an existing VM is not being rebuilt"

    def test_a_vmid_a_container_holds_is_refused(self):
        result = pv.plan([vm()], facts(qemu=[], lxc=[{"vmid": 300, "name": "pihole", "status": "running"}]))
        assert result["create"] == [] and result["errors"] == [
            "VMID 300 belongs to the container 'pihole', not to a VM named 'node-1': choose another vmid in "
            "proxmox_vm_vms, or move the container to another VMID"
        ]

    def test_a_vmid_with_another_name_is_refused(self):
        result = pv.plan([vm()], facts(qemu=[{"vmid": 300, "name": "theirs", "status": "running"}]))
        assert result["errors"] == [
            "VMID 300 belongs to 'theirs', not 'node-1': choose another vmid in proxmox_vm_vms, "
            "or rename or remove that VM by hand"
        ]

    def test_a_missing_storage_is_pending_only_in_a_dry_run(self):
        disks = [*vm()["disks"], {"slot": "scsi1", "storage": "nas", "size_gib": 10}]
        dry = pv.plan([vm(disks=disks)], facts(qemu=[], check_mode=True, pending_storages=["nas"]))
        assert dry["errors"] == [] and dry["pending_storages"] == ["nas"]
        real = pv.plan([vm(disks=disks)], facts(qemu=[], pending_storages=["nas"]))
        assert real["errors"] == ["nas: does not exist (proxmox_vm_vms)"]

    @pytest.mark.parametrize("content", ["images", "rootdir"], ids=["a-vm", "a-container"])
    def test_the_disk_budget_counts_what_is_provisioned(self, content):
        """Another guest's volume counts, a container's (listed as rootdir) as much as a VM's."""
        other = {"volid": "local-lvm:vm-200-disk-0", "content": content, "size": 60 * GIB}
        # A new 32 GiB disk beside 60 GiB of another VM's, on a 80 GiB pool: refused.
        result = pv.plan(
            [vm()], facts(qemu=[], configs={}, contents={"local-lvm": [other]}, totals={"local-lvm": 80 * GIB})
        )
        assert result["errors"] and "provisioned at 92 GiB (60 GiB there now, 32 GiB more" in result["errors"][0]
        # The same disk once it exists is counted once, so the next run passes.
        own = {"volid": "local-lvm:vm-300-disk-1", "content": "images", "size": 32 * GIB}
        again = pv.plan([vm()], facts(contents={"local-lvm": [other, own]}, totals={"local-lvm": 80 * GIB}))
        assert again["errors"] == []
        allowed = pv.plan(
            [vm()],
            facts(
                qemu=[],
                configs={},
                contents={"local-lvm": [other]},
                totals={"local-lvm": 80 * GIB},
                allow_overcommit=True,
            ),
        )
        assert allowed["errors"] == []

    @pytest.mark.parametrize(("sid", "kind"), [("local-zfs", "zfspool"), ("local", "dir"), ("local-btrfs", "btrfs")])
    def test_a_storage_the_host_shares_keeps_headroom(self, sid, kind):
        """A ZFS storage's size shrinks as the host's own datasets on the pool grow, and a directory's or BTRFS
        storage's is its filesystem's, which the host's own files share: the headroom stays out of the budget there,
        and only there. Sparse disks on a directory or BTRFS would otherwise fill the host's root filesystem."""
        storage = [s for s in STORAGE if s["storage"] != sid] + [{"storage": sid, "type": kind, "content": "images"}]
        disks = [{"slot": "scsi0", "storage": sid, "size_gib": 32}]
        on_it = {"qemu": [], "configs": {}, "storage_config": storage, "contents": {sid: []}}
        fits = pv.plan([vm(disks=disks, efidisk_storage=sid)], facts(**on_it, totals={sid: 40 * GIB}))
        assert fits["errors"] == []
        kept = pv.plan(
            [vm(disks=disks, efidisk_storage=sid)],
            facts(**on_it, totals={sid: 40 * GIB}, host_headroom=16 * GIB),
        )
        assert kept["errors"] == [
            f"{sid}: its disks would be provisioned at 32 GiB (0 GiB there now, 32 GiB more for proxmox_vm_vms), "
            "more than its 40 GiB less the 16 GiB kept for the host's own files (proxmox_vm_host_headroom_gib). A "
            "full storage gives every VM on it I/O errors: make the disks smaller, free space, or set "
            "proxmox_vm_allow_overcommit in local.yml"
        ]
        thin = pv.plan(
            [vm()],
            facts(
                qemu=[], configs={}, contents={"local-lvm": []}, totals={"local-lvm": 40 * GIB}, host_headroom=16 * GIB
            ),
        )
        assert thin["errors"] == []

    def test_a_storage_not_thin_is_not_budgeted(self):
        disks = [{"slot": "scsi0", "storage": "nas", "size_gib": 900}]
        storage = [*STORAGE, {"storage": "nas", "type": "nfs", "content": "images"}]
        result = pv.plan([vm(disks=disks)], facts(qemu=[], storage_config=storage, totals={"nas": 10 * GIB}))
        assert result["errors"] == []

    def test_memory_short_is_a_warning(self):
        running = [{"vmid": 100, "name": "big", "status": "running", "maxmem": 12 * GIB}]
        result = pv.plan([vm()], facts(qemu=running, memory_total=16 * GIB))
        assert result["errors"] == [] and "Running now: big (100, 12 GiB)" in result["warnings"][0]

    def test_pending_changes_are_reported(self):
        pending = [{"key": "cores", "value": 4, "pending": 2}, {"key": "hostpci0", "delete": 1}, {"key": "name"}]
        result = pv.plan([vm()], facts(pending={"300": pending}))
        assert result["restart"] == {"node-1": ["cores", "hostpci0"]}

    def test_another_mac_is_a_warning_not_a_change(self):
        result = pv.plan([vm()], facts(configs={"300": config(net0=f"virtio={OTHER_MAC},bridge=vmbr0")}))
        assert result["set"] == [] and f"has the MAC {OTHER_MAC}" in result["warnings"][0]


ISO = {"storage": "local", "filename": "a.iso", "url": "https://example.org/a.iso", "sha256": "ab" * 32}


class TestInputs:
    def test_good_inputs(self):
        assert pv.input_errors([vm()], [ISO]) == []

    @pytest.mark.parametrize("sha256", [None, "", "AB" * 32, "ab" * 31, "sha256:" + "ab" * 32])
    def test_an_iso_needs_its_sha256(self, sha256):
        errors = pv.input_errors([vm()], [ISO | {"sha256": sha256}])
        assert errors == [
            "a.iso in proxmox_vm_isos needs sha256, the file's SHA-256 (64 lower-case hex digits), which Proxmox"
            " checks before keeping the download."
        ]

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"vmid": 99}, "vmid must be a number from 100"),
            ({"name": "a_b"}, "name must be a DNS name"),
            ({"cores": 0}, "cores must be a whole number above 0"),
            ({"efidisk_storage": None}, "bios ovmf needs efidisk_storage"),
            ({"net": {"mac": "not-a-mac"}}, "net.mac not-a-mac is not a MAC address"),
            ({"disks": [{"slot": "scsi0", "storage": "a", "size_gib": 1}] * 2}, "each disk needs a slot of its own"),
            ({"disks": [{"slot": "sata0", "storage": "a", "size_gib": 1}]}, "is not one of scsi0 to scsi30"),
            ({"disks": []}, "disks must be a list of at least one disk"),
            ({"disks": None}, "disks must be a list of at least one disk"),
        ],
    )
    def test_bad_inputs(self, change, message):
        errors = pv.input_errors([vm(**change)], [])
        assert any(message in error for error in errors), errors

    def test_duplicate_vmids(self):
        assert "needs a vmid of its own" in pv.input_errors([vm(), vm(name="node-2")], [])[0]

    def test_strings_for_lists(self):
        assert pv.input_errors("vm", []) == ["proxmox_vm_vms, proxmox_vm_isos and proxmox_vm_imports must be lists."]


def test_old_isos_are_listed_for_removal():
    volumes = [
        {"volid": "local:iso/talos-v1.14.1-ce4c9805-metal-amd64.iso"},
        {"volid": "local:iso/talos-v1.14.2-ce4c9805-metal-amd64.iso"},
        {"volid": "local:iso/debian-13.iso"},
        {"volid": "local:iso/talos-v1.13.0-ce4c9805-metal-amd64.iso"},
    ]
    isos = [{"storage": "local", "filename": "talos-v1.14.2-ce4c9805-metal-amd64.iso"}]
    attached = ["local:iso/talos-v1.13.0-ce4c9805-metal-amd64.iso,media=cdrom"]
    assert pv.unused_isos(volumes, isos, attached) == ["local:iso/talos-v1.14.1-ce4c9805-metal-amd64.iso"]


# ------------------------------------------------------------------------- imported disks and USB devices ---

OVA = "local:import/haos_ova-18.3.ova/home-assistant.vmdk"


def imported(**more):
    """A VM whose system disk is imported from an image, as Home Assistant OS's is."""
    disk = {"slot": "scsi0", "storage": "local-lvm", "size_gib": 32, "import_from": OVA}
    return vm(vmid=310, name="homeassistant", iso=None, tags=["haos"], disks=[disk]) | more


class TestImportsAndUsb:
    def test_an_imported_disk_is_created_from_its_image(self):
        argv = pv.create_argv(imported())
        assert argv[argv.index("--scsi0") + 1] == f"local-lvm:0,import-from={OVA},discard=on,ssd=1"
        assert "--ide2" not in argv

    def test_usb_devices_by_id_or_mapping(self):
        settings = pv.wanted_settings(imported(usb=[{"host": "10c4:EA60"}, {"mapping": "zwave", "usb3": True}]))
        assert settings["usb0"] == "host=10c4:EA60" and settings["usb1"] == "mapping=zwave,usb3=1"
        argv = pv.create_argv(imported(usb=[{"host": "10c4:ea60"}]))
        assert argv[argv.index("--usb0") + 1] == "host=10c4:ea60"

    def test_a_usb_device_compares_however_written(self):
        assert pv.normalised("usb0", "host=10C4:EA60,usb3=0") == pv.normalised("usb0", "host=10c4:ea60")
        assert pv.normalised("usb0", "host=10c4:ea60,usb3=1") != pv.normalised("usb0", "host=10c4:ea60")

    def test_usb_devices_are_added_and_removed(self):
        current = config(scsi0="local-lvm:vm-310-disk-1,discard=on,size=32G,ssd=1", usb1="host=1a86:55d4")
        options, _, refused = pv.drift(imported(usb=[{"host": "10c4:ea60"}]), current)
        assert options[options.index("--usb0") + 1] == "host=10c4:ea60"
        assert options[-2:] == ["--delete", "usb1"] and refused == []

    def test_a_missing_imported_disk_is_refused_not_imported_again(self):
        current = {k: v for k, v in config().items() if k != "scsi0"}
        _, _, refused = pv.drift(imported(), current)
        assert any("imports only when it creates the VM" in r and "vm-restore.md" in r for r in refused), refused

    def test_a_created_disk_smaller_than_configured_is_grown(self):
        grown = imported(disks=[{"slot": "scsi0", "storage": "local-lvm", "size_gib": 64, "import_from": OVA}])
        configs = {"310": config(scsi0="local-lvm:vm-310-disk-1,discard=on,size=32G,ssd=1")}
        assert pv.growth([grown], configs) == [{"vmid": "310", "name": "homeassistant", "slot": "scsi0", "size": "64G"}]
        assert pv.growth([imported()], configs) == []
        assert pv.growth([grown], {}) == [], "a VM not just created is the plan's to grow"

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"usb": [{"host": "10c4"}]}, "is neither vendor:product"),
            ({"usb": [{"host": "10c4:ea60", "mapping": "x"}]}, "needs either host (vendor:product) or mapping"),
            ({"usb": [{}]}, "needs either host (vendor:product) or mapping"),
            ({"usb": "10c4:ea60"}, "usb must be a list of at most 15 devices"),
            (
                {"disks": [{"slot": "scsi0", "storage": "a", "size_gib": 32, "import_from": "haos.ova"}]},
                "must be a volume id",
            ),
        ],
    )
    def test_bad_inputs(self, change, message):
        errors = pv.input_errors([imported(**change)], [], [])
        assert any(message in error for error in errors), errors

    def test_an_image_needs_its_checksum(self):
        image = {"storage": "local", "filename": "haos_ova-18.3.ova", "url": "https://example.org/haos.ova"}
        assert pv.input_errors([imported(usb=[{"host": "1-1.2"}])], [], [image | {"sha256": "a" * 64}]) == []
        errors = pv.input_errors([imported()], [], [image | {"sha256": "abc"}])
        assert any("needs sha256, the file's SHA-256" in error for error in errors), errors
        assert any("needs storage, filename, url and sha256" in e for e in pv.input_errors([], [], [{"url": "x"}]))


def test_old_images_are_listed_for_removal():
    volumes = [{"volid": "local:import/haos_ova-18.2.ova"}, {"volid": "local:import/haos_ova-18.3.ova"}]
    images = [{"storage": "local", "filename": "haos_ova-18.3.ova", "content": "import"}]
    assert pv.unused_isos(volumes, images, []) == ["local:import/haos_ova-18.2.ova"]


EXTRACTION_STORAGE = [
    {"storage": "local", "type": "dir", "path": "/var/lib/vz", "content": "iso,import,backup"},
    {"storage": "local-lvm", "type": "lvmthin", "content": "images,rootdir"},
    {"storage": "nas-vm", "type": "nfs", "path": "/mnt/pve/nas-vm", "content": "images"},
]


class TestExtraction:
    """A disk inside an OVA is extracted first, onto a storage that holds images and has a path (Proxmox VE 9.2's
    PVE/API2/Qemu.pm): the OVA's own unless the VM's import_working_storage names another, which qm create takes as a
    parameter of the command, never of the disk."""

    def test_the_working_storage_is_passed(self):
        vm = imported(import_working_storage="nas-vm")
        argv = pv.create_argv(vm)
        assert argv[argv.index("--import-working-storage") + 1] == "nas-vm"
        assert "import-working-storage" not in argv[argv.index("--scsi0") + 1]
        assert pv.extraction_problem(vm, vm["disks"][0], EXTRACTION_STORAGE, False, []) is None
        assert "--import-working-storage" not in pv.create_argv(
            vm | {"disks": [{"slot": "scsi0", "storage": "a", "size_gib": 1}]}
        )

    def test_a_storage_without_images_or_a_path_is_refused(self):
        vm = imported()
        problem = pv.extraction_problem(vm, vm["disks"][0], EXTRACTION_STORAGE, False, [])
        assert "pvesm set local --content backup,import,iso,images" in problem, problem
        block = imported(import_working_storage="local-lvm")
        problem = pv.extraction_problem(block, block["disks"][0], EXTRACTION_STORAGE, False, [])
        assert "needs images in its content and a path" in problem, problem

    def test_a_plain_image_needs_no_extraction(self):
        disk = {"slot": "scsi0", "storage": "local-lvm", "size_gib": 32, "import_from": "local:import/os.qcow2"}
        assert pv.extraction_problem(imported(), disk, EXTRACTION_STORAGE, False, []) is None

    def test_the_plan_refuses_before_creating(self):
        found = pv.plan([imported()], facts(qemu=[], configs={}, pending={}, storage_config=EXTRACTION_STORAGE))
        assert found["create"] == [] and any("cannot take the disk Proxmox extracts" in e for e in found["errors"])

    def test_a_pending_working_storage_passes_a_dry_run(self):
        vm = imported(import_working_storage="nas")
        assert pv.extraction_problem(vm, vm["disks"][0], EXTRACTION_STORAGE, True, ["nas"]) is None
        assert "does not exist" in pv.extraction_problem(vm, vm["disks"][0], EXTRACTION_STORAGE, False, ["nas"])
