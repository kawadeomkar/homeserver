# proxmox_notes

Keeps a block of text in the Notes of a Proxmox VE node and of its VMs, the Markdown the web UI shows on each one's
Summary, and leaves every other line of them as it is. This repo uses it to stamp each machine with the version of
the repo that last deployed it (`scripts/runbooks/tags.md`); the role itself knows nothing of versions. With only
its defaults it changes nothing.

The block lies between two HTML comments that name it, the begin marker carrying key=value metadata:

```markdown
<!-- homeserver:begin commit=5de78852f353b2353ba33a8b8dcbc7217928bb1c describe=2026.11.0-3-g5de78852f353 date=2026-10-24 -->
Deployed from homeserver `2026.11.0-3-g5de78852f353`, committed 2026-10-24. `just deploy kubernetes` deploys it, and `just status` shows what it runs now.
<!-- homeserver:end -->
```

The web UI renders Notes as Markdown and keeps HTML comments hidden on purpose ("some users use comments inside notes
to embed (non-secret!) metadata", proxmox-widget-toolkit's `Parser.js`), so only the text shows, and a tool such as
`just status` reads the metadata back. Another project can keep a block of its own, under its own marker, beside
this one.

- **Only the block is written.** Without one, it is appended after a blank line; with one, it is replaced from
  marker to marker. A marker is a line of its own from the first column, outside any fenced code block: an indented
  or fenced one, such as this example pasted into the Notes, is Markdown's text and counts for nothing. A marker
  without its partner, or two blocks, is refused, since what lies between could be anyone's: mend or remove it by
  hand. So are Notes that end inside a fenced code block, where an appended block would be text, and Notes longer
  than Proxmox takes, 8192 characters for a VM and 65536 for the node.
- **Compared as Proxmox gives them back.** It trims a VM's Notes at the end, and the trailing spaces of each line of
  a node's, so the role compares in that form and a second run changes nothing. What it writes keeps every other line as it was read,
  trailing whitespace included (two spaces end a Markdown line), and as typed: the Notes go to `qm` and `pvenode` as
  one argument, never expanded, so a `$HOME` or a leading `~` in them stays text.
- **At once.** Notes apply to a running VM directly, never as a pending change waiting for a restart
  (`description` is in qemu-server's `$fast_plug_option`).
- **A dry run** says what it would write, block by block. A VM that does not exist yet, which an earlier role of
  the same run would create, is reported as stamped once created; on a real run it is refused.
- **A write Proxmox refuses fails the run**, with Proxmox's own message and never the Notes themselves: one to a VM
  locked by a running backup, or one whose configuration changed after the role read it (each write passes the
  digest of what it read). The Notes stay as they were, and the next run writes them.
- **Nothing to write.** With an empty text the role reads nothing and writes nothing, and says
  `proxmox_notes_skip_reason` when it is set, so a run that stamps nothing can say why.

It runs over root SSH on the node it stamps, with `pvesh` to read, `qm set --description` for a VM and `pvenode
config set --description` for the node, both of which write the local node's. The parsing and the plan are a filter plugin (`filter_plugins/proxmox_notes.py`), unit-tested in
`tests/unit/test_proxmox_notes.py`.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `proxmox_notes_marker` | `""` | The word that marks the block, such as the project's name: lower-case letters, digits and hyphens |
| `proxmox_notes_metadata` | `{}` | The key=value pairs the begin marker carries, hidden in the web UI: lower-case keys, one-word values of letters, digits and `. _ + -`; the role knows no version's shape |
| `proxmox_notes_text` | `""` | The block's text, Markdown, holding no HTML comment. Empty, nothing is written |
| `proxmox_notes_node` | `false` | Whether the block goes in the node's own Notes |
| `proxmox_notes_vmids` | `[]` | The VMs whose Notes hold the block |
| `proxmox_notes_skip_reason` | `""` | What to say when the text is empty, for Notes the role was given: why there is no block to write |

## Example

```yaml
- hosts: proxmox
  roles:
    - role: proxmox_notes
      vars:
        proxmox_notes_marker: myproject
        proxmox_notes_metadata: {release: "2026.11.0"}
        proxmox_notes_text: "Configured by myproject 2026.11.0."
        proxmox_notes_vmids: [300, 301]
```

This repo's settings are in `group_vars/proxmox/notes.yml`, and each deploy playbook names the machines it stamps.

## Reference

[REFERENCE.md](REFERENCE.md) is docsible's page for this role, written by `just fix docs` from the role itself:
its argument specs, defaults and vars, and each tasks file as a table and a flow graph. Never edit it by hand.
