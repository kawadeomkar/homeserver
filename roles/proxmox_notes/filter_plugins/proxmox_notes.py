"""What roles/proxmox_notes writes in the Notes of a Proxmox node and of its VMs, worked out from what it read.

The role keeps a block of its own in each, between two HTML comments that name it:

    <!-- <marker>:begin key=value ... -->
    <text>
    <!-- <marker>:end -->

and leaves every other line of the Notes as it is. Proxmox renders Notes as Markdown and keeps HTML comments hidden on
purpose ("some users use comments inside notes to embed (non-secret!) metadata", proxmox-widget-toolkit's Parser.js),
so the markers and their key=value metadata never show in the web UI, and another project can keep a block of its own
beside this one. A marker is a line of its own from the first column, outside any fenced code block: an indented or
fenced one is Markdown's text, such as an example of the block pasted in. roles/status's filter reads the block by the
same rule. Everything here is pure: it runs on the controller, never connects anywhere, and is unit-tested in
tests/unit/test_proxmox_notes.py.

Proxmox gives Notes back as it stores them, which is not quite as they were written: a VM's with trailing whitespace
trimmed (qemu-server's parse_vm_config), a node's with each line's trailing spaces trimmed and a newline after each
(pve-manager's NodeConfig writes each line as a comment, which PVE::JSONSchema::parse_config reads back trimmed; a node
with no configuration file answers no description at all). So Notes are compared in that form, never as text, while
what is written keeps every other line as it was read, its trailing whitespace included (two trailing spaces are a
Markdown line break, which a VM's Notes keep).
"""

import json
import re

# Proxmox's limits on Notes, in characters: qemu-server's maxLength for a VM's description, pve-manager's NodeConfig
# for a node's.
VM_LIMIT = 8 * 1024
NODE_LIMIT = 64 * 1024
# Each is matched whole (fullmatch): a $ would also match before a final newline, which would split a begin marker
# over two lines that the next run could no longer read.
MARKER = re.compile(r"[a-z][a-z0-9-]{0,31}")
META_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}")
# A metadata value is one word of the characters a commit, a tag or a date is written with: never a space, which
# separates the pairs, nor anything that could end the comment.
META_VALUE = re.compile(r"[A-Za-z0-9._+-]{1,128}")
VMID = re.compile(r"[1-9][0-9]{2,8}")
# A fenced code block's fence: up to three spaces, then three or more backticks or tildes.
FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")


def _begin(marker):
    return re.compile(rf"<!-- {re.escape(marker)}:begin( [^\n]*)? -->")


def _end(marker):
    return f"<!-- {marker}:end -->"


def _outside_code(lines):
    """The indexes of the lines outside fenced code blocks, and whether the last fence is left open."""
    outside, fence = [], ""
    for i, line in enumerate(lines):
        found_fence = FENCE.match(line)
        if not fence:
            if found_fence:
                fence = found_fence.group(1)
            else:
                outside.append(i)
        elif (
            found_fence
            and found_fence.group(1)[0] == fence[0]
            and len(found_fence.group(1)) >= len(fence)
            and not line[found_fence.end() :].strip()
        ):
            fence = ""
    return outside, bool(fence)


def markers(lines, marker):
    """(begins, ends, open): the indexes of the lines that are the block's begin and end markers, and whether the Notes
    end inside a fenced code block. A marker is a line of its own from the first column (trailing whitespace aside),
    outside any fenced code block."""
    begin, end = _begin(marker), _end(marker)
    outside, open_fence = _outside_code(lines)
    begins = [i for i in outside if begin.fullmatch(lines[i].rstrip())]
    ends = [i for i in outside if lines[i].rstrip() == end]
    return begins, ends, open_fence


def normalised(notes):
    """Notes as Proxmox gives them back, whichever way it read them: each line's trailing whitespace and the trailing
    blank lines dropped."""
    return "\n".join(line.rstrip() for line in str(notes or "").split("\n")).rstrip()


def block(marker, metadata, text):
    """The block: the begin marker with its key=value metadata, the text, the end marker."""
    pairs = "".join(f" {key}={value}" for key, value in (metadata or {}).items())
    return f"<!-- {marker}:begin{pairs} -->\n{normalised(text)}\n{_end(marker)}"


