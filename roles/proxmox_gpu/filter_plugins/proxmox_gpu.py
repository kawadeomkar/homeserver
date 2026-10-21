"""What roles/proxmox_gpu does, worked out from what it read on the Proxmox host.

The role's tasks read the host's PCI devices (`pvesh get /nodes/<node>/hardware/pci`), a few of their sysfs
attributes (the console's device, the driver holding each, its modalias, the DRM nodes), the guests' configurations
and the PCI resource mappings, pass them here, and act on what comes back. Everything here is pure: it runs on the
controller, never connects anywhere, and is unit-tested in tests/unit/test_proxmox_gpu.py.

How Proxmox reads these, from its own code (pve-guest-common's PVE/Mapping/PCI.pm, qemu-server's PCI handling):
- a mapping's entry for a node names the device by `path` (a function, 0000:01:00.0, or a whole slot, 0000:01:00,
  for every function of a card) and must carry the `id` (vendor:device), `iommugroup` and, when the device has one,
  `subsystem-id` of that function, or of function 0 for a slot; a VM using it refuses to start otherwise ("PCI device
  mapping invalid (hardware probably changed)"). Values compare with every 0x dropped, case as it is, so they are
  written without it, in lower case.
- starting a VM binds vfio-pci to every device in the passed device's IOMMU group but bridges, taking any of them
  from the host: a GPU is passed only when its group holds nothing but the card's own devices and PCI bridges.
"""

import re

