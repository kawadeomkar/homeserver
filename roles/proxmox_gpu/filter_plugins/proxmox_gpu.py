"""What roles/proxmox_gpu does, worked out from what it read on the Proxmox host.

The role's tasks read the host's PCI devices (`pvesh get /nodes/<node>/hardware/pci`), a few of their sysfs
attributes (the console's device, the driver holding each, its modalias) and the PCI resource mappings, pass them
here, and act on what comes back. Everything here is pure: it runs on the controller, never connects anywhere, and
is unit-tested in tests/unit/test_proxmox_gpu.py.

How Proxmox reads these, from its own code (pve-guest-common's PVE/Mapping/PCI.pm, qemu-server's PCI handling):
- a mapping's entry for a node names the device by `path` (a function, 0000:01:00.0, or a whole slot, 0000:01:00,
  for every function of a card) and must carry the `id` (vendor:device), `iommugroup` and, when the device has one,
  `subsystem-id` of that function, or of function 0 for a slot; a VM using it refuses to start otherwise ("PCI device
  mapping invalid (hardware probably changed)"). Values compare without their 0x, so they are written without it.
- starting a VM binds vfio-pci to every device in the passed device's IOMMU group but bridges, taking any of them
  from the host: a GPU is passed only when its group holds nothing but its own functions and PCI bridges.
"""

import re

# Display controllers: VGA (0x0300), XGA (0x0301), 3D (0x0302) and other (0x0380), as `class` gives them (0x030000).
DISPLAY_CLASS = re.compile(r"^(0x)?03")
# PCI-to-PCI bridges, which the kernel groups with the devices behind them and vfio leaves alone.
BRIDGE_CLASS = re.compile(r"^(0x)?0604")
# A PCI address as lspci and Proxmox write it, the domain and the function optional: 0000:01:00.0, 01:00.0, 01:00.
ADDRESS = re.compile(r"^(?:([0-9a-f]{4}):)?([0-9a-f]{2}):([0-9a-f]{2})(?:\.([0-7]))?$", re.IGNORECASE)
# A mapping id as Proxmox takes one (pve-configid).
MAPPING_ID = re.compile(r"^[a-z][a-z0-9_-]+$", re.IGNORECASE)
VENDOR = re.compile(r"^[0-9a-f]{4}$")
# The fields of a mapping's entry this role writes and compares; Proxmox may keep others (a description).
ENTRY_KEYS = ("node", "path", "id", "subsystem-id", "iommugroup")


def hexid(value):
    """A PCI id as Proxmox's mapping compares it: lower case, without 0x. Empty for none."""
    text = str(value if value is not None else "").strip().lower()
    return text[2:] if text.startswith("0x") else text


def parse_address(text):
    """(slot, function) of an address, the domain filled in: ("0000:01:00", "0") or ("0000:01:00", None)."""
    found = ADDRESS.fullmatch(str(text).strip())
    if not found:
        return None
    domain, bus, device, function = found.groups()
    return f"{(domain or '0000').lower()}:{bus.lower()}:{device.lower()}", function


def select_errors(select, vendors, mapping, select_name="proxmox_gpu_select"):
    """The role's inputs, one message each."""
    out = []
    text = str(select if select is not None else "").strip().lower()
    if text not in ("", "none", "auto") and parse_address(text) is None:
        out.append(
            f"{select_name} must be auto, none, or a PCI address as lspci or Proxmox's PCI list shows it, such as "
            f"0000:01:00.0 or 01:00.0, not {select!r}."
        )
    if isinstance(vendors, str) or not isinstance(vendors, (list, tuple)):
        out.append('proxmox_gpu_vendors must be a list of PCI vendor ids, such as ["8086"], not a string.')
    elif any(not VENDOR.fullmatch(hexid(v)) for v in vendors):
        out.append(f"proxmox_gpu_vendors must hold PCI vendor ids of four hex digits, such as 8086, not {vendors}.")
    if text not in ("", "none") and not MAPPING_ID.fullmatch(str(mapping or "")):
        out.append(
            "proxmox_gpu_mapping must name the PCI resource mapping the GPU is kept under (letters, digits, - and _,"
            f" starting with a letter), not {mapping!r}: a VM is given the GPU through it."
        )
    return out


def parse_sysfs(text):
    """What the role's sysfs script printed: whether the host has an IOMMU, and per device its boot_vga flag, the
    driver holding it and its modalias ("-" for none)."""
    iommu, devices = False, {}
    for line in str(text or "").splitlines():
        fields = line.split("\t")
        if fields[0] == "iommu" and len(fields) == 2:
            iommu = fields[1].strip() not in ("", "0")
        elif fields[0] == "device" and len(fields) == 5:
            _, address, boot_vga, driver, modalias = fields
            devices[address.lower()] = {
                "boot_vga": boot_vga.strip() == "1",
                "driver": "" if driver.strip() == "-" else driver.strip(),
                "modalias": "" if modalias.strip() == "-" else modalias.strip(),
            }
    return iommu, devices


