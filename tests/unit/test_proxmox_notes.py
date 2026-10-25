"""roles/proxmox_notes's filters: the block it keeps in a Proxmox node's and its VMs' Notes, written beside whatever
else they hold, and what it writes, worked out from what the role read.

Notes come back from Proxmox in the forms Proxmox VE 9.2 stores them in (qemu-server 9.2.11 and pve-manager's
NodeConfig, read from a host's installed code): a VM's trimmed at the end, a node's with each line's trailing spaces
trimmed and a newline after each, nothing for a node with no configuration file.
"""

import importlib.util
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[2] / "roles" / "proxmox_notes" / "filter_plugins" / "proxmox_notes.py"
_spec = importlib.util.spec_from_file_location("proxmox_notes_filters", PLUGIN)
pn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pn)

COMMIT = "5de78852f353b2353ba33a8b8dcbc7217928bb1c"
META = {"commit": COMMIT, "describe": "2026.11.0-3-g5de78852f353", "date": "2026-10-24"}
TEXT = (
    "Deployed from homeserver `2026.11.0-3-g5de78852f353`, committed 2026-10-24. `just deploy kubernetes` deploys it,"
    " and `just status` shows what it runs now."
)
BLOCK = pn.block("homeserver", META, TEXT)


def test_the_block():
    assert (
        f"<!-- homeserver:begin commit={COMMIT} describe=2026.11.0-3-g5de78852f353 date=2026-10-24 -->\n"
        f"{TEXT}\n<!-- homeserver:end -->"
    ) == BLOCK
    assert pn.block("homeserver", {}, "x\n\n") == "<!-- homeserver:begin -->\nx\n<!-- homeserver:end -->"


def test_notes_are_compared_as_proxmox_gives_them_back():
    assert pn.normalised("a  \n\nb\t\n\n") == "a\n\nb"
    assert pn.normalised(None) == pn.normalised("") == ""


def test_a_block_is_appended_after_the_owners_text():
    assert pn.merged("", "homeserver", BLOCK, pn.VM_LIMIT) == {"notes": BLOCK, "problem": ""}
    out = pn.merged("Rack 2, bottom shelf.\n", "homeserver", BLOCK, pn.VM_LIMIT)
    assert out == {"notes": "Rack 2, bottom shelf.\n\n" + BLOCK, "problem": ""}


def test_every_other_line_is_written_as_it_was_read():
    # A VM's Notes keep each line's trailing whitespace (two spaces are a Markdown line break), and only the end is
    # trimmed, as Proxmox trims it; what was read is compared with what would be written as Proxmox gives it back.
    owner = "Two spaces end this line,  \nand a tab this one.\t\nLast."
    assert pn.merged(owner + "\n\n", "homeserver", BLOCK, pn.VM_LIMIT)["notes"] == owner + "\n\n" + BLOCK
    old = pn.block("homeserver", {"commit": "0" * 40}, "Old.")
    out = pn.merged(f"{owner}\n\n{old}\nBelow,  \nthe owner's.", "homeserver", BLOCK, pn.VM_LIMIT)
    assert out["notes"] == f"{owner}\n\n{BLOCK}\nBelow,  \nthe owner's."
    configs = {"300": {"description": f"{owner}\n\n{BLOCK}"}}
    assert pn.plan("homeserver", META, TEXT, False, [300], [{"vmid": 300}], configs, None)["writes"] == []


def test_a_block_is_replaced_from_marker_to_marker_alone():
    old = pn.block("homeserver", {"commit": "0" * 40}, "Old text,\nover two lines.")
    notes = f"Above.\n\n{old}\n\nBelow, the owner's.\n<!-- another:begin x=1 -->\nTheirs.\n<!-- another:end -->"
    out = pn.merged(notes, "homeserver", BLOCK, pn.VM_LIMIT)
    assert out["problem"] == ""
    assert out["notes"] == notes.replace(old, BLOCK)
    assert pn.found(out["notes"], "another") == {"x": "1"}


def test_a_broken_block_is_refused_never_guessed():
    for notes in (
        "<!-- homeserver:begin commit=0 -->\nhalf a block",
        "half a block\n<!-- homeserver:end -->",
        f"{BLOCK}\n{BLOCK}",
        "<!-- homeserver:end -->\n<!-- homeserver:begin -->",
    ):
        out = pn.merged(notes, "homeserver", BLOCK, pn.VM_LIMIT)
        assert out["problem"].startswith("its Notes hold a broken homeserver block"), notes
        assert out["notes"] == notes.rstrip()


