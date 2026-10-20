"""What roles/proxmox_vm has to do, worked out from what it read from the Proxmox node.

The role's tasks read the node with pvesh and qm, pass what they read to proxmox_vm_plan, and run the commands the
plan returns. Everything here is pure: it runs on the controller, never connects anywhere, and is unit-tested in
tests/unit/test_proxmox_vm_plan.py. Proxmox writes a VM's settings back in its own forms (tags lower-cased and
sorted, a disk as a volume id with its size, property strings in any order, numbers as text or as numbers), so
each setting is compared in a normalised form, never as text.
"""

import re

GIB = 2**30
MIB = 2**20
# Storage types whose space is shared by every disk on them, and so counted against their size: thin LVM, ZFS, and
# the host's own directories and BTRFS, where sparse disks would otherwise fill the root filesystem as they fill. An
# NFS storage is not budgeted: only a cap set where it lives holds its disks, such as a quota on the NAS's dataset.
BUDGETED_TYPES = ("lvmthin", "zfspool", "dir", "btrfs")
# Those whose size also holds the host's own files (a ZFS install's root filesystem on the same pool, a directory's
# or BTRFS volume's filesystem): their size shrinks as those grow, so host_headroom stays out of the budget.
HOST_SHARED_TYPES = ("zfspool", "dir", "btrfs")
# The content types of the volumes counted there: VMs' disks, and containers', which Proxmox lists as rootdir.
GUEST_CONTENT = ("images", "rootdir")
# What an EFI disk takes, whatever size it is created with.
EFI_DISK_BYTES = 4 * MIB
# Head-room the node keeps for itself when the VMs' memory is added up.
NODE_MEMORY_RESERVE = 2 * GIB
# Settings Proxmox applies to a running VM only at its next start: they show in the configuration at once, and
# as pending until then.
COLD_KEYS = ("cores", "cpu", "memory", "balloon", "watchdog", "agent", "boot", "hostpci")
SIZE_UNITS = {"": 1, "K": 2**10, "M": 2**20, "G": 2**30, "T": 2**40}
VM_NAME = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$", re.IGNORECASE)
MAC = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$", re.IGNORECASE)
DISK_SLOT = re.compile(r"^scsi([0-9]|[12][0-9]|30)$")
# Proxmox's USB slots (usb0 to usb14 on a q35 machine), and a device as `host=` takes it: vendor:product, which
# follows the device to any port, or bus-port.
USB_SLOT = re.compile(r"^usb([0-9]|1[0-4])$")
USB_HOST = re.compile(r"^([0-9a-f]{4}:[0-9a-f]{4}|[0-9]+-[0-9]+(\.[0-9]+)*)$", re.IGNORECASE)
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def props(text, default_key):
    """A Proxmox property string, such as "model=i6300esb,action=reset", as a dict. A first item without "=" is the
    value of `default_key`, as in "local-lvm:vm-300-disk-1,size=32G" or "host"."""
    out = {}
    for index, item in enumerate(str(text if text is not None else "").split(",")):
        if not item:
            continue
        key, sep, value = item.partition("=")
        if not sep and index == 0:
            out[default_key] = key
        else:
            out[key] = value
    return out


def size_bytes(text):
    """A size as Proxmox writes it (32G, 4M, 1T, or bytes) in bytes."""
    found = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)B?\s*", str(text), re.IGNORECASE)
    if not found:
        raise ValueError(f"not a size: {text!r}")
    return int(float(found.group(1)) * SIZE_UNITS[found.group(2).upper()])


def gib(size):
    """A size in GiB, to a tenth: an EFI disk's 4 MiB does not show."""
    return f"{round(size / GIB, 1):g} GiB"


def volume_storage(volid):
    return str(volid).split(":", 1)[0]


# ---------------------------------------------------------------------------------- the wanted settings ---


