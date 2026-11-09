"""The CLAUDE.md files stay small enough for Claude Code to read whole.

Claude Code loads the root CLAUDE.md into every session, and a CLAUDE.md below it once Claude works on a file under its
directory. Its limit for one file is 5% of the model's context window at three characters a token, and never less than
40,000 characters, the limit on a 200k-token window. Over it, a file loaded at launch is still read whole but warned
about, and a file loaded on demand is cut off there, so Claude never reads its end. The root file keeps under the
floor, so no model warns, and every other one keeps a margin under it. Each one below the root is named in the root's
"Where the rest is written", so a session knows it exists before it loads.
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

ROOT_LIMIT = 40_000
NESTED_LIMIT = 35_000
# Its whole text is an import of filter_plugins/CLAUDE.md, which the root names.
UNNAMED = {"callback_plugins/CLAUDE.md"}


def nested() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--", "*/CLAUDE.md"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return sorted(path for path in listed.split("\0") if path)


def test_the_root_claude_md_stays_under_the_smallest_limit():
    size = len((ROOT / "CLAUDE.md").read_text())
    assert size <= ROOT_LIMIT, (
        f"CLAUDE.md is {size} characters, over {ROOT_LIMIT}: move what only one part of the tree needs into that "
        "part's README.md or CLAUDE.md"
    )


def test_every_other_claude_md_stays_under_the_limit_it_is_cut_at():
    large = {path: size for path in nested() if (size := len((ROOT / path).read_text())) > NESTED_LIMIT}
    assert large == {}, f"over {NESTED_LIMIT} characters, which Claude Code would cut off on a 200k-token window"


def test_the_root_names_every_other_claude_md():
    text = (ROOT / "CLAUDE.md").read_text()
    unnamed = [path for path in nested() if path not in UNNAMED and f"`{path}`" not in text]
    assert unnamed == [], 'name it under "Where the rest is written" in CLAUDE.md'
