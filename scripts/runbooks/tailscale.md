# Remote access through Tailscale

The Proxmox host is the LAN's Tailscale subnet router (`roles/proxmox_tailscale`). Every VM on its bridge, the
NAS, the router and every app behind Traefik answer from your tailnet at their LAN addresses. None of them runs
Tailscale. The role installs and keeps Tailscale on the host. This runbook covers what only Tailscale's admin
console can do (the policy, DNS and the auth key), the run itself, and how to check it all from outside.

Values are named by their variable in `group_vars/all/local.yml`; examples use RFC 5737 placeholders:

- `192.0.2.0/24` for the LAN;
- `192.0.2.1` for the router;
- `192.0.2.10` for the NAS (`truenas_static_address`);
- `192.0.2.20` for the Proxmox host (`proxmox_address`).

## Why the NAS needs care

The host masquerades every tailnet client to its own LAN address, and the NAS exports the VM disks and backups to
that address alone, with root kept as root. Any tailnet client allowed to reach the NAS's NFS ports would be trusted
like the host.

Two controls stop that, each enough alone:

- **the tailnet policy below**, which gives the NAS only its HTTPS port;
- **the role's guard on the host** (`roles/proxmox_tailscale/README.md`), which drops everything else to the NAS
  whatever the policy says.

Both are allow-lists. The NAS's mountd, statd and lock manager listen on random ports, so a list of NFS ports to block
would miss them.

## 1. The policy, before anything else

A new tailnet lets every device reach every other on every port, subnet routes included. **Write this policy before
the host advertises anything.**

In the admin console, open Access controls and merge this into the policy. Check it in the preview before saving:

```jsonc
{
  // [] would mean the same: the tailnet's admins.
  "tagOwners": { "tag:homeserver": ["autogroup:admin"] },
  // The host's route is approved as soon as it is advertised, after a reinstall too.
  "autoApprovers": { "routes": { "192.0.2.0/24": ["tag:homeserver"] } },
  // The LAN without the NAS, whose NFS shares trust the Proxmox host's address.
  "ipsets": { "ipset:lan-but-nas": ["add 192.0.2.0/24", "remove 192.0.2.10"] },
  "grants": [
    // Your devices: every LAN address but the NAS, on every port (the router's DNS included).
    { "src": ["autogroup:member"], "dst": ["ipset:lan-but-nas"], "ip": ["*"] },
    // The NAS: its web UI and API only. Never NFS (2049), the port mapper (111) or mountd.
    { "src": ["autogroup:member"], "dst": ["192.0.2.10"], "ip": ["tcp:443"] },
    // The host itself, at its tailnet address.
    { "src": ["autogroup:member"], "dst": ["tag:homeserver"], "ip": ["*"] },
    // Your devices among themselves.
    { "src": ["autogroup:member"], "dst": ["autogroup:self"], "ip": ["*"] }
  ]
  // No grant has tag:homeserver as its source: the host cannot start a connection to any of your devices.
}
```

Without a NAS, leave out the `ipsets` entry and the NAS's grant, and give the first grant `"dst": ["192.0.2.0/24"]`.

## 2. DNS

In the admin console's DNS settings, add a nameserver: the router's LAN address, restricted to two domains:

- the cluster's domain (`kubernetes_domain`, `homeserver.internal` by default);
- the router's local domain, the search domain in the Proxmox host's `/etc/resolv.conf`.

Unbound on the router then answers with the same wildcard record as at home. The queries reach it masqueraded to the
Proxmox host's address, which its default access list already allows. Leave MagicDNS as it is.

## 3. Two-factor authentication on Proxmox

The Proxmox web UI is now one hop from every device the policy admits. Turn on two-factor authentication for
`root@pam`: Datacenter → Permissions → Two Factor.

## 4. The auth key

In the admin console, open Settings → Keys and generate an auth key:

- one-off (not reusable);
- pre-approved;
- tagged `tag:homeserver`;
- not ephemeral;
- with the shortest expiry that covers the run.

Copy it once into the vault: `make vault-edit`, as `vault_tailscale_auth_key`. The first run spends it; a spent key
left there does nothing.

## 5. The run, from home

1. In `group_vars/all/local.yml`, set `tailscale_advertise_routes: [<the LAN's prefix>]`. Update the
   password-manager copy of `local.yml`.
2. Run `make check-proxmox`. Expect:
   - the keyring, `tailscale.sources`, the packages, the guard's three files and the sysctl file;
   - "Would enable the guard at boot and load the guard";
   - "IPv4 forwarding is off";
   - "Would log the node in with the auth key".