def wanted_settings(vm):
    """The settings the role compares on an existing VM, as `qm set` takes them."""
    out = {
        "cores": str(vm["cores"]),
        "memory": str(vm["memory_mib"]),
        "balloon": "0",
        "cpu": vm.get("cpu", "host"),
        "onboot": "1" if vm.get("onboot", True) else "0",
        "agent": "enabled=1",
    }
    if vm.get("startup"):
        out["startup"] = vm["startup"]
    if vm.get("watchdog"):
        out["watchdog"] = f"model={vm['watchdog']['model']},action={vm['watchdog'].get('action', 'reset')}"
    if vm.get("tags"):
        out["tags"] = ";".join(vm["tags"])
    if vm.get("iso"):
        out["ide2"] = f"{vm['iso']},media=cdrom"
    # Compared, not only set at creation: Proxmox drops a deleted drive from the order and never adds a disk back,
    # so a system disk replaced by hand would leave the VM booting its CD-ROM for good.
    if vm.get("boot"):
        out["boot"] = vm["boot"]
    for index, device in enumerate(vm.get("hostpci") or []):
        out[f"hostpci{index}"] = f"mapping={device['mapping']}" + (",pcie=1" if device.get("pcie", True) else "")
    for index, device in enumerate(vm.get("usb") or []):
        source = f"host={device['host']}" if device.get("host") else f"mapping={device['mapping']}"
        out[f"usb{index}"] = source + (",usb3=1" if device.get("usb3") else "")
    return out


def normalised(key, value):
    """A setting in a form that compares equal however Proxmox writes it back."""
    if value is None:
        return None
    if key in ("cores", "memory", "balloon", "onboot", "sockets"):
        return int(props(value, "current").get("current", 0))
    if key == "tags":
        return sorted({tag.strip().lower() for tag in re.split(r"[;, ]", str(value)) if tag.strip()})
    if key == "cpu":
        return props(value, "cputype")
    if key == "agent":
        found = props(value, "enabled")
        return {k: ("1" if v in ("1", "true", "yes", "on") else "0") if k == "enabled" else v for k, v in found.items()}
    if key == "ide2":
        found = props(value, "volume")
        return {"volume": found.get("volume"), "media": found.get("media", "disk")}
    if key.startswith("hostpci"):
        found = props(value, "host")
        return {k: ("1" if v in ("1", "true", "yes", "on") else "0") if k == "pcie" else v for k, v in found.items()}
    if USB_SLOT.match(key):
        found = props(value, "host")
        usb3 = "1" if found.pop("usb3", "0") in ("1", "true", "yes", "on") else "0"
        return {**{k: v.lower() if k == "host" else v for k, v in found.items()}, "usb3": usb3}
    return props(value, "_")


def disk_option(disk):
    """A disk as `qm create` or `qm set` takes it: a new volume of the given size on its storage, or one imported from
    an image (import_from, a volume id), which Proxmox's special size 0 asks for: it takes the image's size, and the
    role grows it to size_gib afterwards."""
    if disk.get("import_from"):
        parts = [f"{disk['storage']}:0", f"import-from={disk['import_from']}", "discard=on", "ssd=1"]
    else:
        parts = [f"{disk['storage']}:{disk['size_gib']}", "discard=on", "ssd=1"]
    if disk.get("serial"):
        parts.append(f"serial={disk['serial']}")
    if disk.get("format"):
        parts.append(f"format={disk['format']}")
    if disk.get("backup") is False:
        parts.append("backup=0")
    return ",".join(parts)


def net_option(vm):
    net = vm.get("net") or {}
    model = "virtio" + (f"={net['mac']}" if net.get("mac") else "")
    return f"{model},bridge={net.get('bridge', 'vmbr0')},firewall=0"