def slot_of(address):
    return address.lower().rsplit(".", 1)[0]


def subsystem_of(device):
    """A device's subsystem as Proxmox's mapping expects it: present whenever the host lists one, whatever its value,
    since Proxmox refuses an entry without it for a device that has one."""
    if device.get("subsystem_vendor") is None or device.get("subsystem_device") is None:
        return ""
    return f"{hexid(device['subsystem_vendor'])}:{hexid(device['subsystem_device'])}"


def ids_of(device):
    """A function as vfio-pci's ids option takes it: vendor:device, then its subsystem when it has one."""
    sub = subsystem_of(device)
    return f"{hexid(device['vendor'])}:{hexid(device['device'])}" + (f":{sub}" if sub else "")


def describe(device, devices, sysfs, iommu, vendors):
    """A display device as the role reports and passes it, with what keeps it from being passed alone."""
    address = device["id"].lower()
    slot = slot_of(address)
    functions = sorted(d["id"].lower() for d in devices if slot_of(d["id"]) == slot)
    group = device.get("iommugroup")
    group = int(group) if str(group).lstrip("-").isdigit() else -1
    members = [
        d["id"].lower()
        for d in devices
        if str(d.get("iommugroup")) == str(group) and slot_of(d["id"]) != slot and not BRIDGE_CLASS.match(d["class"])
    ]
    # A slot's entry names function 0's ids, which Proxmox checks for the whole card.
    first = next((d for d in devices if d["id"].lower() == f"{slot}.0"), device)
    problems = []
    if not iommu or group < 0:
        problems.append(
            "the host has no IOMMU on: turn on VT-d (Intel) or AMD-Vi (AMD) in its firmware, every VM down first"
        )
    elif members:
        problems.append(
            f"it shares IOMMU group {group} with {', '.join(sorted(members))}, which a VM would take from the host too"
        )
    if vendors and hexid(device["vendor"]) not in [hexid(v) for v in vendors]:
        problems.append(
            f"its vendor ({device.get('vendor_name') or hexid(device['vendor'])}) is not supported here yet"
        )
    facts = sysfs.get(address, {})
    return {
        "address": address,
        "slot": slot,
        "path": slot if len(functions) > 1 else address,
        "functions": functions,
        "vendor": hexid(device["vendor"]),
        "device": hexid(device["device"]),
        "name": " ".join(filter(None, [device.get("vendor_name"), device.get("device_name")])) or address,
        "entry_id": f"{hexid(first['vendor'])}:{hexid(first['device'])}",
        "subsystem": subsystem_of(first),
        "vfio_ids": list(dict.fromkeys(ids_of(d) for d in devices if d["id"].lower() in functions)),
        "iommugroup": group,
        "boot_vga": facts.get("boot_vga", False),
        "host_drivers": {f: sysfs.get(f, {}).get("driver", "") for f in functions},
        "modaliases": {f: sysfs.get(f, {}).get("modalias", "") for f in functions},
        "problems": problems,
    }


def choose(devices, sysfs_text, select, vendors=None, select_name="proxmox_gpu_select"):
    """The GPU to pass, from the host's PCI devices and the sysfs script's output.

    `select` is auto, none (or empty), or an address. auto takes the one GPU that can be passed alone, of a vendor in
    `vendors` (any when empty), that is not the host's console (boot_vga: passing it blanks the host's own screen);
    none found means no GPU, more than one is refused. An address takes that GPU; one no longer in the host means no
    GPU (the card was taken out, and the VM must still start), while one that is not a GPU, or cannot be passed, is
    refused. Returns {gpu: {...} or {}, notes: [...], error: "..."}."""
    text = str(select if select is not None else "").strip().lower()
    out = {"gpu": {}, "notes": [], "error": ""}
    if text in ("", "none"):
        return out
    iommu, sysfs = parse_sysfs(sysfs_text)
    devices = [d for d in devices or [] if d.get("id") and d.get("class")]
    gpus = [describe(d, devices, sysfs, iommu, vendors or []) for d in devices if DISPLAY_CLASS.match(d["class"])]
    if text == "auto":
        usable = [g for g in gpus if not g["problems"] and not g["boot_vga"]]
        if len(usable) > 1:
            out["error"] = (
                "Several GPUs could be passed: "
                + "; ".join(f"{g['name']} at {g['address']}" for g in usable)
                + f". Name the one to pass in {select_name}."
            )
            return out
        if usable:
            out["gpu"] = usable[0]
        for g in gpus:
            if g is out["gpu"]:
                continue
            if g["problems"]:
                why = "; ".join(g["problems"])
            else:
                why = (
                    f"it is the host's console, which auto leaves alone: set {select_name} to {g['address']} to pass"
                    " it anyway, and the host's own screen goes dark"
                )
            out["notes"].append(f"{g['name']} at {g['address']} is not passed: {why}.")
        if not gpus:
            out["notes"].append("The host has no GPU.")
        return out
    slot, function = parse_address(text)
    at_slot = [
        d
        for d in devices
        if slot_of(d["id"]) == slot and (function is None or d["id"].lower().endswith(f".{function}"))
    ]
    if not at_slot:
        out["notes"].append(
            f"{select_name} names {select}, and the host has no device there: the VM gets no GPU until it is back."
        )
        return out
    named = [g for g in gpus if g["slot"] == slot and (function is None or g["address"].endswith(f".{function}"))]
    if not named:
        out["error"] = (
            f"{select_name} names {select}, which is not a GPU: {at_slot[0].get('device_name') or at_slot[0]['id']}."
        )
        return out
    gpu = named[0]
    if gpu["problems"]:
        out["error"] = (
            f"{gpu['name']} at {gpu['address']}, which {select_name} names, cannot be passed: "
            + "; ".join(gpu["problems"])
            + "."
        )
        return out
    out["gpu"] = gpu
    if gpu["boot_vga"]:
        out["notes"].append(
            f"{gpu['name']} at {gpu['address']} is the host's console: once vfio-pci takes it, the host's own screen"
            " goes dark, and the network is the only way in."
        )
    return out


