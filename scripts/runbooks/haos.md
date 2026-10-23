# Home Assistant OS on Proxmox

Home Assistant runs as a VM of its own, Home Assistant OS (HAOS), not in the cluster: home automation must keep
working while the cluster is down, and HAOS manages its own updates, add-ons and backups.
`make deploy TYPE=homeassistant` (`homeassistant.yml`) creates it and keeps its settings, with or without a cluster or a
NAS; `roles/proxmox_vm` builds the VM and `roles/haos_onboard` gives a new Home Assistant its owner account.

## Before the first run

1. **A MAC, reserved in DHCP.** Choose a locally administered MAC (lower case, the second hex digit 2, 6, a or e, and
   not a cluster node's), put it in `group_vars/all/local.yml` as `haos_mac`, and in the router reserve an address
   for it with the hostname `homeassistant`, so it is reachable as `homeassistant.<the router's local domain>`.
   Never under the cluster's wildcard: a `*.<domain>` record answers every name below it with Traefik's address.
   Home Assistant serves on its own ports, never through the cluster: Home Assistant OS 18 serves it on 80 and
   redirects the classic 8123 there.
2. **The owner's password**, at least 12 characters, a long random one kept in your password manager:
   `make vault`, then set `vault_haos_owner_password: <password>`. The run refuses to start a Home Assistant
   without it (below says why).
3. **A Zigbee or Z-Wave stick**, if there is one: plug it into the Proxmox host, read its id with `lsusb`
   (`ID 10c4:ea60 ...`), and name it in `local.yml`: `haos_usb: [{host: "10c4:ea60"}]`. The vendor and product id
   follow the stick to any port. A Proxmox USB resource mapping works too: `[{mapping: <id>}]`.
4. Optionally, in `local.yml`: `haos_size` (2 cores, 4 GiB and a 32 GiB disk otherwise; the disk never below 32),
   `haos_owner_name` and `haos_owner_username` (Owner and owner otherwise), `haos_vmid` (310 otherwise).

5. **Without a NAS, images on `local`, once.** Proxmox extracts the OVA's disk onto a directory storage that holds
   VM images before converting it onto `local-lvm`, and a fresh install's `local` holds none. On the Proxmox host:
   `pvesm set local --content backup,import,iso,vztmpl,images`, keeping whatever else it holds now: `--content`
   replaces the whole list, which `pvesh get /storage/local` shows. The extracted copy is removed after the import.
   With a NAS, its storage serves, and nothing is needed. The run refuses, naming the command, until it is done; no
   role changes the installer's storages. A BTRFS install disables `local`: name `local-btrfs` in `local.yml` as
   `haos_import_storage`. It holds images already; the run refuses, naming it, if it lacks `import` content.

`make deploy TYPE=proxmox` must have run, so the storage the disk goes on exists.

## The run

```bash
make check TYPE=homeassistant     # what it would download, create and correct
make deploy TYPE=homeassistant
```

It downloads Home Assistant OS's OVA of the pinned version (`haos_version` in `group_vars/proxmox/haos.yml`) into
the installer's `local` storage, which Proxmox checks against its pinned sha256 before storing it, and creates
VM 310 with UEFI (no pre-enrolled keys), its disk imported from the OVA and grown to its size, both disks on
`homeserver-persistent` (qcow2) with a NAS and on the installer's storage without one, the i6300esb watchdog (Home Assistant OS arms
it, so a hung VM is reset), the USB devices, and the startup order after the cluster's nodes. It starts the VM, finds
its address through the QEMU guest agent, waits for Home Assistant (half an hour at most: the first boot downloads
it), and creates the owner account.

**Why the account is made for you.** Until a Home Assistant has an owner, its onboarding API answers without a
login: whoever first reaches port 8123 could create the owner account, and so own the house, or upload and restore a
backup (the path behind CVE-2026-64825). Both close once the account exists, which the run makes within moments of
Home Assistant answering. While Home Assistant OS prepares on its first boot its landing page refuses the onboarding
API, but still forwards a few Supervisor calls (network, resolution, job status) without a login, for some minutes:
an upstream design, which a servers VLAN closes (`TODO.md`).

Then log in at `http://homeassistant.<local domain>/` as the owner, and either finish setting it up or restore
a backup (Settings, System, Backups). Home Assistant updates itself; the pinned OVA only ever makes a new VM. A later
run corrects the VM's size, USB devices and other settings, reports what waits for a restart, and leaves Home
Assistant itself alone: a Home Assistant that has its owner, set up by hand or restored, is never onboarded again.

## Stopping it for a while

`make suspend ONLY=homeassistant` shuts the VM down through Home Assistant OS's own shutdown, never a forced stop (one
that takes more than three minutes fails the run and leaves it running), and `make resume ONLY=homeassistant` starts it;
a bare `make suspend` stops it with the cluster's apps. It stays down until then, or until the host boots or
`make deploy TYPE=homeassistant` runs, both of which start it.

## Backups

The nightly VM backup (`vm-nightly`, `make deploy TYPE=proxmox`) covers VM 310 whenever `haos_mac` is set, in both
setups and whether or not a cluster exists. Home Assistant's own backups land on its disk, so they are inside that
backup too. Restoring: `scripts/runbooks/vm-restore.md`. A VMID whose VM is gone but whose backups remain is refused by
`make deploy TYPE=homeassistant`: restore it rather than build a new, empty one over its history.

## Homepage

Homepage's Home Assistant tile links to `haos_url` from `local.yml`, when it is set: a URL on the VM's local name,
such as `http://homeassistant.<local domain>/`, never tracked. Without it the tile links nowhere.

## TLS

Home Assistant keeps its own certificate (its Let's Encrypt or NGINX add-on), since it is not behind Traefik. The
cluster's private CA (`scripts/runbooks/tls.md`) is limited to the cluster's domain and cannot sign for it.

## A new HAOS version

Bump `haos_version` and `haos_ova_sha256` together: the digest GitHub shows for the release's `haos_ova-<v>.ova`
(`gh api repos/home-assistant/operating-system/releases/tags/<v> --jq '.assets[] | select(.name|endswith(".ova")) | .digest'`).
Only a new VM uses it; the old OVA stays in `local` until removed by hand (the run lists it).