def create_argv(vm):
    """`qm create` with every option given, so nothing depends on Proxmox's defaults."""
    vmid = str(vm["vmid"])
    argv = ["qm", "create", vmid, "--name", vm["name"], "--ostype", "l26", "--machine", vm.get("machine", "q35")]
    argv += ["--bios", vm.get("bios", "ovmf")]
    if vm.get("bios", "ovmf") == "ovmf":
        argv += ["--efidisk0", f"{vm['efidisk_storage']}:1,efitype=4m,pre-enrolled-keys=0"]
    argv += ["--cpu", vm.get("cpu", "host"), "--sockets", "1", "--cores", str(vm["cores"])]
    argv += ["--memory", str(vm["memory_mib"]), "--balloon", "0", "--scsihw", vm.get("scsihw", "virtio-scsi-pci")]
    for disk in vm["disks"]:
        argv += [f"--{disk['slot']}", disk_option(disk)]
    argv += ["--net0", net_option(vm), "--agent", "enabled=1", "--serial0", "socket", "--vga", "std"]
    # Where Proxmox extracts a disk out of an OVA before converting it: one storage for every import of the command.
    if vm.get("import_working_storage") and any(disk.get("import_from") for disk in vm["disks"]):
        argv += ["--import-working-storage", vm["import_working_storage"]]
    settings = wanted_settings(vm)
    for key in ("watchdog", "startup", "tags", "ide2"):
        if key in settings:
            argv += [f"--{key}", settings[key]]
    argv += ["--onboot", settings["onboot"]]
    if vm.get("boot"):
        argv += ["--boot", vm["boot"]]
    for index in range(len(vm.get("hostpci") or [])):
        argv += [f"--hostpci{index}", settings[f"hostpci{index}"]]
    for index in range(len(vm.get("usb") or [])):
        argv += [f"--usb{index}", settings[f"usb{index}"]]
    return argv


# ------------------------------------------------------------------------------------------ the checks ---


def input_errors(vms, isos, imports=None):
    """Problems with proxmox_vm_vms, proxmox_vm_isos and proxmox_vm_imports, one sentence each, naming the
    variable."""
    errors = []
    imports = [] if imports is None else imports
    if any(isinstance(value, (str, dict)) for value in (vms, isos, imports)):
        return ["proxmox_vm_vms, proxmox_vm_isos and proxmox_vm_imports must be lists."]
    vmids = [vm.get("vmid") for vm in vms]
    if len(set(map(str, vmids))) != len(vmids):
        errors.append("Each VM in proxmox_vm_vms needs a vmid of its own.")
    names = [vm.get("name") for vm in vms]
    if len(set(names)) != len(names):
        errors.append("Each VM in proxmox_vm_vms needs a name of its own.")
    for vm in vms:
        label = f"VM {vm.get('vmid')} ({vm.get('name')}) in proxmox_vm_vms"
        if not (isinstance(vm.get("vmid"), int) or str(vm.get("vmid", "")).isdigit()) or not (
            100 <= int(vm.get("vmid") or 0) <= 999999999
        ):
            errors.append(f"{label}: vmid must be a number from 100 to 999999999.")
        if not VM_NAME.match(str(vm.get("name") or "")):
            errors.append(f"{label}: name must be a DNS name (letters, digits, hyphens and dots).")
        for key in ("cores", "memory_mib"):
            if not str(vm.get(key, "")).isdigit() or int(vm[key]) < 1:
                errors.append(f"{label}: {key} must be a whole number above 0.")
        disks = vm.get("disks") or []
        if not isinstance(vm.get("disks"), list) or not disks:
            errors.append(f"{label}: disks must be a list of at least one disk.")
            disks = []
        slots = [disk.get("slot") for disk in disks]
        if len(set(slots)) != len(slots):
            errors.append(f"{label}: each disk needs a slot of its own.")
        for disk in disks:
            if not DISK_SLOT.match(str(disk.get("slot"))):
                errors.append(f"{label}: disk slot {disk.get('slot')} is not one of scsi0 to scsi30.")
            if not disk.get("storage") or not str(disk.get("size_gib", "")).isdigit() or int(disk["size_gib"]) < 1:
                errors.append(f"{label}: disk {disk.get('slot')} needs a storage and a size_gib above 0.")
            if disk.get("import_from") is not None and ":" not in str(disk["import_from"]):
                errors.append(
                    f"{label}: disk {disk.get('slot')}'s import_from must be a volume id, such as "
                    f"local:import/<image>, not {disk['import_from']}."
                )
        if vm.get("bios", "ovmf") == "ovmf" and not vm.get("efidisk_storage"):
            errors.append(f"{label}: bios ovmf needs efidisk_storage.")
        mac = (vm.get("net") or {}).get("mac")
        if mac and not MAC.match(str(mac)):
            errors.append(f"{label}: net.mac {mac} is not a MAC address.")
        usb = vm.get("usb") or []
        if not isinstance(usb, list) or len(usb) > 15:
            errors.append(f"{label}: usb must be a list of at most 15 devices.")
            usb = []
        for device in usb:
            if not isinstance(device, dict) or bool(device.get("host")) == bool(device.get("mapping")):
                errors.append(f"{label}: each usb device needs either host (vendor:product) or mapping: {device}.")
            elif device.get("host") and not USB_HOST.match(str(device["host"])):
                errors.append(
                    f"{label}: usb host {device['host']} is neither vendor:product (lsusb's ID, such as 10c4:ea60) "
                    f"nor bus-port."
                )
    for iso in isos:
        if not iso.get("storage") or not iso.get("filename") or not iso.get("url"):
            errors.append(f"Each ISO in proxmox_vm_isos needs storage, filename and url: {iso}.")
        elif not SHA256.match(str(iso.get("sha256") or "")):
            errors.append(
                f"{iso['filename']} in proxmox_vm_isos needs sha256, the file's SHA-256 (64 lower-case hex digits),"
                " which Proxmox checks before keeping the download."
            )
    for image in imports:
        if not image.get("storage") or not image.get("filename") or not image.get("url"):
            errors.append(f"Each image in proxmox_vm_imports needs storage, filename, url and sha256: {image}.")
        elif not SHA256.match(str(image.get("sha256") or "")):
            errors.append(
                f"{image['filename']} in proxmox_vm_imports needs sha256, the file's SHA-256 (64 lower-case hex"
                " digits), which Proxmox checks before keeping the download: an image becomes a VM's disk."
            )
    return errors


