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
# Storage types whose space is shared by every disk on them, and so counted against their size (thin LVM and
# ZFS). A file or NFS storage is left to whatever caps it (the NAS's quotas).
BUDGETED_TYPES = ("lvmthin", "zfspool")
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
    return props(value, "_")


def disk_option(disk):
    """A disk as `qm create` or `qm set` takes it: a new volume of the given size on its storage."""
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
    settings = wanted_settings(vm)
    for key in ("watchdog", "startup", "tags", "ide2"):
        if key in settings:
            argv += [f"--{key}", settings[key]]
    argv += ["--onboot", settings["onboot"]]
    if vm.get("boot"):
        argv += ["--boot", vm["boot"]]
    for index in range(len(vm.get("hostpci") or [])):
        argv += [f"--hostpci{index}", settings[f"hostpci{index}"]]
    return argv


# ------------------------------------------------------------------------------------------ the checks ---


def input_errors(vms, isos):
    """Problems with proxmox_vm_vms and proxmox_vm_isos, one sentence each, naming the variable."""
    errors = []
    if isinstance(vms, (str, dict)) or isinstance(isos, (str, dict)):
        return ["proxmox_vm_vms and proxmox_vm_isos must be lists."]
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
        if vm.get("bios", "ovmf") == "ovmf" and not vm.get("efidisk_storage"):
            errors.append(f"{label}: bios ovmf needs efidisk_storage.")
        mac = (vm.get("net") or {}).get("mac")
        if mac and not MAC.match(str(mac)):
            errors.append(f"{label}: net.mac {mac} is not a MAC address.")
    for iso in isos:
        if not iso.get("storage") or not iso.get("filename") or not iso.get("url"):
            errors.append(f"Each ISO in proxmox_vm_isos needs storage, filename and url: {iso}.")
    return errors


def storage_problems(storage, config, content, check_mode, pending):
    """Why a storage cannot take what the role puts on it, or None. A storage pending creation by an earlier role
    passes a dry run."""
    have = {entry["storage"]: entry for entry in config}
    if storage not in have:
        if check_mode and storage in pending:
            return None
        return f"{storage}: does not exist"
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
        if key.startswith("hostpci") and key not in wanted:
            delete.append(key)
    current = disks_of(config)
    wanted_slots = {disk["slot"] for disk in vm["disks"]}
    for disk in vm["disks"]:
        slot = disk["slot"]
        if slot not in current:
            options += [f"--{slot}", disk_option(disk)]
            continue
        volume, size = current[slot]
        want = int(disk["size_gib"]) * GIB
        if volume_storage(volume) != disk["storage"]:
            refused.append(
                f"VM {vm['vmid']}'s {slot} is on {volume_storage(volume)}, configured on {disk['storage']}: moving it "
                f"is by hand (qm disk move {vm['vmid']} {slot} {disk['storage']} --delete 1)"
            )
        elif size > want:
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


def configured_mac(config):
    net = props(config.get("net0"), "_")
    for model in ("virtio", "e1000", "rtl8139", "vmxnet3"):
        if model in net:
            return net[model]
    return None


def plan(vms, facts):
    """Everything the role does on this run, from what it read. `facts`:

    check_mode, pending_storages, allow_overcommit, ignore_existing_volumes: the role's switches;
    storage_config: `pvesh get /storage`; qemu: `pvesh get /nodes/<node>/qemu`; lxc: `pvesh get /nodes/<node>/lxc`;
    node_storage: `pvesh get /nodes/<node>/storage`, which says whether each storage is active;
    configs and pending: per existing VM's vmid, its configuration and its pending changes;
    volumes: per missing VM's vmid, what the node's active storages list for its VMID (Proxmox adds files that
    belong to no VMID, such as imports and snippets, which are left out here);
    contents and totals: per budgeted storage, its volumes (every content type) and its size in bytes;
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
        problem = storage_problems(storage, config, "images", check_mode, pending)
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
    rebuild = "make kubernetes ANSIBLE_ARGS='-e proxmox_vm_ignore_existing_volumes=true'"
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

    # The disk budget: on thin LVM or ZFS, the disks' provisioned sizes, other guests' included, never add up to more
    # than the storage's size. Counted from what is provisioned, never from what is free, which shrinks as data is
    # written, so the check gives the same answer on every run. A container's volumes count too: Proxmox lists them
    # as rootdir, not images.
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
        if provisioned + added > total and not facts.get("allow_overcommit"):
            errors.append(
                f"{storage}: its disks would be provisioned at {gib(provisioned + added)} ({gib(provisioned)} there "
                f"now, {gib(added)} more for proxmox_vm_vms), more than its {gib(total)}. A full thin pool gives every "
                f"VM on it I/O errors: make the disks smaller, free space, or set proxmox_vm_allow_overcommit in "
                f"local.yml"
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
    """The ISOs in the role's ISO storages named like one of its own (the same first word, such as talos-) that
    neither proxmox_vm_isos nor any VM's CD-ROM uses: old versions, for the owner to remove by hand. `attached`
    is every VM's CD-ROMs on the node, not only the role's."""
    families = {(iso["storage"], iso["filename"].split("-", 1)[0] + "-") for iso in isos}
    wanted = {f"{iso['storage']}:iso/{iso['filename']}" for iso in isos}
    wanted |= {props(cdrom, "volume").get("volume") for cdrom in attached}
    out = []
    for volume in volumes:
        storage, _, path = str(volume.get("volid", "")).partition(":")
        name = path.removeprefix("iso/")
        if any(storage == s and name.startswith(f) for s, f in families) and volume["volid"] not in wanted:
            out.append(volume["volid"])
    return sorted(out)


class FilterModule:
    def filters(self):
        return {
            "proxmox_vm_input_errors": input_errors,
            "proxmox_vm_plan": plan,
            "proxmox_vm_unused_isos": unused_isos,
            "proxmox_vm_storage_problem": storage_problems,
        }
