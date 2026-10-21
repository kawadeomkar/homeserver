"""Every setting make init asks for: group_vars/all/local.yml's, and the secrets that go in the vault.

Each section of local.yml is switched on by a setting of its own (the NAS's addresses, the cluster's node addresses,
Home Assistant's MAC, the routes Tailscale advertises), as the playbooks work it out; the others are asked only when
it is on. tests/policy/test_make_init_settings.py checks this list against local.yml.example and against the vault
variables the group_vars read, so a setting added to one and not the other fails.

Pure data and parsing, no input or output: init.py asks the questions.
"""

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

SECTIONS = {
    "proxmox": "Proxmox host",
    "nas": "TrueNAS NAS",
    "remote": "Remote access through Tailscale",
    "cluster": "Kubernetes cluster",
    "homeassistant": "Home Assistant",
}

# The question that switches each optional section on, and the setting whose value decides it (as nas_present,
# kubernetes_present and haos_present do).
SWITCHES = {
    "nas": ("Is there a TrueNAS NAS, for the VMs' disks, the backups and the apps' data?", "truenas_static_address"),
    "remote": (
        "Should the Proxmox host give remote access to the LAN through Tailscale?",
        "tailscale_advertise_routes",
    ),
    "cluster": ("Should the Proxmox host run the Kubernetes cluster (Talos and Flux)?", "kubernetes_node_addresses"),
    "homeassistant": ("Should the Proxmox host run Home Assistant OS in a VM of its own?", "haos_mac"),
}

CONTROL_PLANE = "homeserver-cp-1"
WORKER = "homeserver-worker-1"
NODES = (CONTROL_PLANE, WORKER)

# What the playbooks use when a setting is unset, for the prompts to show.
DEFAULTS = {
    "kubernetes_domain": "homeserver.internal",
    "kubernetes_talos_config_dir": "~/.talos/homeserver",
    "sops_age_key_file": "~/.config/sops/age/homeserver.agekey",
    "flux_sync_ref": "refs/heads/main",
    "truenas_pool_persistent": "persistent",
    "truenas_pool_ephemeral": "ephemeral",
    "truenas_mail_port": 587,
    "truenas_mail_security": "TLS",
    "haos_owner_name": "Owner",
    "haos_owner_username": "owner",
    "haos_vmid": 310,
    "haos_size": {"cores": 2, "memory_mib": 4096, "disk_gib": 32},
    "proxmox_local_vm_storage": "local-lvm",
    "kubernetes_iso_storage": "local",
    "haos_import_storage": "local",
}


@dataclass(frozen=True)
class Setting:
    key: str
    section: str
    kind: str  # how an answer is read and checked: a key of PARSERS, or a composite handled by init.py
    label: str  # what it is, in one line
    help: str = ""  # more, shown with ?
    need: str = "optional"  # required, recommended or optional, once its section is on
    secret: bool = False  # kept in the vault, never shown
    nas: bool | None = None  # asked only with a NAS (True) or only without one (False)
    after: str | None = None  # asked only once this setting is set
    required_without_nas: bool = False
    popular: bool = False  # asked by a plain make init too; the other optional settings only by TYPE=full
    fields: tuple[str, ...] = field(default_factory=tuple)  # the parts of a composite


def _ipv4(text: str) -> str:
    text = text.strip()
    try:
        return str(ipaddress.IPv4Address(text))
    except ValueError:
        raise ValueError(f"{text!r} is not an IPv4 address, such as 192.0.2.10") from None


def _ipv4_interface(text: str) -> str:
    text = text.strip()
    if "/" not in text:
        raise ValueError("give the address with its prefix length, such as 192.0.2.10/24")
    try:
        return str(ipaddress.IPv4Interface(text))
    except ValueError:
        raise ValueError(f"{text!r} is not an IPv4 address with its prefix, such as 192.0.2.10/24") from None


def _cidr(text: str) -> str:
    text = text.strip()
    try:
        return str(ipaddress.IPv4Network(text, strict=True))
    except ValueError:
        raise ValueError(f"{text!r} is not an IPv4 network in CIDR form, such as 192.0.2.0/24") from None


