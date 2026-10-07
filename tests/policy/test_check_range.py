"""scripts/ci/check-range.sh catches what a change adds, however the change tries to hide it.

Each test builds a small git history in a temporary directory, with this repo's .gitleaks.toml on its
main branch, and runs the script as CI does. The address is built from split literals, so this file
holds none. Marked `gitleaks`, like test_gitleaks_rules.py.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci" / "check-range.sh"
ADDRESS = ".".join(["10", "20", "30", "40"])

pytestmark = pytest.mark.gitleaks


class Repo:
    def __init__(self, path):
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.write(".gitleaks.toml", (ROOT / ".gitleaks.toml").read_text())
        self.write("notes.md", "first line\nsecond line\n")
        self.commit("start")
        self.git("update-ref", "refs/remotes/origin/main", "main")

    def git(self, *args, check=True):
        identity = {
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        result = subprocess.run(
            ["git", "-c", "commit.gpgsign=false", *args],
            cwd=self.path,
            env={**os.environ, **identity},
            capture_output=True,
            text=True,
        )
        if check and result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def write(self, name, text):
        (self.path / name).parent.mkdir(parents=True, exist_ok=True)
        (self.path / name).write_text(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def sha(self, ref):
        return self.git("rev-parse", ref)

    def check(self, base, head="HEAD", first_parent=False, require=True, ci=True):
        env = {k: v for k, v in os.environ.items() if k != "CI"}
        env.update(
            BASE=base, HEAD=head, FIRST_PARENT="1" if first_parent else "0", REQUIRE_COMMITS="1" if require else "0"
        )
        if ci:
            env["CI"] = "true"
        return subprocess.run(["bash", str(SCRIPT)], cwd=self.path, env=env, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, gitleaks):
    return Repo(tmp_path)


def caught(result, rule="private-ipv4"):
    return result.returncode != 0 and rule in result.stdout


def branch_with(repo, files):
    main = repo.sha("main")
    repo.git("switch", "-q", "-c", "feature")
    for name, text in files.items():
        repo.write(name, text)
    repo.commit("change")
    return main


def test_a_clean_change_passes(repo):
    main = branch_with(repo, {"clean.md": "nothing to see\n"})
    result = repo.check(main)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Checking 1 commit(s)" in result.stdout


def test_an_address_in_a_new_commit_is_caught(repo):
    main = branch_with(repo, {"nas.md": f"nas {ADDRESS}\n"})
    assert caught(repo.check(main))


def test_an_address_added_while_resolving_a_merge_conflict_is_caught(repo):
    repo.git("switch", "-q", "-c", "feature")
    repo.write("notes.md", "first line\nfeature's line\n")
    repo.commit("feature edit")
    repo.git("switch", "-q", "main")
    repo.write("notes.md", "first line\nmain's line\n")
    main = repo.commit("main edit")
    repo.git("switch", "-q", "feature")
    repo.git("merge", "main", check=False)  # conflicts on notes.md
    repo.write("notes.md", f"first line\nresolved, nas {ADDRESS}\n")
    repo.commit("merge main")
    assert caught(repo.check(main))
    # A dispatched run has no base and checks from origin/main instead.
    repo.git("update-ref", "refs/remotes/origin/main", main)
    assert caught(repo.check(""))


def test_a_push_to_main_checks_each_merge_on_the_first_parent_line(repo):
    before = branch_with(repo, {"nas.md": f"nas {ADDRESS}\n"})
    repo.git("switch", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    assert caught(repo.check(before, first_parent=True))


def test_a_change_cannot_weaken_its_own_rules(repo):
    rules = (ROOT / ".gitleaks.toml").read_text()
    weakened = rules.replace('id = "private-ipv4"', 'id = "private-ipv4"\npath = """^nowhere$"""')
    assert weakened != rules
    main = branch_with(repo, {".gitleaks.toml": weakened, "nas.md": f"nas {ADDRESS}\n"})
    assert caught(repo.check(main))


def test_an_inline_gitleaks_allow_is_ignored(repo):
    main = branch_with(repo, {"nas.md": f"nas {ADDRESS}  # gitleaks:allow\n"})
    assert caught(repo.check(main))


def test_a_committed_gitleaksignore_is_ignored_in_ci(repo):
    main = branch_with(repo, {"nas.md": f"nas {ADDRESS}\n"})
    commit = repo.sha("HEAD")
    repo.write(".gitleaksignore", f"{commit}:nas.md:private-ipv4:1\n")
    repo.commit("ignore it")
    assert repo.check(main, ci=False).returncode == 0  # the ignore file works where it is honoured...
    assert caught(repo.check(main))  # ...and CI deletes it before scanning


def test_a_no_diff_attribute_does_not_hide_a_file(repo):
    main = branch_with(repo, {".gitattributes": "nas.txt -diff\n", "nas.txt": f"nas {ADDRESS}\n"})
    assert caught(repo.check(main))


def test_a_branch_behind_its_target_is_checked_from_the_merge_base(repo):
    branch_with(repo, {"nas.md": f"nas {ADDRESS}\n"})
    repo.git("switch", "-q", "main")
    repo.write("later.md", "main moved on\n")
    tip = repo.commit("main moves on")  # a pull request's base: not an ancestor of the head
    result = repo.check(tip, head=repo.sha("feature"))
    assert caught(result)
    assert "Checking 1 commit(s)" in result.stdout


def test_an_empty_range_fails_only_when_commits_are_required(repo):
    main = repo.sha("main")
    assert repo.check(main).returncode != 0
    assert repo.check(main, require=False).returncode == 0
