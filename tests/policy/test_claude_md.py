"""The CLAUDE.md files stay small enough for Claude Code to read whole.

Claude Code loads the root CLAUDE.md into every session, and a CLAUDE.md below it once Claude works on a file under its
directory. Its limit for one file is 5% of the model's context window at three characters a token, and never less than
40,000 characters, the limit on a 200k-token window. Over it, a file loaded at launch is still read whole but warned
about, and a file loaded on demand is cut off there, so Claude never reads its end. The root file keeps under the
floor, so no model warns, and every other one keeps a margin under it. Each one below the root is named in the root's
"Where the rest is written", so a session knows it exists before it loads.

A rule in .claude/rules/ loads once Claude works on a file its paths match. A README loads only when Claude reads it,
which a line in a CLAUDE.md alone got done in 5 of 12 measured runs, and a rule per role, saying so in words, in 11 of
12. A rule must name its paths, or it loads in every session, and must import nothing: Claude Code loads what a rule
imports at the start of every session, whatever its paths say, which with one import per role cost more than the
163.8k-character CLAUDE.md these rules replace (measured on Claude Code 2.1.311).
"""

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

ROOT_LIMIT = 40_000
NESTED_LIMIT = 35_000
# Its whole text is an import of filter_plugins/CLAUDE.md, which the root names.
UNNAMED = {"callback_plugins/CLAUDE.md"}
# Claude Code's own pattern for an import, which it skips in code spans and fenced blocks.
IMPORT = re.compile(r"(?:^|\s)@((?:[^\s\\]|\\ )+)")
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.DOTALL)


def tracked(pathspec: str) -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", pathspec], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return sorted(path for path in listed.split("\0") if path)


def nested() -> list[str]:
    return tracked("*/CLAUDE.md")


def rules() -> dict[str, tuple[dict, str]]:
    """Each rule's frontmatter and body, by its path."""
    found = {}
    for path in tracked(".claude/rules/*.md"):
        text = (ROOT / path).read_text()
        parts = text.split("---\n", 2)
        framed = text.startswith("---\n") and len(parts) == 3
        found[path] = (yaml.safe_load(parts[1]) or {}, parts[2]) if framed else ({}, text)
    return found


def test_the_root_claude_md_stays_under_the_smallest_limit():
    size = len((ROOT / "CLAUDE.md").read_text())
    assert size <= ROOT_LIMIT, (
        f"CLAUDE.md is {size} characters, over {ROOT_LIMIT}: move what only one part of the tree needs into that "
        "part's README.md or CLAUDE.md"
    )


def test_every_other_claude_md_stays_under_the_limit_it_is_cut_at():
    files = nested() + list(rules())
    large = {path: size for path in files if (size := len((ROOT / path).read_text())) > NESTED_LIMIT}
    assert large == {}, f"over {NESTED_LIMIT} characters, which Claude Code would cut off on a 200k-token window"


def test_the_root_names_every_other_claude_md():
    text = (ROOT / "CLAUDE.md").read_text()
    unnamed = [path for path in nested() if path not in UNNAMED and f"`{path}`" not in text]
    assert unnamed == [], 'name it under "Where the rest is written" in CLAUDE.md'


def test_every_role_has_a_rule_that_says_to_read_its_readme():
    found = rules()
    missing = []
    for role in sorted(path.parent.name for path in (ROOT / "roles").glob("*/README.md")):
        front, body = found.get(f".claude/rules/{role}.md", ({}, ""))
        if f"`roles/{role}/README.md`" not in body or f"roles/{role}/**" not in front.get("paths", []):
            missing.append(role)
    assert missing == [], "each role needs a .claude/rules/<role>.md, on its files, that names its README"


def test_every_rule_names_paths_and_each_matches_a_file():
    stale = {}
    for path, (front, _) in rules().items():
        paths = front.get("paths")
        assert paths, f"{path} names no paths, so it would load in every session"
        unmatched = [pattern for pattern in paths if not any(ROOT.glob(pattern))]
        if unmatched:
            stale[path] = unmatched
    assert stale == {}, "these paths match nothing, so the rule never loads: a file was moved or renamed"


def test_no_rule_imports_a_file():
    imports = {}
    for path, (_, body) in rules().items():
        found = IMPORT.findall(CODE.sub("", body))
        if found:
            imports[path] = found
    assert imports == {}, "Claude Code loads what a rule imports in every session, whatever its paths: say it in words"