def extraction_problem(vm, disk, config, check_mode, pending):
    """Why Proxmox could not extract an imported disk from its OVA, or None. A disk inside an OVA is first written to
    a working storage, the OVA's own unless the VM's import_working_storage names another (qm create's
    --import-working-storage), which must hold images and have a path (a directory or NFS storage): the installer's
    `local` holds no images unless they are enabled."""
    source = str(disk.get("import_from") or "")
    if ".ova/" not in source:
        return None
    working = vm.get("import_working_storage") or volume_storage(source)
    entry = {e["storage"]: e for e in config}.get(working)
    if entry is None:
        return None if check_mode and working in pending else f"{working}: does not exist (import_working_storage)"
    content = sorted(item for item in str(entry.get("content", "")).split(",") if item)
    if "images" in content and entry.get("path"):
        return None
    return (
        f"{working} cannot take the disk Proxmox extracts from {source}: it needs images in its content and a path (a "
        f"directory or NFS storage). Name such a storage in the VM's import_working_storage, or enable images on "
        f"it: pvesm set {working} --content {','.join([*content, 'images'])}"
    )


def storage_problems(storage, config, content, check_mode, pending, hint=""):
    """Why a storage cannot take what the role puts on it, or None. A storage pending creation by an earlier role
    passes a dry run; on a real run, the hint says what creates it."""
    have = {entry["storage"]: entry for entry in config}
    if storage not in have:
        if check_mode and storage in pending:
            return None
        return f"{storage}: does not exist" + (f" ({hint})" if storage in pending and hint else "")
    entry = have[storage]
    if int(entry.get("disable") or 0) == 1:
        return f"{storage}: is disabled"
    if content not in str(entry.get("content", "")).split(","):
        return f"{storage}: has no {content} in its content"
    return None


# --------------------------------------------------------------------------------------------- the plan ---


def disks_of(config):
    """The disk slots of an existing VM: {slot: (volume id, size in bytes)}."""
    out = {}
    for key, value in config.items():
        if DISK_SLOT.match(key):
            found = props(value, "volume")
            if found.get("media") == "cdrom":
                continue
            out[key] = (found["volume"], size_bytes(found.get("size", "0")))
    return out