def found(notes, marker):
    """The metadata of the block already in the Notes, {} without one (or with a broken one). Only pairs of the
    shapes the role writes are kept: whoever can edit the Notes could put anything there, and these reach the run's
    output and log, so a terminal's escape sequences never would."""
    lines = str(notes or "").split("\n")
    begins, ends, _ = markers(lines, marker)
    if len(begins) != 1 or len(ends) != 1 or begins[0] > ends[0]:
        return {}
    pairs = (_begin(marker).fullmatch(lines[begins[0]].rstrip()).group(1) or "").split()
    return {
        key: value
        for key, value in (pair.split("=", 1) for pair in pairs if "=" in pair)
        if META_KEY.fullmatch(key) and META_VALUE.fullmatch(value)
    }


def merged(notes, marker, new_block, limit):
    """{notes, problem}: the Notes with the block in place, appended after a blank line when there is none and replaced
    from marker to marker when there is one, every other line as it was read. A marker without its partner, or two
    blocks, is a problem, never a guess: whatever lies between could be the owner's. So are Notes that end inside a
    fenced code block, where an appended block would be text, never found again, and a result longer than Proxmox
    takes."""
    # Only the end is trimmed, as Proxmox trims a VM's: each line keeps its own trailing whitespace.
    current = str(notes or "").rstrip()
    lines = current.split("\n") if current else []
    starts, ends, open_fence = markers(lines, marker)
    if not starts and not ends and open_fence:
        return {
            "notes": current,
            "problem": "its Notes end inside a code block (a ``` or ~~~ fence that is never closed), where the "
            f"{marker} block would be text; close it by hand",
        }
    if not starts and not ends:
        out = f"{current}\n\n{new_block}" if current else new_block
    elif len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0]:
        out = "\n".join(lines[: starts[0]] + new_block.split("\n") + lines[ends[0] + 1 :])
    else:
        return {
            "notes": current,
            "problem": f"its Notes hold a broken {marker} block ({len(starts)} begin markers and "
            f"{len(ends)} end markers); mend or remove it by hand",
        }
    if len(out) > limit:
        return {
            "notes": current,
            "problem": f"its Notes would be {len(out)} characters long, more than the {limit} Proxmox takes",
        }
    return {"notes": out, "problem": ""}


def has_text(text):
    """Whether there is a block to write: text with more than whitespace in it."""
    return isinstance(text, str) and text.strip() != ""


def input_errors(marker, metadata, text, node, vmids):
    """Each problem with the role's inputs, in its own sentence naming the variable. The marker is needed only with a
    block to write, so the defaults pass."""
    out = []
    if (marker != "" or has_text(text)) and (not isinstance(marker, str) or not MARKER.fullmatch(marker)):
        out.append(
            "proxmox_notes_marker must be a lower-case word of letters, digits and hyphens, such as the project's name."
        )
    if not isinstance(metadata, dict):
        out.append("proxmox_notes_metadata must be a dict of key=value pairs.")
    else:
        bad = [
            str(k) for k, v in metadata.items() if not META_KEY.fullmatch(str(k)) or not META_VALUE.fullmatch(str(v))
        ]
        if bad:
            out.append(
                f"proxmox_notes_metadata takes lower-case keys and one-word values of letters, digits and . _ "
                f"+ -; these are not: {', '.join(bad)}."
            )
    if not isinstance(text, str):
        out.append("proxmox_notes_text must be a string.")
    elif "-->" in text or "<!--" in text:
        out.append("proxmox_notes_text may not hold an HTML comment, which would end the block's markers.")
    if not isinstance(node, bool):
        out.append("proxmox_notes_node must be true or false.")
    if isinstance(vmids, str) or not isinstance(vmids, list):
        out.append("proxmox_notes_vmids must be a list of VMIDs.")
    else:
        bad = [str(v) for v in vmids if isinstance(v, bool) or not VMID.fullmatch(str(v))]
        if bad:
            out.append(f"proxmox_notes_vmids takes VMIDs from 100 up; these are not: {', '.join(bad)}.")
    return out


