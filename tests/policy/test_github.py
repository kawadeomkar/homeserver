"""The ruleset body and the workflow agree on the gate.

.github/rulesets/main.json is applied to the "Protect main" ruleset by hand, and GitHub never reads the file.
If it named a check the workflow does not report, every pull request would wait on it forever; if the workflow
added a job that ci-success does not wait for, that job could fail and the pull request still merge. ci-success
also lets a job be skipped only if it is skipped on pushes alone, so no pull request can skip it.
"""

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GITHUB_ACTIONS_APP = 15368


def ruleset():
    return json.loads((ROOT / ".github" / "rulesets" / "main.json").read_text())


def workflow():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())


def test_ruleset_requires_the_gate_job_from_github_actions():
    rules = {rule["type"]: rule.get("parameters", {}) for rule in ruleset()["rules"]}
    required = rules["required_status_checks"]
    gate = workflow()["jobs"]["ci-success"]
    assert required["required_status_checks"] == [{"context": gate["name"], "integration_id": GITHUB_ACTIONS_APP}]
    assert required["strict_required_status_checks_policy"] is True


def test_ruleset_protects_the_default_branch():
    body = ruleset()
    assert body["enforcement"] == "active"
    assert body["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    assert body["bypass_actors"] == []
    assert {"deletion", "non_fast_forward", "pull_request"} <= {rule["type"] for rule in body["rules"]}


def test_gate_waits_for_every_other_job_even_when_one_fails():
    jobs = workflow()["jobs"]
    gate = jobs["ci-success"]
    assert set(gate["needs"]) == set(jobs) - {"ci-success"}
    assert gate["if"] == "always()"


def test_gate_allows_skipping_only_jobs_that_run_on_every_pull_request():
    jobs = workflow()["jobs"]
    (gate,) = [step for step in jobs["ci-success"]["steps"] if step["uses"].startswith("re-actors/alls-green@")]
    skippable = {name.strip() for name in gate["with"].get("allowed-skips", "").split(",") if name.strip()}
    conditional = {name: job["if"] for name, job in jobs.items() if "if" in job and name != "ci-success"}
    assert conditional == dict.fromkeys(skippable, "github.event_name == 'pull_request'")


JUSTFILE = ROOT / "justfile"


def justfile_types(recipe):
    """The types the justfile's <recipe>_types lists, written as the pattern the recipe's arguments must match."""
    match = re.search(rf"^{recipe}_types := '([a-z0-9|-]+)'$", JUSTFILE.read_text(), re.M)
    assert match, f"the justfile has no {recipe}_types"
    return match.group(1).split("|")


def test_each_type_is_a_recipe_and_each_recipe_a_type():
    """lint, test and fix run <recipe>-<type> for each of their types and refuse any other, by the pattern their types
    argument takes from the list: a type without its recipe would fail only when run, and a recipe left out of the
    list would never run at all."""
    text = JUSTFILE.read_text()
    for recipe in ("lint", "test", "fix"):
        recipes = re.findall(rf"^{recipe}-([a-z0-9]+)(?: [^:\n]*)?:", text, re.M)
        assert recipes and sorted(recipes) == sorted(justfile_types(recipe)), recipe
        assert re.search(rf"^\[arg\('types', pattern={recipe}_types[ )]", text, re.M), recipe
    assert set(justfile_types("k8s")) <= set(justfile_types("lint"))


def test_ci_runs_every_lint_and_test_type():
    """just lint and just test each run every type without arguments, while CI splits them between its jobs; a type
    added to the justfile and to no job would run locally and never gate a pull request."""
    ran = {"lint": set(), "test": set()}
    for job in workflow()["jobs"].values():
        for step in job.get("steps", []):
            for line in step.get("run", "").splitlines():
                if not line.startswith("just "):
                    continue
                words = shlex.split(line)
                goal, types = words[1], [word for word in words[2:] if not word.startswith("-")]
                if goal in ran:
                    assert types, f"CI runs just {goal} without types, so its jobs would repeat each other: {line}"
                    for kind in types:
                        ran[goal].update(justfile_types("k8s") if kind == "k8s" else [kind])
    for goal, types in ran.items():
        assert types == set(justfile_types(goal)), goal


def test_every_job_installs_just_first_at_a_version_the_justfile_takes():
    """Every job's commands are just's, and just install fills the job's virtualenv, so just comes first, from
    scripts/ci/install-just.sh; and the pinned release is no older than the justfile's minimum-version, below which
    just refuses to read it."""
    flow = workflow()
    minimum = re.search(r"^set minimum-version := '([0-9.]+)'$", JUSTFILE.read_text(), re.M)
    assert minimum, "the justfile names the oldest just it takes"

    def version(text):
        return tuple(int(part) for part in text.split("."))

    assert version(flow["env"]["JUST_VERSION"]) >= version(minimum.group(1))
    assert {"JUST_SHA256_X86_64", "JUST_SHA256_AARCH64"} <= set(flow["env"])
    for name, job in flow["jobs"].items():
        runs = [step.get("run", "") for step in job.get("steps", [])]
        uses = [i for i, run in enumerate(runs) if re.search(r"(^|\s)just\s", run, re.M)]
        if uses:
            assert any(run == "scripts/ci/install-just.sh" for run in runs[: uses[0]]), name


def test_every_idempotence_test_runs_once():
    """The test job's matrix skips the tests marked cluster and the cluster job runs only those, split into shares
    that together hold each once, so each runs, on the matrix's Pythons or on one runner, and none twice."""
    jobs = workflow()["jobs"]

    def commands(job):
        return [step["run"] for step in jobs[job]["steps"] if "just test idempotence" in step.get("run", "")]

    assert commands("test") == ["just test idempotence --pytest-args='-m \"not cluster\"'"]
    assert commands("cluster") == ['just test idempotence --pytest-args="-m cluster --shard $SHARD"']
    shards = jobs["cluster"]["strategy"]["matrix"]["shard"]
    assert shards == [f"{k}/{len(shards)}" for k in range(1, len(shards) + 1)]

    def collect(*args):
        command = [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-m", "cluster"]
        result = subprocess.run([*command, *args, "tests/integration"], cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        return [line for line in result.stdout.splitlines() if "::" in line]

    shares = [test for shard in shards for test in collect("--shard", shard)]
    assert sorted(shares) == sorted(collect())