def _split(text: str) -> list[str]:
    return [p for p in re.split(r"[,\s]+", text.strip()) if p]


def _list_of(parse):
    def parse_list(text: str) -> list:
        items = _split(text)
        if not items:
            raise ValueError("give at least one, separated by commas")
        return [parse(i) for i in items]

    return parse_list


def _mac(text: str) -> str:
    text = text.strip().lower().replace("-", ":")
    if not re.fullmatch(r"[0-9a-f][26ae](:[0-9a-f]{2}){5}", text):
        raise ValueError(
            "a MAC must be locally administered: six pairs of hex digits, the second digit 2, 6, a or e (02:...)"
        )
    return text


def _lb_range(text: str) -> str:
    text = text.strip().replace(" ", "")
    if "-" in text:
        first, last = (_ipv4(p) for p in text.split("-", 1))
        if ipaddress.IPv4Address(first) > ipaddress.IPv4Address(last):
            raise ValueError("the first address of the range comes after the last")
        return f"{first}-{last}"
    if "/" in text:
        return _cidr(text)
    raise ValueError("give a range as first-last (192.0.2.40-192.0.2.49) or a CIDR (192.0.2.40/29)")


def _https_url(text: str) -> str:
    text = text.strip()
    if not re.fullmatch(r"https://[^\s<>]+", text):
        raise ValueError("give an https:// address, such as https://github.com/<you>/homeserver.git")
    if "@" in urlsplit(text).netloc:
        raise ValueError("give the address without a user name or token: local.yml and the cluster would keep them")
    return text


def _http_url(text: str) -> str:
    text = text.strip()
    if not re.fullmatch(r"https?://[^\s<>]+", text):
        raise ValueError("give an http:// or https:// address, such as http://homeassistant.home.arpa/")
    return text


def _git_ref(text: str) -> str:
    text = text.strip()
    if not text.startswith("refs/"):
        text = f"refs/heads/{text}"
    if not re.fullmatch(r"refs/[A-Za-z0-9._/-]+", text):
        raise ValueError("give a branch, such as main, or a full reference, such as refs/heads/main")
    return text


def _hostname(text: str) -> str:
    text = text.strip().lower().rstrip(".")
    label = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
    if not re.fullmatch(rf"{label}(\.{label})*", text):
        raise ValueError(f"{text!r} is not a host name, such as smtp.example.org")
    return text


def _domain(text: str) -> str:
    text = _hostname(text)
    if "." not in text:
        raise ValueError("give a domain with at least two labels, such as homeserver.internal")
    return text


def _name_or_address(text: str) -> str:
    try:
        return _ipv4(text)
    except ValueError:
        return _hostname(text)


def _email(text: str) -> str:
    text = text.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
        raise ValueError(f"{text!r} is not an e-mail address")
    return text


def _port(text: str) -> int:
    value = _int(text)
    if not 1 <= value <= 65535:
        raise ValueError("a port is a number from 1 to 65535")
    return value


def _int(text: str) -> int:
    try:
        return int(text.strip())
    except ValueError:
        raise ValueError(f"{text.strip()!r} is not a whole number") from None


def _count(text: str) -> int:
    value = _int(text)
    if value < 1:
        raise ValueError("give a whole number of at least 1")
    return value


def _size_or_none(text: str) -> int:
    value = _int(text)
    if value < 0:
        raise ValueError("give a whole number, or 0 for none")
    return value


def _vmid(text: str) -> int:
    value = _int(text)
    if not 100 <= value <= 999_999_999:
        raise ValueError("a VMID is a number from 100 to 999999999")
    return value


def _storage_id(text: str) -> str:
    text = text.strip()
    if not re.fullmatch(r"[a-z][a-z0-9_.-]*[a-z0-9]", text):
        raise ValueError(f"{text!r} is not a Proxmox storage id, such as local-zfs")
    return text


def _pool_name(text: str) -> str:
    text = text.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]*", text):
        raise ValueError(f"{text!r} is not a ZFS pool name")
    return text