def drift(vm, config):
    """How an existing VM differs: the qm set arguments, the disks to grow, and what is refused."""
    wanted = wanted_settings(vm)
    options, delete, resize, refused = [], [], [], []
    for key, value in wanted.items():
        if key != "boot" and normalised(key, config.get(key)) != normalised(key, value):
            options += [f"--{key}", value]
    for key in sorted(config):
        if (key.startswith("hostpci") or USB_SLOT.match(key)) and key not in wanted:
            delete.append(key)
    current = disks_of(config)
    wanted_slots = {disk["slot"] for disk in vm["disks"]}
    # A volume left unused, such as the disk a restore over the VM kept because the backup excluded it: a slot is
    # never filled with a new, empty disk while it may hold that slot's data.
    unused = {key: props(config[key], "volume")["volume"] for key in sorted(config) if re.fullmatch(r"unused\d+", key)}
    for disk in vm["disks"]:
        slot = disk["slot"]
        if slot not in current and disk.get("import_from"):
            # An imported disk is the VM's own system, imported once, when the role creates the VM: a fresh copy of
            # the image beside what the VM runs would be a second, empty system.
            refused.append(
                f"VM {vm['vmid']} has no {slot}, which the role imports only when it creates the VM: restore the VM "
                f"from its backup (scripts/runbooks/vm-restore.md), or attach its disk again by hand"
            )
            continue
        if slot not in current:
            if unused:
                found = ", ".join(f"{key} ({volume})" for key, volume in unused.items())
                refused.append(
                    f"VM {vm['vmid']} has no {slot} but keeps {found}, unused: a restore leaves the disks its backup "
                    f"excluded so. Attach the one {slot} held back (qm set {vm['vmid']} --{slot} <volume>), or remove "
                    f"it (qm disk unlink {vm['vmid']} --idlist <unusedN> --force) to get a new, empty one"
                )
                continue
            options += [f"--{slot}", disk_option(disk)]
            continue
        volume, size = current[slot]
        want = int(disk["size_gib"]) * GIB
        if volume_storage(volume) != disk["storage"]:
            refused.append(
                f"VM {vm['vmid']}'s {slot} is on {volume_storage(volume)}, configured on {disk['storage']}: moving it "
                f"is by hand (qm disk move {vm['vmid']} {slot} {disk['storage']} --delete 1)"
            )
            continue
        # Its serial and whether backups include it are the volume's options, set again on the same volume. Backups
        # include a disk unless it says backup=0.
        have = props(config[slot], "volume")
        settings = {"serial": disk.get("serial"), "backup": "0" if disk.get("backup") is False else None}
        current_settings = {"serial": have.get("serial"), "backup": "0" if have.get("backup") == "0" else None}
        if current_settings != settings:
            kept = {k: v for k, v in have.items() if k not in ("volume", "size", "format", *settings)}
            kept.update({k: v for k, v in settings.items() if v is not None})
            options += [f"--{slot}", ",".join([volume, *(f"{k}={v}" for k, v in kept.items())])]
        if size > want:
            refused.append(
                f"VM {vm['vmid']}'s {slot} is {gib(size)}, configured as {gib(want)}: a disk is never shrunk. Grow "
                f"the configuration, or move the data to a new disk by hand"
            )
        elif size < want:
            resize.append({"vmid": str(vm["vmid"]), "name": vm["name"], "slot": slot, "size": f"{disk['size_gib']}G"})
    for slot in sorted(set(current) - wanted_slots):
        refused.append(
            f"VM {vm['vmid']}'s {slot} ({current[slot][0]}) is not in its configuration: a disk is never detached "
            f"by the role. Add it back to the configuration, or detach it by hand (qm disk unlink)"
        )
    # The boot order last, after any disk it names that the same command adds.
    if "boot" in wanted and normalised("boot", config.get("boot")) != normalised("boot", wanted["boot"]):
        options += ["--boot", wanted["boot"]]
    if delete:
        options += ["--delete", ",".join(delete)]
    return options, resize, refused