def entry_text(entry):
    return ",".join(f"{k}={entry[k]}" for k in ENTRY_KEYS if entry.get(k) not in (None, ""))


def entry_props(text):
    out = {}
    for item in str(text).split(","):
        key, sep, value = item.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def mapping_plan(mappings, mapping_id, node, gpu, description=""):
    """What to do about the PCI resource mapping that holds `gpu` for `node`: create it, set it (this node's entry
    replaced, other nodes' kept as they are), or nothing. `mappings` is `pvesh get /cluster/mapping/pci`'s list.
    Returns {action: create, set, ok or none, argv: [...], entry: "...", checks: [Proxmox's errors for it]}."""
    if not gpu or not mapping_id:
        return {"action": "none", "argv": [], "entry": "", "checks": []}
    wanted = {
        "node": node,
        "path": gpu["path"],
        "id": gpu["entry_id"],
        "subsystem-id": gpu["subsystem"],
        "iommugroup": str(gpu["iommugroup"]),
    }
    text = entry_text(wanted)
    current = next((m for m in mappings or [] if m.get("id") == mapping_id), None)
    if current is None:
        argv = ["pvesh", "create", "/cluster/mapping/pci", "--id", mapping_id, "--map", text]
        return {
            "action": "create",
            "argv": argv + (["--description", description] if description else []),
            "entry": text,
            "checks": [],
        }
    entries = current.get("map") or []
    entries = [entries] if isinstance(entries, str) else list(entries)
    ours = [entry_props(e) for e in entries if entry_props(e).get("node") == node]
    # As Proxmox compares them when a VM starts: every 0x dropped, case kept, so an upper-case id is corrected.
    same = len(ours) == 1 and all(
        str(ours[0].get(k, "")).replace("0x", "") == wanted[k] for k in ENTRY_KEYS if k != "node"
    )
    checks = [c.get("message", "") for c in current.get("checks") or [] if c.get("severity") == "error"]
    if same:
        return {"action": "ok", "argv": [], "entry": text, "checks": checks}
    others = [e for e in entries if entry_props(e).get("node") != node]
    argv = ["pvesh", "set", f"/cluster/mapping/pci/{mapping_id}"]
    for e in [*others, text]:
        argv += ["--map", e]
    if current.get("digest"):
        argv += ["--digest", current["digest"]]
    return {"action": "set", "argv": argv, "entry": text, "checks": checks}


def modprobe_conf(gpu, drivers, header=""):
    """The modprobe.d file that keeps the host's drivers off the GPU: vfio-pci claims each of its functions by id, and
    loads before every driver that would claim one (softdep). `drivers` maps each function to the modules
    `modprobe --resolve-alias` gives for its modalias. Empty when there is no GPU, and the file goes."""
    if not gpu:
        return ""
    modules = set()
    for function in gpu["functions"]:
        modules.update(m for m in (drivers or {}).get(function, []) if m and m not in ("vfio_pci", "vfio-pci"))
    lines = [f"# {line}" if line else "#" for line in str(header).splitlines()]
    lines.append(f"options vfio-pci ids={','.join(gpu['vfio_ids'])}")
    lines += [f"softdep {m} pre: vfio-pci" for m in sorted(modules)]
    return "\n".join(lines) + "\n"


def held_by_host(gpu):
    """The functions a host driver holds, as "function (driver)": vfio-pci, or no driver, is what passing needs."""
    return [
        f"{f} ({d})" for f, d in sorted((gpu or {}).get("host_drivers", {}).items()) if d and d not in ("vfio-pci",)
    ]


class FilterModule:
    def filters(self):
        return {
            "proxmox_gpu_select_errors": select_errors,
            "proxmox_gpu_choose": choose,
            "proxmox_gpu_mapping_plan": mapping_plan,
            "proxmox_gpu_modprobe_conf": modprobe_conf,
            "proxmox_gpu_held_by_host": held_by_host,
        }