# Display controllers: VGA (0x0300), XGA (0x0301), 3D (0x0302) and other (0x0380), as `class` gives them (0x030000).
DISPLAY_CLASS = re.compile(r"^(0x)?03")
# PCI-to-PCI bridges, which the kernel groups with the devices behind them and vfio leaves alone.
BRIDGE_CLASS = re.compile(r"^(0x)?0604")
# Multimedia devices (0x04: audio, 0x0403 HDMI audio): a card's own, when its vendor's and in its IOMMU group. An Arc
# card puts its audio on a bus of its own, behind the card's own PCIe switch, not beside the GPU in its slot.
MULTIMEDIA_CLASS = re.compile(r"^(0x)?04")
# A PCI address as lspci and Proxmox write it, the domain and the function optional: 0000:01:00.0, 01:00.0, 01:00.
ADDRESS = re.compile(r"^(?:([0-9a-f]{4}):)?([0-9a-f]{2}):([0-9a-f]{2})(?:\.([0-7]))?$", re.IGNORECASE)
# A mapping id as Proxmox takes one (pve-configid).
MAPPING_ID = re.compile(r"^[a-z][a-z0-9_-]+$", re.IGNORECASE)
VENDOR = re.compile(r"^[0-9a-f]{4}$")
# The fields of a mapping's entry this role writes and compares; Proxmox may keep others (a description).
ENTRY_KEYS = ("node", "path", "id", "subsystem-id", "iommugroup")
# A DRM device node, as sysfs's class/drm names it and a container's configuration passes it.
DRM_NODE = re.compile(r"^(card|renderD)\d+$")
# DRM's character-device major: a container allowed c 226:<minor> reaches card<minor>, or renderD<minor> from 128.
DRM_MAJOR = "226"


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
    if not isinstance(select, (str, type(None))):
        out.append(
            f'{select_name} must be auto, none, or a PCI address in quotes, such as "0000:01:00.0": YAML reads an'
            f" address of digits written without them as a number, and no or false as a boolean, not {select!r}."
        )
    elif text not in ("", "none", "auto") and parse_address(text) is None:
        out.append(
            f"{select_name} must be auto, none, or a PCI address as lspci or Proxmox's PCI list shows it, such as "
            f'"0000:01:00.0" or "01:00.0", in quotes, not {select!r}.'
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
    """What the role's sysfs script printed: whether the host has an IOMMU; per device its boot_vga flag, the driver
    holding it and its modalias ("-" for none); and each DRM node's PCI device."""
    iommu, devices, drm = False, {}, {}
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
        elif fields[0] == "drm" and len(fields) == 3 and DRM_NODE.fullmatch(fields[1]):
            drm[fields[1]] = fields[2].strip().lower()
    return iommu, devices, drm


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
    """A display device as the role reports and passes it, with what keeps it from being passed alone. `functions`
    are its slot's, which the VM gets; `held` adds the card's own devices elsewhere in its IOMMU group (an Arc card's
    audio), which vfio-pci holds too, since a VM's start takes the whole group from the host."""
    address = device["id"].lower()
    slot = slot_of(address)
    vendor = hexid(device["vendor"])
    functions = sorted(d["id"].lower() for d in devices if slot_of(d["id"]) == slot)
    group = device.get("iommugroup")
    group = int(group) if str(group).lstrip("-").isdigit() else -1
    others = [
        d
        for d in devices
        if group >= 0
        and str(d.get("iommugroup")) == str(group)
        and slot_of(d["id"]) != slot
        and not BRIDGE_CLASS.match(d["class"])
    ]
    companions = sorted(
        d["id"].lower() for d in others if hexid(d["vendor"]) == vendor and MULTIMEDIA_CLASS.match(d["class"])
    )
    members = sorted(d["id"].lower() for d in others if d["id"].lower() not in companions)
    held = functions + companions
    # A slot's entry names function 0's ids, which Proxmox checks for the whole card.
    first = next((d for d in devices if d["id"].lower() == f"{slot}.0"), device)
    problems = []
    if not iommu or group < 0:
        problems.append(
            "the host has no IOMMU on: turn on VT-d (Intel) or AMD-Vi (AMD) in its firmware, every VM down first"
        )
    elif members:
        problems.append(
            f"it shares IOMMU group {group} with {', '.join(members)}, which a VM would take from the host too"
        )
    if vendors and vendor not in [hexid(v) for v in vendors]:
        problems.append(f"its vendor ({device.get('vendor_name') or vendor}) is not supported here yet")
    facts = sysfs.get(address, {})
    return {
        "address": address,
        "slot": slot,
        "path": slot if len(functions) > 1 else address,
        "functions": functions,
        "held": held,
        "vendor": vendor,
        "device": hexid(device["device"]),
        "name": " ".join(filter(None, [device.get("vendor_name"), device.get("device_name")])) or address,
        "entry_id": f"{hexid(first['vendor'])}:{hexid(first['device'])}",
        "subsystem": subsystem_of(first),
        "vfio_ids": list(dict.fromkeys(ids_of(d) for d in devices if d["id"].lower() in held)),
        "iommugroup": group,
        "boot_vga": facts.get("boot_vga", False),
        "host_drivers": {f: sysfs.get(f, {}).get("driver", "") for f in held},
        "modaliases": {f: sysfs.get(f, {}).get("modalias", "") for f in held},
        "problems": problems,
    }


def props(text, default_key):
    """A Proxmox property string as a dict, a first item without "=" the value of `default_key`."""
    out = {}
    for index, item in enumerate(str(text).split(",")):
        key, sep, value = item.partition("=")
        if sep:
            out[key.strip()] = value.strip()
        elif index == 0 and key.strip():
            out[default_key] = key.strip()
    return out


def guest_lines(text):
    """(kind, id, key, value) for each line of the role's read of the guests' configurations, as
    "qemu-server/105.conf:hostpci0: 0000:03:00.0,pcie=1" or "lxc/200.conf:dev0: /dev/dri/renderD128,gid=44"."""
    out = []
    for line in str(text or "").splitlines():
        found = re.fullmatch(r"(qemu-server|lxc)/(\d+)\.conf:([^:\s]+):\s*(.*)", line.strip())
        if found:
            kind, guest, key, value = found.groups()
            out.append(("VM" if kind == "qemu-server" else "container", int(guest), key, value))
    return out


def users(gpu, guests_text, drm, mappings, own_mapping, node, allowed):
    """The other guests that use the GPU, as "VM 105 (hostpci0)": a VM given it by address, or through a mapping other
    than the role's own that names it on this node, and a container given its DRM nodes, by name, by number (c 226:N)
    or all of /dev/dri. The guests in `allowed`, those the GPU is meant for, never count."""
    held = set(gpu["held"])
    slots = {slot_of(a) for a in held}

    def names_it(address):
        parsed = parse_address(address)
        if parsed is None:
            return False
        slot, function = parsed
        return slot in slots if function is None else f"{slot}.{function}" in held

    out = []
    allowed = {int(g) for g in allowed or []}
    for kind, guest, key, value in guest_lines(guests_text):
        if guest in allowed:
            continue
        hit = False
        if kind == "VM" and re.fullmatch(r"hostpci\d+", key):
            given = props(value, "host")
            if given.get("mapping"):
                if given["mapping"] == own_mapping:
                    continue
                paths = [
                    entry_props(e).get("path", "")
                    for m in mappings or []
                    if m.get("id") == given["mapping"]
                    for e in ([m.get("map")] if isinstance(m.get("map"), str) else m.get("map") or [])
                    if entry_props(e).get("node") == node
                ]
            else:
                paths = [given.get("host", "")]
            hit = any(names_it(a) for path in paths for a in path.split(";"))
        elif kind == "container":
            if re.fullmatch(r"dev\d+", key):
                path = value.split(",", 1)[0].strip()
            elif key == "lxc.mount.entry":
                path = (value.split() or [""])[0]
            elif key in ("lxc.cgroup.devices.allow", "lxc.cgroup2.devices.allow"):
                found = re.fullmatch(rf"c\s+{DRM_MAJOR}:(\d+|\*)\s*\S*", value.strip())
                if not found:
                    continue
                minor = found.group(1)
                path = "/dev/dri" if minor == "*" else f"/dev/dri/{'renderD' if int(minor) >= 128 else 'card'}{minor}"
            else:
                continue
            path = path.rstrip("/")
            if path == "/dev/dri" or path == "/dev/dri/by-path":
                hit = True
            elif path.startswith("/dev/dri/"):
                name = path.rsplit("/", 1)[1]
                by_path = re.search(r"pci-([0-9a-f:.]+)-", name)
                hit = names_it(by_path.group(1)) if by_path else drm.get(name, "") in held
        if hit:
            out.append(f"{kind} {guest} ({key})")
    return sorted(dict.fromkeys(out))


def choose(devices, sysfs_text, select, vendors=None, select_name="proxmox_gpu_select", guests=None):
    """The GPU to pass, from the host's PCI devices and the sysfs script's output.

    `select` is auto, none (or empty), or an address. auto takes the one GPU that can be passed alone, of a vendor in
    `vendors` (any when empty), that is not the host's console (boot_vga: passing it blanks the host's own screen)
    and that no other guest uses; none found means no GPU, more than one is refused. An address takes that GPU; one
    no longer in the host means no GPU (the card was taken out, and the VM must still start), while one that is not a
    GPU, cannot be passed alone, or that another guest uses, is refused. `guests` is what users() needs: the guests'
    configuration lines (`text`), the PCI mappings, the role's own mapping, the node and the guests the GPU is meant
    for. Returns {gpu: {...} or {}, notes: [...], error: "..."}."""
    text = str(select if select is not None else "").strip().lower()
    out = {"gpu": {}, "notes": [], "error": ""}
    if text in ("", "none"):
        return out
    iommu, sysfs, drm = parse_sysfs(sysfs_text)
    guests = guests or {}
    devices = [d for d in devices or [] if d.get("id") and d.get("class")]
    gpus = [describe(d, devices, sysfs, iommu, vendors or []) for d in devices if DISPLAY_CLASS.match(d["class"])]
    for g in gpus:
        using = users(
            g,
            guests.get("text", ""),
            drm,
            guests.get("mappings", []),
            guests.get("mapping", ""),
            guests.get("node", ""),
            guests.get("allowed", []),
        )
        if using:
            g["problems"].append(
                f"{', '.join(using)} {'uses' if len(using) == 1 else 'use'} it: take it from there first"
            )
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
                    f'it is the host\'s console, which auto leaves alone: set {select_name} to "{g["address"]}" to'
                    " pass it anyway, and the host's own screen goes dark"
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
    """The modprobe.d file that keeps the host's drivers off the GPU: vfio-pci claims each device it holds (its
    functions and the card's own devices beside them) by id, and loads before every driver that would claim one
    (softdep). `drivers` maps each device to the modules `modprobe --resolve-alias` gives for its modalias. Empty when
    there is no GPU, and the file goes."""
    if not gpu:
        return ""
    modules = set()
    for function in gpu["held"]:
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