def growth(vms, configs):
    """The disks of VMs the role has just created that are smaller than configured: an imported disk takes its
    image's size. `configs` maps each created VM's vmid to its configuration as read after the creation."""
    out = []
    for vm in vms:
        config = configs.get(str(vm["vmid"]))
        if config is None:
            continue
        current = disks_of(config)
        for disk in vm["disks"]:
            if disk["slot"] in current and current[disk["slot"]][1] < int(disk["size_gib"]) * GIB:
                out.append(
                    {"vmid": str(vm["vmid"]), "name": vm["name"], "slot": disk["slot"], "size": f"{disk['size_gib']}G"}
                )
    return out


def configured_mac(config):
    net = props(config.get("net0"), "_")
    for model in ("virtio", "e1000", "rtl8139", "vmxnet3"):
        if model in net:
            return net[model]
    return None


def plan(vms, facts):
    """Everything the role does on this run, from what it read. `facts`:

    check_mode, pending_storages, allow_overcommit, ignore_existing_volumes: the role's switches; pending_hint: what
    creates the pending storages, said when one is missing on a real run; rebuild_command: how to run the role
    again with ignore_existing_volumes, named when a missing VM is refused over its old volumes;
    storage_config: `pvesh get /storage`; qemu: `pvesh get /nodes/<node>/qemu`; lxc: `pvesh get /nodes/<node>/lxc`;
    node_storage: `pvesh get /nodes/<node>/storage`, which says whether each storage is active;
    configs and pending: per existing VM's vmid, its configuration and its pending changes;
    volumes: per missing VM's vmid, what the node's active storages list for its VMID (Proxmox adds files that
    belong to no VMID, such as imports and snippets, which are left out here);
    contents and totals: per budgeted storage, its volumes (every content type) and its size in bytes;
    host_headroom: bytes of each ZFS, directory or BTRFS storage's size kept out of the budget, for the host's
    own files;
    memory_total: the node's memory in bytes; isos: the volume ids the role downloads.
    """
    check_mode = bool(facts.get("check_mode"))
    pending = facts.get("pending_storages") or []
    config = facts.get("storage_config") or []
    qemu = {str(entry["vmid"]): entry for entry in facts.get("qemu") or []}
    errors, warnings = [], []
    result = {"create": [], "set": [], "resize": [], "start": [], "restart": {}, "macs": {}, "summary": {}}

    # Every storage a VM's disks go on must exist, be enabled and hold images.
    for storage in sorted(
        {disk["storage"] for vm in vms for disk in vm["disks"]}
        | {vm["efidisk_storage"] for vm in vms if vm.get("bios", "ovmf") == "ovmf"}
    ):
        problem = storage_problems(storage, config, "images", check_mode, pending, facts.get("pending_hint") or "")
        if problem:
            errors.append(f"{problem} (proxmox_vm_vms)")
    containers = {str(entry["vmid"]): entry for entry in facts.get("lxc") or []}
    # Storages that are enabled but not active (an unplugged USB disk, an unreachable NAS) cannot be searched for a
    # missing VM's old disks and backups, so they cannot be ruled out.
    inactive = sorted(
        entry["storage"]
        for entry in facts.get("node_storage") or []
        if int(entry.get("enabled", 1) or 0) == 1 and int(entry.get("active") or 0) != 1
    )
    rebuild = facts.get("rebuild_command") or "the play again with -e proxmox_vm_ignore_existing_volumes=true"
    for vm in vms:
        vmid = str(vm["vmid"])
        found = qemu.get(vmid)
        if found is None and vmid in containers:
            errors.append(
                f"VMID {vmid} belongs to the container {containers[vmid].get('name')!r}, not to a VM named "
                f"{vm['name']!r}: choose another vmid in proxmox_vm_vms, or move the container to another VMID"
            )
            continue
        if found is None:
            extraction = [extraction_problem(vm, disk, config, check_mode, pending) for disk in vm["disks"]]
            if any(extraction):
                errors += [problem for problem in extraction if problem]
                continue
            volumes = [v["volid"] for v in (facts.get("volumes") or {}).get(vmid, []) if str(v.get("vmid", "")) == vmid]
            if volumes and not facts.get("ignore_existing_volumes"):
                errors.append(
                    f"VM {vmid} ({vm['name']}) does not exist, but its VMID still has volumes: {', '.join(volumes)}. "
                    f"It is being rebuilt, not built: restore it from its backup (qmrestore <archive> {vmid}, "
                    f"scripts/runbooks/vm-restore.md). To build it new over them on purpose, run {rebuild}"
                )
                continue
            if inactive and not facts.get("ignore_existing_volumes"):
                errors.append(
                    f"VM {vmid} ({vm['name']}) does not exist, and {', '.join(inactive)} cannot be searched for its "
                    f"old disks and backups: enabled but not active (an unplugged disk, an unreachable NAS). Bring "
                    f"them back (pvesm status), or, to build it new without looking there, run {rebuild}"
                )
                continue
            result["create"].append({"vmid": vmid, "name": vm["name"], "argv": create_argv(vm)})
            result["summary"][vm["name"]] = "create"
            if vm.get("start", True):
                result["start"].append({"vmid": vmid, "name": vm["name"]})
            continue
        if found.get("name") != vm["name"]:
            errors.append(
                f"VMID {vmid} belongs to {found.get('name')!r}, not {vm['name']!r}: choose another vmid in "
                f"proxmox_vm_vms, or rename or remove that VM by hand"
            )
            continue
        current = (facts.get("configs") or {}).get(vmid, {})
        options, resize, refused = drift(vm, current)
        errors += refused
        changes = [option[2:] for option in options if option.startswith("--") and option != "--delete"]
        if "--delete" in options:
            changes += options[options.index("--delete") + 1].split(",")
        if options:
            result["set"].append({"vmid": vmid, "name": vm["name"], "argv": ["qm", "set", vmid, *options]})
        result["resize"] += resize
        changes += [f"grow {r['slot']} to {r['size']}" for r in resize]
        if found.get("status") != "running" and vm.get("start", True):
            result["start"].append({"vmid": vmid, "name": vm["name"]})
            changes.append("start")
        result["summary"][vm["name"]] = changes or "ok"
        mac, want = configured_mac(current), (vm.get("net") or {}).get("mac")
        result["macs"][vm["name"]] = mac
        if want and mac and mac.lower() != str(want).lower():
            warnings.append(
                f"VM {vmid} ({vm['name']}) has the MAC {mac}, configured as {want}. The role never changes it: fix "
                f"the DHCP reservation or kubernetes_node_macs, whichever is wrong, or the VM's net0 by hand"
            )
        waiting = sorted(
            entry["key"]
            for entry in (facts.get("pending") or {}).get(vmid, [])
            if "pending" in entry or entry.get("delete")
        )
        if waiting:
            result["restart"][vm["name"]] = waiting

    # The disk budget: on thin LVM, ZFS, a directory or BTRFS, the disks' provisioned sizes, other guests' included,
    # never add up to more than the storage's size. Counted from what is provisioned, never from what is free, which
    # shrinks as data is written. A container's volumes count too: Proxmox lists them as rootdir, not images. A thin
    # pool's size is its own, so on thin LVM the check gives the same answer on every run. A ZFS storage's size is its
    # dataset's use plus the pool's free space, and the pool also holds the host's own datasets (the installer's root
    # filesystem and local on rpool): it shrinks as they grow, so a plan that passed can be refused later, and disks
    # budgeted to the last byte would fill the host's root filesystem as they fill. A directory or BTRFS storage's size
    # is its filesystem's, which the host's own files share in the same way. host_headroom keeps room for them.
    types = {entry["storage"]: entry.get("type") for entry in config}
    additions = {}
    for vm in vms:
        vmid = str(vm["vmid"])
        creating = any(c["vmid"] == vmid for c in result["create"])
        current = disks_of((facts.get("configs") or {}).get(vmid, {})) if not creating else {}
        if creating and vm.get("bios", "ovmf") == "ovmf":
            additions[vm["efidisk_storage"]] = additions.get(vm["efidisk_storage"], 0) + EFI_DISK_BYTES
        for disk in vm["disks"]:
            want = int(disk["size_gib"]) * GIB
            have = current.get(disk["slot"], (None, 0))[1] if not creating else 0
            if want > have:
                additions[disk["storage"]] = additions.get(disk["storage"], 0) + want - have
    for storage, added in sorted(additions.items()):
        if types.get(storage) not in BUDGETED_TYPES or storage not in (facts.get("totals") or {}):
            continue
        provisioned = sum(
            int(v.get("size") or 0)
            for v in (facts.get("contents") or {}).get(storage, [])
            if v.get("content") in GUEST_CONTENT
        )
        total = int(facts["totals"][storage])
        headroom = int(facts.get("host_headroom") or 0) if types[storage] in HOST_SHARED_TYPES else 0
        if provisioned + added > total - headroom and not facts.get("allow_overcommit"):
            kept = f" less the {gib(headroom)} kept for the host's own files (proxmox_vm_host_headroom_gib)"
            errors.append(
                f"{storage}: its disks would be provisioned at {gib(provisioned + added)} ({gib(provisioned)} there "
                f"now, {gib(added)} more for proxmox_vm_vms), more than its {gib(total)}{kept if headroom else ''}. "
                f"A full storage gives every VM on it I/O errors: make the disks smaller, free space, or set "
                f"proxmox_vm_allow_overcommit in local.yml"
            )

    # Memory: a warning, never a refusal.
    total = int(facts.get("memory_total") or 0)
    running = [entry for entry in qemu.values() if entry.get("status") == "running"]
    starting = [vm for vm in vms if any(s["vmid"] == str(vm["vmid"]) for s in result["start"])]
    demand = sum(int(entry.get("maxmem") or 0) for entry in running) + sum(
        int(vm["memory_mib"]) * MIB for vm in starting
    )
    if total and starting and demand > total - NODE_MEMORY_RESERVE:
        names = ", ".join(f"{e.get('name')} ({e['vmid']}, {gib(int(e.get('maxmem') or 0))})" for e in running)
        warnings.append(
            f"The VMs would need {gib(demand)} of memory, with {gib(total)} on the node and 2 GiB kept for it. "
            f"Running now: {names or 'none'}. Stop some, or give the cluster's VMs less memory"
        )

    result["errors"] = errors
    result["warnings"] = warnings
    targeted = {disk["storage"] for vm in vms for disk in vm["disks"]}
    absent = set(pending) - {entry["storage"] for entry in config}
    result["pending_storages"] = sorted(targeted & absent) if check_mode else []
    return result