def test_notes_longer_than_proxmox_takes_are_refused():
    out = pn.merged("x" * (pn.VM_LIMIT - 10), "homeserver", BLOCK, pn.VM_LIMIT)
    assert "more than the 8192 Proxmox takes" in out["problem"]
    assert pn.merged("x" * (pn.VM_LIMIT - 10), "homeserver", BLOCK, pn.NODE_LIMIT)["problem"] == ""


def test_the_metadata_already_there():
    assert pn.found(f"Owner's.\n\n{BLOCK}", "homeserver") == META
    assert pn.found("Owner's.", "homeserver") == {}
    assert pn.found(f"{BLOCK}\n{BLOCK}", "homeserver") == {}
    # Whoever can edit the Notes can write anything there, and these reach the run's output: only the role's shapes.
    odd = "<!-- homeserver:begin commit=abc date=\x1b[2K\x1b[1Gok: Key=x =y -->\nx\n<!-- homeserver:end -->"
    assert pn.found(odd, "homeserver") == {"commit": "abc"}


def test_markers_are_lines_of_their_own_outside_code():
    # An indented marker, or one inside a fenced example such as the README's, is Markdown's text, never the block.
    example = (
        f"An example:\n\n```markdown\n{BLOCK}\n```\n\n    {BLOCK.splitlines()[0]}\n~~~\n<!-- homeserver:end -->\n~~~"
    )
    assert pn.found(example, "homeserver") == {}
    out = pn.merged(example, "homeserver", BLOCK, pn.VM_LIMIT)
    assert out == {"notes": f"{example}\n\n{BLOCK}", "problem": ""}
    assert pn.found(out["notes"], "homeserver") == META
    # A fence of four opens and only four or more close it; one followed by text is no closing fence.
    assert pn.markers(["````", "```", BLOCK.splitlines()[0], "````"], "homeserver") == ([], [], False)
    assert pn.markers(["```", "``` x", BLOCK.splitlines()[0]], "homeserver") == ([], [], True)


def test_notes_ending_inside_a_code_block_are_refused():
    # An appended block would be text there, never found again, and appended anew on each run.
    out = pn.merged("Notes:\n```\nqm list", "homeserver", BLOCK, pn.VM_LIMIT)
    assert out["problem"].startswith("its Notes end inside a code block"), out
    assert pn.merged(f"{BLOCK}\n```\nqm list", "homeserver", BLOCK, pn.VM_LIMIT)["problem"] == ""


def test_input_errors():
    assert pn.input_errors("homeserver", META, TEXT, True, [300, 301]) == []
    assert pn.input_errors("homeserver", {}, "", False, []) == []
    assert pn.input_errors("", {}, "", False, []) == [], "the role's own defaults"
    assert pn.input_errors("", {}, "Text.", False, [300])[0].startswith("proxmox_notes_marker must be")
    # A trailing newline would split the begin marker over two lines, which the next run could not read.
    assert len(pn.input_errors("homeserver\n", {"commit": "abc\n"}, TEXT, False, ["300\n"])) == 3
    errors = pn.input_errors("Home Server", {"commit": "a b"}, "a <!-- comment -->", "yes", "300")
    assert len(errors) == 5, errors
    assert any("proxmox_notes_marker" in e for e in errors)
    assert any("proxmox_notes_metadata" in e and "commit" in e for e in errors)
    assert any("HTML comment" in e for e in errors)
    assert any("proxmox_notes_node must be true or false" in e for e in errors)
    assert any("proxmox_notes_vmids must be a list" in e for e in errors)
    assert pn.input_errors("homeserver", {"ok": "1"}, "", False, [99, True, "301"]) == [
        "proxmox_notes_vmids takes VMIDs from 100 up; these are not: 99, True."
    ]