def _fingerprint(text: str) -> str:
    hexes = re.sub(r"[^0-9A-Fa-f]", "", text.strip().removeprefix("sha256").removeprefix("SHA256"))
    if len(hexes) != 64:
        raise ValueError("a SHA-256 fingerprint is 32 bytes, such as AA:BB:...:99 (64 hex digits)")
    return ":".join(hexes[i : i + 2] for i in range(0, 64, 2)).upper()


def _choice(*options):
    def parse(text: str) -> str:
        value = text.strip().upper()
        if value not in options:
            raise ValueError(f"one of {', '.join(options)}")
        return value

    return parse


def _username(text: str) -> str:
    text = text.strip()
    if not re.fullmatch(r"[a-z0-9._-]+", text):
        raise ValueError("lower case letters, digits, dots, dashes and underscores")
    return text


def _text(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("give a value")
    return text


def _path(text: str) -> str:
    return _text(text)


def _usb(text: str) -> list[dict[str, str]]:
    out = []
    for item in _split(text):
        if item.startswith("mapping:"):
            out.append({"mapping": _text(item.removeprefix("mapping:"))})
        elif re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{4}", item):
            out.append({"host": item.lower()})
        else:
            raise ValueError(f"{item!r}: give a vendor:product id as lsusb shows it (10c4:ea60), or mapping:<id>")
    if not out:
        raise ValueError("give at least one device")
    return out


def _secret(text: str) -> str:
    if not text.strip():
        raise ValueError("give a value")
    return text.strip()


def _owner_password(text: str) -> str:
    if len(text) < 12:
        raise ValueError("at least 12 characters")
    return text


PARSERS = {
    "ipv4": _ipv4,
    "ipv4_interface": _ipv4_interface,
    "ipv4_list": _list_of(_ipv4),
    "cidr": _cidr,
    "cidr_list": _list_of(_cidr),
    "mac": _mac,
    "lb_range": _lb_range,
    "https_url": _https_url,
    "http_url": _http_url,
    "git_ref": _git_ref,
    "host": _name_or_address,
    "domain": _domain,
    "names": _list_of(_name_or_address),
    "email": _email,
    "port": _port,
    "mail_security": _choice("TLS", "SSL", "PLAIN"),
    "vmid": _vmid,
    "vmid_list": _list_of(_vmid),
    "storage_id": _storage_id,
    "pool_name": _pool_name,
    "fingerprint": _fingerprint,
    "username": _username,
    "text": _text,
    "path": _path,
    "usb": _usb,
    "secret": _secret,
    "owner_password": _owner_password,
    "positive_int": _count,
    "size_or_none": _size_or_none,
}

# Composite kinds, asked part by part: node_map (each node's value, of fields' kind), node_sizes, haos_size,
# pool_disks, and bool.
COMPOSITE = {"node_map_ipv4", "node_map_mac", "node_sizes", "haos_size", "pool_disks", "bool"}

NODE_SIZE_FIELDS = {
    CONTROL_PLANE: ("cores", "memory_mib", "system_disk_gib"),
    WORKER: ("cores", "memory_mib", "system_disk_gib", "data_disk_gib", "media_disk_gib"),
}
# The sizes 0 may give: no such disk.
OPTIONAL_DISKS = ("media_disk_gib",)

S = Setting
SETTINGS: tuple[Setting, ...] = (
    # ------------------------------------------------------------------------------------------------- Proxmox ---
    S(
        "proxmox_address",
        "proxmox",
        "ipv4",
        "The Proxmox host's address, which Ansible reaches over root SSH",
        need="required",
        help="The address the Proxmox installer gave the host. Ansible logs in as root with an SSH key: "
        "scripts/runbooks/REBUILD.md (section 3) has the ssh-copy-id line that works with Proxmox VE 9.",
    ),
    S(
        "proxmox_local_vm_storage",
        "proxmox",
        "storage_id",
        f"The installer's VM storage, when it is not {DEFAULTS['proxmox_local_vm_storage']}",
        help="local-zfs on a ZFS install, local-btrfs on a BTRFS one. It holds every VM's disks without a NAS, and the "
        "cluster's system disks with one too.",
        popular=True,
    ),
    S(
        "vm_backup_storage",
        "proxmox",
        "storage_id",
        "Without a NAS: the Proxmox storage the nightly backup writes to",
        nas=False,
        need="recommended",
        help="A USB disk added as a checked directory storage (pvesm set <id> --is_mountpoint yes "
        "--create-base-path 0), or a Proxmox Backup Server. Unset, nothing is backed up.",
    ),
    S(
        "vm_backup_extra_vmids",
        "proxmox",
        "vmid_list",
        "Other VMs to back up nightly, such as one made by hand",
        help="VMIDs separated by commas. Never Home Assistant's: with haos_mac set, the backup covers it by itself.",
    ),
    # ----------------------------------------------------------------------------------------------------- NAS ---
    S(
        "truenas_static_address",
        "nas",
        "ipv4_interface",
        "The address the NAS should end up on, with its prefix (192.0.2.10/24)",
        need="required",
        help="The role moves the NAS from its DHCP lease to this address, with TrueNAS's commit-and-check-in.",
    ),
    S("truenas_gateway", "nas", "ipv4", "The NAS's gateway once DHCP is off", need="required"),
    S("truenas_nameservers", "nas", "ipv4_list", "The NAS's name servers once DHCP is off", need="required"),
    S(
        "truenas_bootstrap_address",
        "nas",
        "ipv4",
        "The address a freshly installed NAS answers on (its DHCP lease), tried after the static one",
        help="Only needed while the NAS has not been given its static address, after a reinstall say.",
        popular=True,
    ),
    S(
        "vault_truenas_api_key",
        "nas",
        "secret",
        "The TrueNAS API key",
        need="required",
        secret=True,
        help="Made in the TrueNAS web UI over HTTPS, owned by a local user in builtin_administrators "
        "(scripts/runbooks/REBUILD.md, section 2). TrueNAS shows it once.",
    ),
    S(
        "truenas_api_cert_sha256",
        "nas",
        "fingerprint",
        "The SHA-256 fingerprint of the NAS's web certificate, so the API key goes to no other server",
        need="recommended",
        help="Compare it with System -> Certificates in the TrueNAS web UI. Replace it whenever the certificate is.",
    ),
    S(
        "truenas_pool_persistent",
        "nas",
        "pool_name",
        f"The redundant pool's name, when it is not {DEFAULTS['truenas_pool_persistent']}",
    ),
    S(
        "truenas_pool_ephemeral",
        "nas",
        "pool_name",
        f"The pool without redundancy's name, when it is not {DEFAULTS['truenas_pool_ephemeral']}",
    ),
    S(
        "truenas_pool_disks",
        "nas",
        "pool_disks",
        "Disk serials per pool, read only to create a pool on blank disks",
        help="Importing an existing pool ignores them. Creating one also needs truenas_pool_allow_create for the run.",
    ),
    S(
        "truenas_mail_server",
        "nas",
        "host",
        "The mail server the NAS sends its alerts through",
        help="A degraded or filling pool among them. Unset, the NAS sends no mail.",
        popular=True,
    ),
    S("truenas_mail_port", "nas", "port", "The mail server's port", after="truenas_mail_server", popular=True),
    S(
        "truenas_mail_security",
        "nas",
        "mail_security",
        "The mail server's security: TLS, SSL or PLAIN",
        after="truenas_mail_server",
        popular=True,
    ),
    S(
        "truenas_mail_user",
        "nas",
        "text",
        "The mail server's login, when it needs one",
        after="truenas_mail_server",
        popular=True,
    ),
    S(
        "vault_truenas_mail_password",
        "nas",
        "secret",
        "The mail server's password",
        secret=True,
        after="truenas_mail_user",
        popular=True,
    ),
    S(
        "truenas_mail_from",
        "nas",
        "email",
        "The sender address, when the login is not one",
        after="truenas_mail_server",
        popular=True,
    ),
    S("truenas_alert_email", "nas", "email", "Where the alerts go", after="truenas_mail_server", need="recommended"),
    # -------------------------------------------------------------------------------------------------- Remote ---
    S(
        "tailscale_advertise_routes",
        "remote",
        "cidr_list",
        "The LAN's IPv4 prefix, which the host advertises",
        need="required",
        help="Write the tailnet's policy in Tailscale's admin console first: a new tailnet allows everything "
        "(scripts/runbooks/tailscale.md, section 1).",
    ),
    S(
        "vault_tailscale_auth_key",
        "remote",
        "secret",
        "A one-off Tailscale auth key, for the host's first login",
        secret=True,
        need="recommended",
        help="Pre-approved and tagged tag:homeserver (scripts/runbooks/tailscale.md, section 4). Spent at the first "
        "login; a spent one does nothing.",
    ),
    # ------------------------------------------------------------------------------------------------- Cluster ---
    S(
        "kubernetes_node_addresses",
        "cluster",
        "node_map_ipv4",
        "Each node's address, reserved in the router's DHCP outside its dynamic range",
        need="required",
        fields=NODES,
    ),
    S(
        "kubernetes_node_macs",
        "cluster",
        "node_map_mac",
        "Each node's MAC, chosen by you and reserved in DHCP for its address",
        need="required",
        fields=NODES,
        help="Locally administered, in lower case. Enter on an empty one takes the random MAC offered.",
    ),
    S(
        "kubernetes_lb_range",
        "cluster",
        "lb_range",
        "MetalLB's addresses, outside the DHCP range: first-last or a CIDR",
        need="required",
    ),
    S(
        "kubernetes_lb_traefik_ip",
        "cluster",
        "ipv4",
        "The ingress's (Traefik's) address, in that range",
        need="required",
    ),
    S(
        "kubernetes_lb_frigate_ip",
        "cluster",
        "ipv4",
        "The cameras' streams' (Frigate's) address, in that range",
        need="required",
    ),
    S(
        "flux_sync_url",
        "cluster",
        "https_url",
        "The Git repository the cluster syncs, over HTTPS",
        need="required",
        help="Flux applies whatever it holds with cluster-admin rights, so it has no default: a fork names its own.",
    ),
    S(
        "flux_sync_ref",
        "cluster",
        "git_ref",
        f"The branch Flux syncs, when not {DEFAULTS['flux_sync_ref'].removeprefix('refs/heads/')}",
        help="Only main is protected; whoever can push to another branch owns the cluster "
        "(scripts/runbooks/talos.md, Syncing a branch other than main).",
    ),
    S(
        "flux_sync_ref_unprotected",
        "cluster",
        "bool",
        "Sync that unprotected branch all the same (a test cluster), keeping Flux's source suspended between tests",
        after="flux_sync_ref",
    ),
    S(
        "kubernetes_domain",
        "cluster",
        "domain",
        "The domain the apps answer under, as <app>.<domain>",
        help="Point a wildcard DNS record for *.<domain> at the ingress's address. Another domain needs a CA of its "
        "own (scripts/runbooks/tls.md, Making a new CA).",
        popular=True,
    ),
    S(
        "kubernetes_ingress_allowed_range",
        "cluster",
        "cidr",
        "The clients Traefik serves (your LAN's prefix), once every app is set up",
        help="Leave it unset on a new cluster: several apps make their first visitor the admin. Set them up through "
        "port forwards first (scripts/runbooks/first-run.md), then set this and run make kubernetes.",
        popular=True,
    ),
    S(
        "kubernetes_api_sans",
        "cluster",
        "names",
        "Extra names for the Kubernetes API's certificate",
        help="Set before the first build, so the API can later be reached by name.",
    ),
    S(
        "kubernetes_node_sizes",
        "cluster",
        "node_sizes",
        "The nodes' sizes, when not the defaults",
        required_without_nas=True,
        help="Without a NAS, the worker's every size is required (the defaults fit the home host): at least 4 cores, "
        "12 GiB of memory, a 32 GiB system disk and an 11 GiB data disk, with a media disk (11 GiB or more) unless the "
        "media stay on the data volume.",
    ),
    S(
        "kubernetes_media_on_data_volume",
        "cluster",
        "bool",
        "Without a NAS: keep the media on the worker's data volume rather than a media disk",
        nas=False,
        popular=True,
    ),
    S(
        "kubernetes_iso_storage",
        "cluster",
        "storage_id",
        f"The storage for the Talos ISO, when not {DEFAULTS['kubernetes_iso_storage']}",
        help="local-btrfs on a BTRFS install, which disables local.",
    ),
    S(
        "kubernetes_gpu_mapping",
        "cluster",
        "text",
        "The Proxmox PCI resource mapping of the host's iGPU",
        help="Made by hand first (scripts/runbooks/gpu.md). Unset, every app runs on the CPU.",
        popular=True,
    ),
    S(
        "kubernetes_talos_config_dir",
        "cluster",
        "path",
        f"Where this machine keeps the cluster's credentials, when not {DEFAULTS['kubernetes_talos_config_dir']}",
        help="One directory per cluster, outside the repository: a test host's names its own.",
    ),
    S(
        "sops_age_key_file",
        "cluster",
        "path",
        f"The age key Flux decrypts the cluster's Secrets with, when not {DEFAULTS['sops_age_key_file']}",
    ),
    S(
        "haos_url",
        "cluster",
        "http_url",
        "Home Assistant's address, for Homepage's tile",
        help="Its own local name, never under the cluster's domain (scripts/runbooks/haos.md).",
        popular=True,
    ),
    # ------------------------------------------------------------------------------------------ Home Assistant ---
    S(
        "haos_mac",
        "homeassistant",
        "mac",
        "Home Assistant's MAC, reserved in DHCP with the hostname homeassistant",
        need="required",
        help="Locally administered, and not a cluster node's. Enter takes the random MAC offered.",
    ),
    S(
        "vault_haos_owner_password",
        "homeassistant",
        "owner_password",
        "The password of Home Assistant's owner account, at least 12 characters",
        need="required",
        secret=True,
        help="A long random one from your password manager: the run gives a new Home Assistant its owner before "
        "anyone else on the network can.",
    ),
    S(
        "haos_usb",
        "homeassistant",
        "usb",
        "USB devices to pass through, such as a Zigbee stick",
        help="vendor:product ids as lsusb on the Proxmox host shows them (10c4:ea60), or mapping:<id> for a Proxmox "
        "USB resource mapping, separated by commas.",
        popular=True,
    ),
    S(
        "haos_size",
        "homeassistant",
        "haos_size",
        "Its size, when not {cores} cores, {memory_mib} MiB of memory and a {disk_gib} GiB disk".format(
            **DEFAULTS["haos_size"]
        ),
    ),
    S("haos_owner_name", "homeassistant", "text", f"The owner account's name, when not {DEFAULTS['haos_owner_name']}"),
    S(
        "haos_owner_username",
        "homeassistant",
        "username",
        f"The owner account's username, when not {DEFAULTS['haos_owner_username']}",
    ),
    S("haos_vmid", "homeassistant", "vmid", f"Its VMID, when {DEFAULTS['haos_vmid']} is taken"),
    S(
        "haos_import_storage",
        "homeassistant",
        "storage_id",
        f"The storage the OVA is extracted on, when not {DEFAULTS['haos_import_storage']}",
        help="local-btrfs on a BTRFS install, which disables local.",
    ),
)
del S

BY_KEY = {s.key: s for s in SETTINGS}


def without_example(value: Any, example: Any) -> Any:
    """A setting's value without what it shares with local.yml.example's value for it, which an older make init
    copied into local.yml: an entry of a mapping equal to the example's, an item of a list the example's list holds.
    None when nothing else is left."""
    if value == example:
        return None
    if isinstance(value, dict) and isinstance(example, dict):
        kept = {k: r for k, v in value.items() if (r := without_example(v, example.get(k))) is not None}
        return kept or None
    if isinstance(value, list) and isinstance(example, list):
        return [v for v in value if v not in example] or None
    return value


def is_set(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str | list | dict):
        return bool(value.strip() if isinstance(value, str) else value)
    return True


def parse(setting: Setting, text: str) -> Any:
    """An answer as the value local.yml or the vault keeps, or ValueError saying what is wrong."""
    return PARSERS[setting.kind](text)