def plan(marker, metadata, text, node, vmids, qemu, configs, node_config):
    """What to write: {writes, missing, problems}.

    `qemu` is the node's VMs as pvesh lists them, `configs` {vmid: configuration} for those of `vmids` that exist, and
    `node_config` the node's configuration (None when the node is not written). Each write is {target (node or vm),
    vmid, label, notes, digest, before, after}: the whole Notes to set, the configuration's digest as read (empty
    when Proxmox gave none), which the write passes so that Proxmox refuses it if anyone changed the configuration
    since, and the block's metadata before (empty for a new block) and after. `missing` is the VMIDs of `vmids` the
    node has no VM for: a dry run before they are created stamps them once they are, and a real run refuses them. A
    VMID named twice is written once, and text of whitespace alone writes nothing.
    """
    if not has_text(text):
        return {"writes": [], "missing": [], "problems": []}
    new_block = block(marker, metadata, text)
    after = {str(key): str(value) for key, value in (metadata or {}).items()}
    have = {str(vm.get("vmid")) for vm in qemu or []}
    writes, problems = [], []

    def consider(target, vmid, label, config, limit):
        notes = config.get("description", "")
        result = merged(notes, marker, new_block, limit)
        if result["problem"]:
            problems.append(f"{label}: {result['problem']}")
        elif normalised(result["notes"]) != normalised(notes):
            writes.append(
                {
                    "target": target,
                    "vmid": vmid,
                    "label": label,
                    "notes": result["notes"],
                    "digest": str(config.get("digest") or ""),
                    "before": found(notes, marker),
                    "after": after,
                }
            )

    if node and node_config is not None:
        consider("node", 0, "the node", node_config or {}, NODE_LIMIT)
    missing = []
    for vmid in dict.fromkeys(str(v) for v in vmids or []):
        if vmid not in have:
            missing.append(int(vmid))
            continue
        consider("vm", int(vmid), f"VM {vmid}", (configs or {}).get(vmid) or {}, VM_LIMIT)
    return {"writes": writes, "missing": missing, "problems": problems}


def configs(results):
    """{vmid: configuration} from the registered results of reading each VM's configuration."""
    return {str(r["item"]): json.loads(r["stdout"]) for r in results or [] if "stdout" in r}


def summary(write):
    """One write in a few words, for the task's output: whose Notes, and how the block's metadata changes."""
    before = write.get("before") or {}
    after = {str(k): str(v) for k, v in (write.get("after") or {}).items()}
    if not before:
        pairs = " ".join(f"{k}={v}" for k, v in after.items())
        return f"{write['label']}: a new block{', ' + pairs if pairs else ''}"
    changes = [f"{k} {before.get(k, 'unset')} to {v}" for k, v in after.items() if before.get(k) != v]
    changes += [f"{k} {v} to unset" for k, v in before.items() if k not in after]
    return f"{write['label']}: {'; '.join(changes) if changes else 'its text'}"


def would(writes, marker, missing):
    """What a dry run would write, a sentence each."""
    out = [f"Would write the {marker} block in the Notes of {summary(w)}" for w in writes or []]
    if missing:
        out.append(f"Would write the {marker} block in the Notes of VM {', '.join(map(str, missing))} once created")
    return out


def failures(results):
    """Each write Proxmox refused, by whose Notes it was and Proxmox's own last line (a VM locked by a backup, a
    configuration changed since it was read), never with the Notes themselves, which a failed task would print."""
    out = []
    for r in results or []:
        if r.get("skipped") or r.get("rc", 0) == 0:
            continue
        said = [line.strip() for line in str(r.get("stderr") or r.get("msg") or "").splitlines() if line.strip()]
        out.append(f"{r['item']['label']}: {said[-1] if said else 'rc ' + str(r.get('rc'))}")
    return out


class FilterModule:
    def filters(self):
        return {
            "proxmox_notes_configs": configs,
            "proxmox_notes_failures": failures,
            "proxmox_notes_has_text": has_text,
            "proxmox_notes_input_errors": input_errors,
            "proxmox_notes_plan": plan,
            "proxmox_notes_summary": summary,
            "proxmox_notes_would": would,
        }
