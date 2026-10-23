"""The repository runs through just (the justfile), and nothing tracked still names a make command or runs make as a
program: a change written before just, merged after it, would otherwise bring make's commands back into what the code
tells a person to run, or run make where it may not be installed (make init once ran make vault as a subprocess).

A make command is make followed by one of the Makefile's old targets, -k or TYPE=, so the English verb passes. Running
make is the word as an argument list's first item, or a git hook's entry. docs/ is git-ignored, so never read here.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

TARGETS = "help|install|init|lock|vault|lint|test|fix|check|deploy|suspend|resume|talos-upgrade|etcd-snapshot"
COMMAND = re.compile(rf"\bmake (-k |TYPE=|({TARGETS})\b)")
PROGRAM = re.compile(r"""\[\s*["']make["']|\bentry:\s*make\b""")
# This file, which spells the patterns out.
EXEMPT = {"tests/policy/test_no_make.py"}


def tracked() -> list[str]:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [path for path in listed.split("\0") if path and path not in EXEMPT]


def test_nothing_names_a_make_command_or_runs_make():
    found = []
    for path in tracked():
        try:
            text = (ROOT / path).read_text()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if COMMAND.search(line) or PROGRAM.search(line):
                found.append(f"{path}:{number}: {line.strip()}")
    assert not found, "make is gone; name the just recipe instead:\n" + "\n".join(found)