3. Run `make proxmox`, **from home**. `make proxmox` upgrades Tailscale on every run, which restarts it. Run from away,
   that drops the connection the run itself uses. An OpenSSH session usually rides it out, but if dpkg is
   interrupted, `dpkg --configure -a` over a new session repairs it, and if Tailscale does not come back, remote
   access waits until you are home.
4. Run `make proxmox` again. The Tailscale tasks report no change, and the last one says "Nothing for the admin
   console to fix." It may also print Tailscale's own warning that UDP GRO forwarding is suboptimal on `vmbr0`; that
   concerns throughput only.
5. On the host, check the guard:
   - `nft list table inet tailnet_guard` shows the NAS in `guarded`;
   - `systemctl show -p Requires,After tailscaled` names `tailnet-guard.service`.

## 6. The machine in the console

In the admin console, the host must show three things:

- tagged `tag:homeserver`;
- key expiry disabled (tagged machines have it so by default);
- the route approved.

If Tailnet Lock is on, sign the node from a signing device: `tailscale lock sign`.

## 7. Optional: direct connections

OPNsense's outbound NAT randomises source ports, so peers may connect through Tailscale's DERP relays. That works,
only slower. For direct connections:

1. Open Firewall → NAT → Outbound, in hybrid mode.
2. Add a rule for the Proxmox host's address, UDP, source port 41641, with "static port".

`tailscale ping <the host's tailnet name>` says whether a connection is direct or relayed.

## 8. Your devices

- **Phones and laptops:** macOS, iOS, Windows and Android use subnet routes by default. Linux needs
  `tailscale set --accept-routes`.
- **At home:** such a device sends LAN traffic through the tunnel to the host rather than straight to the target. That
  still works, but it stops while the host is down, and the NAS sees the host's address. Turn on VPN On Demand on
  iOS and macOS so that Tailscale disconnects on your home Wi-Fi.

## 9. Check it from outside

From a phone on mobile data:

- **The hosts:**
  - `ssh` into a VM at its LAN address;
  - `https://<proxmox_address>:8006` loads the Proxmox UI;
  - Home Assistant answers by its name;
  - with a cluster: `http://<app>.<domain>` loads through Traefik, and `kubectl get nodes` works.
- **Names:** `nslookup <app>.<domain>` answers with Traefik's address.
- **The NAS:** its UI on 443 loads, and these all **time out**:
  - `nc -vz <nas> 2049`
  - `nc -vzu <nas> 2049`
  - `nc -vz <nas> 111`
  - `showmount -e <nas>`
- **The guard alone.** Widen the policy for one test device to every port on the NAS for a few minutes:
  1. The commands above must still time out.
  2. On the host, `nft list counters table inet tailnet_guard` shows `guarded_dropped` rising, and
     `journalctl -k -g tailnet-guard` shows the drops.
  3. Put the policy back.
- **After a reboot.** Reboot the host once, at a quiet time:
  - remote access comes back by itself;
  - `systemd-analyze critical-chain tailscaled.service` shows the guard first.

Test the guard's fail-closed behaviour only at the console, never from away. With a deliberately broken
`/etc/nftables.d/tailnet-guard.nft`, `systemctl start tailnet-guard` fails and `tailscaled` refuses to start. Then
`make proxmox` puts the file back. `systemctl stop tailnet-guard` stops Tailscale too.

## After a reinstall of the host

The node's identity lived on the old boot drive.

1. In the admin console, delete the old machine first, or the new one is named `<hostname>-1`.
2. Make a new key (step 4) and run `make proxmox`.

The auto-approver approves the route again.

## Removing it

1. Remove `tailscale_advertise_routes` from `local.yml`. The role then does nothing, and removes nothing.
2. On the host:

   ```bash
   tailscale logout
   apt purge tailscale tailscale-archive-keyring
   systemctl disable tailnet-guard
   rm /etc/systemd/system/tailnet-guard.service /etc/systemd/system/tailscaled.service.d/10-tailnet-guard.conf
   systemctl daemon-reload
   nft destroy table inet tailnet_guard
   rm /etc/nftables.d/tailnet-guard.nft /etc/apt/sources.list.d/tailscale.sources /etc/sysctl.d/99-tailscale.conf
   sysctl -w net.ipv4.ip_forward=0
   ```

3. Delete the machine in the admin console.