def unused_isos(volumes, isos, attached):
    """The ISOs and import images in the role's storages named like one of its own (the same first word, such as
    talos- or haos_ova-) that neither the role's lists nor any VM's CD-ROM uses: old versions, for the owner to remove
    by hand. `isos` holds both lists' entries, each with its `content` (iso or import); `attached` is every VM's
    CD-ROMs on the node, not only the role's. An image is used once, when its VM is created, so the pinned one is
    kept for a rebuild."""
    families = {(iso["storage"], iso["filename"].split("-", 1)[0] + "-") for iso in isos}
    wanted = {f"{iso['storage']}:{iso.get('content', 'iso')}/{iso['filename']}" for iso in isos}
    wanted |= {props(cdrom, "volume").get("volume") for cdrom in attached}
    out = []
    for volume in volumes:
        storage, _, path = str(volume.get("volid", "")).partition(":")
        name = path.removeprefix("iso/").removeprefix("import/")
        if any(storage == s and name.startswith(f) for s, f in families) and volume["volid"] not in wanted:
            out.append(volume["volid"])
    return sorted(out)


class FilterModule:
    def filters(self):
        return {
            "proxmox_vm_input_errors": input_errors,
            "proxmox_vm_plan": plan,
            "proxmox_vm_unused_isos": unused_isos,
            "proxmox_vm_growth": growth,
            "proxmox_vm_storage_problem": storage_problems,
        }
