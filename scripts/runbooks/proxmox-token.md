# The Proxmox API token

The project's one Proxmox API token, `homeserver@pve!homeserver`, which `just status` reads the Proxmox host with:
through Proxmox's API on the host's own loopback, each read a few milliseconds, where `pvesh` starts Proxmox's Perl
API afresh for every read. The repo makes no Proxmox user, role, token or permission itself; this runbook does it,
once per host, by hand.

It is the project's token, not just status's: another role may use it later, and adds the privileges it needs here,
in the table below. Each Proxmox install makes its own secret, so each setup's vault holds its own host's.

## Make it

On the controller, with root SSH to the host as Ansible has it (the `Host` block for it in `~/.ssh/config`): a role of
the project's own holding only the privileges the table below lists, a user without a password, the role for that
user on `/`, and the user's token.

```bash
ssh <host> 'pveum role add Homeserver --privs "Datastore.Audit,Sys.Audit,VM.Audit,VM.GuestAgent.Audit"'
ssh <host> 'pveum user add homeserver@pve --comment "homeserver: its Proxmox API token"'
ssh <host> 'pveum acl modify / --users homeserver@pve --roles Homeserver'
ssh <host> 'pveum user token add homeserver@pve homeserver --privsep 0 --output-format json' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["value"], end="")' | pbcopy
```

The last line puts the secret on the clipboard (`pbcopy` is macOS's; elsewhere, drop it and copy the `value`
printed), never in the shell's history or on the screen. Proxmox shows it once: put it in your password manager now.
The clipboard is not private for long: macOS's Universal Clipboard hands it to your other devices on the same
Apple ID, and a clipboard manager keeps a history of it, so clear it once the check below is done.

- **The user has no password**: it signs in by its token alone, and nobody can log in to the web UI as it.
- **`--privsep 0`** gives the token exactly its user's privileges, which are the role's and no others, so one grant
  on the user covers the token. The web UI's own form ticks Privilege Separation by default; a token made there with
  the box ticked holds nothing until it has a grant of its own, and `just status` then says which privileges it
  lacks.
- **The token does not expire.** `pveum user token add … --expire <epoch>` makes one that does, to be made again
  before the date (below); `just status` then says Proxmox refused it.
- **A token with another name** works too: name it in `local.yml` (`proxmox_token_id: <user>@<realm>!<name>`), and
  use that name below.

## Check it

The token's privileges, then a read on the host's own loopback, which answers 200. The secret goes to the host on
SSH's stdin, and from there to curl on curl's stdin (`-H @-`), never on a command line, where any account on the
host could read it while curl runs.

```bash
user=homeserver@pve name=homeserver   # or local.yml's proxmox_token_id, split at its exclamation mark
ssh <host> "pveum user token permissions $user $name --path /"
pbpaste | ssh <host> "read -r s; printf 'Authorization: PVEAPIToken=%s\041%s=%s\n' '$user' '$name' \"\$s\" | curl -sk -o /dev/null -w '%{http_code}\n' -H @- https://127.0.0.1:8006/api2/json/version"
```

No `!` is typed: an interactive shell would take it for its history (`printf` writes it, as `\041`).

Then put the secret in the vault as `vault_proxmox_token_secret` (`just vault`, or `just init`, which asks for it),
and clear the clipboard: `pbcopy < /dev/null`. `just status vms` reads the host with it from then on, and says so if
the token may not read something.

## What it may do

| Privilege | Who needs it, for what |
| --- | --- |
| `Sys.Audit` | `just status`: the node's status and history, the backup jobs, the vzdump runs and their logs (another user's tasks need it on the node), and the node's figures in `/cluster/resources` |
| `VM.Audit` | `just status`: every VM's state, history and pending changes; `/cluster/resources` leaves out a VM the token lacks it for |
| `VM.GuestAgent.Audit` | `just status`: Home Assistant's filesystems, OS and address, from its guest agent (Proxmox VE 9 moved these reads from `VM.Monitor`) |
| `Datastore.Audit` | `just status`: each storage's use; `/cluster/resources` leaves out a storage the token lacks it for |

The role `Homeserver` holds these four on `/` and nothing else, and nothing grants a privilege no role uses.
`just status` reads the token's privileges every run (`/access/permissions`) and says which of them it lacks.

**What a leaked secret is worth.** Read-only as it is, the token reads the whole host: every VM's configuration,
every task's log, the node's report (`/nodes/<node>/report`: its filesystems, interfaces and disks, by name and
serial), the datacenter's options. pveproxy answers on every address the host has, the LAN's and the tailnet's, so
the secret works from anywhere on either: keep it as you keep the vault's other secrets.

**Two lists are not in the table, on purpose: a storage's VM disks and its backups.** Proxmox lists a VM's disk only
to a token holding `VM.Config.Disk` on that VM (or `Datastore.Allocate` on the storage), and a backup only to one
holding `VM.Backup` on the VM and `Datastore.AllocateSpace` on the storage (`check_volume_access` in
`PVE/Storage.pm`): each can change what it lists. `just status` reads those two through `pvesh` as root instead, in
the same pass, and the token stays read-only.

**When a role needs more**, add the privilege to the role, and to this table with why:

```bash
ssh <host> 'pveum role modify Homeserver --append 1 --privs "<privilege>"'
```

`just status` stays read-only whatever the token may do: its reads are GETs alone, to the exact paths on
`status_read`'s list. The secret's worth grows with the token's privileges, though: once it can change anything,
keep the vault as you would root's password.

**A token made before the role**, with `PVEAuditor` on `/` (which also grants `Mapping.Audit`, `Pool.Audit` and
`SDN.Audit`, which nothing uses): make the role and move the grant to it; the token and its secret stay.

```bash
ssh <host> 'pveum role add Homeserver --privs "Datastore.Audit,Sys.Audit,VM.Audit,VM.GuestAgent.Audit"'
ssh <host> 'pveum acl modify / --users homeserver@pve --roles Homeserver'
ssh <host> 'pveum acl delete / --users homeserver@pve --roles PVEAuditor'
```

## Where the secret goes

Only to the host it belongs to, inside the root SSH connection, and from there to the host's own loopback, on a
connection whose certificate is the node's own: `roles/status/README.md` ("Proxmox's own reads go through its API")
says how. It never crosses the LAN or the tailnet.

## Replace a lost or expired secret

Proxmox never shows a secret again. Remove the token and make it anew, then put the new secret in the vault:

```bash
ssh <host> 'pveum user token remove homeserver@pve homeserver'
ssh <host> 'pveum user token add homeserver@pve homeserver --privsep 0 --output-format json' \
  | python3 -c 'import json, sys; print(json.load(sys.stdin)["value"], end="")' | pbcopy
```

Until the vault holds it, `just status` says the Proxmox host could not be read and points here. `just deploy` never
needs the token.