def test_the_plan():
    qemu = [{"vmid": 300}, {"vmid": 301}]
    configs = {"300": {"name": "cp", "description": "Owner's."}, "301": {"description": BLOCK}}
    node = {"description": "Rack 2.  \n", "digest": "x"}
    out = pn.plan("homeserver", META, TEXT, True, [300, 301, 302], qemu, configs, node)
    assert out["missing"] == [302] and out["problems"] == []
    assert [(w["target"], w["vmid"], w["label"]) for w in out["writes"]] == [
        ("node", 0, "the node"),
        ("vm", 300, "VM 300"),
    ]
    assert out["writes"][0]["notes"] == "Rack 2.\n\n" + BLOCK and out["writes"][0]["before"] == {}
    assert out["writes"][1]["notes"] == "Owner's.\n\n" + BLOCK


def test_the_plan_writes_nothing_the_notes_hold_already():
    # A node's Notes come back with each line trimmed and a newline after the last.
    node = {"description": "Rack 2.\n\n" + BLOCK + "\n"}
    configs = {"300": {"description": BLOCK}}
    out = pn.plan("homeserver", META, TEXT, True, [300], [{"vmid": 300}], configs, node)
    assert out == {"writes": [], "missing": [], "problems": []}


def test_the_plan_writes_each_vm_once_and_nothing_for_blank_text():
    configs = {"300": {"description": "", "digest": "d300"}}
    once = pn.plan("homeserver", META, TEXT, False, [300, "300"], [{"vmid": 300}], configs, None)
    assert [(w["vmid"], w["digest"]) for w in once["writes"]] == [(300, "d300")]
    assert pn.plan("homeserver", META, "  \n ", True, [300], [{"vmid": 300}], configs, {}) == {
        "writes": [],
        "missing": [],
        "problems": [],
    }


def test_each_write_carries_the_digest_it_read():
    # So Proxmox refuses it if anyone changed the configuration since; a node with no configuration file has none.
    configs = {"300": {"description": "Owner's.", "digest": "d300"}}
    out = pn.plan("homeserver", META, TEXT, True, [300], [{"vmid": 300}], configs, {})
    assert [(w["target"], w["digest"]) for w in out["writes"]] == [("node", ""), ("vm", "d300")]


def test_the_plan_names_a_broken_block_by_its_machine():
    configs = {"300": {"description": "<!-- homeserver:begin -->"}}
    out = pn.plan("homeserver", META, TEXT, False, [300], [{"vmid": "300"}], configs, None)
    assert out["writes"] == [] and out["problems"][0].startswith("VM 300: its Notes hold a broken homeserver block")


def test_what_the_output_says():
    new = {"label": "VM 300", "before": {}, "after": {"commit": COMMIT}}
    moved = {"label": "VM 301", "before": {"commit": "0" * 40, "date": "2026-10-24"}, "after": META}
    assert pn.summary(new) == f"VM 300: a new block, commit={COMMIT}"
    assert pn.summary(moved) == (f"VM 301: commit {'0' * 40} to {COMMIT}; describe unset to 2026.11.0-3-g5de78852f353")
    assert pn.summary({"label": "the node", "before": META, "after": META}) == "the node: its text"
    # Values are compared as the Notes hold them, as text, and a key the metadata no longer has is said too.
    assert pn.summary({"label": "VM 1", "before": {"n": "5", "x": "1"}, "after": {"n": 5}}) == "VM 1: x 1 to unset"
    assert pn.would([new], "homeserver", [302]) == [
        f"Would write the homeserver block in the Notes of VM 300: a new block, commit={COMMIT}",
        "Would write the homeserver block in the Notes of VM 302 once created",
    ]


def test_a_refused_write_is_said_without_the_notes():
    results = [
        {"item": {"label": "VM 300"}, "rc": 0},
        {"item": {"label": "VM 301"}, "skipped": True},
        {
            "item": {"label": "VM 310"},
            "rc": 25,
            "stderr": "update VM 310: -description Owner's\nVM is locked (backup)\n",
        },
        {"item": {"label": "the node"}, "rc": 2, "stderr": "", "msg": "[Errno 2] No such file or directory"},
    ]
    assert pn.failures(results) == ["VM 310: VM is locked (backup)", "the node: [Errno 2] No such file or directory"]


def test_the_configurations_read():
    results = [{"item": "300", "stdout": '{"description": "x"}'}, {"item": "301", "skipped": True}]
    assert pn.configs(results) == {"300": {"description": "x"}}
