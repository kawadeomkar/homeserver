"""The ruleset body and the workflow agree on the gate.

.github/rulesets/main.json is applied to the "Protect main" ruleset by hand, and GitHub never reads the file.
If it named a check the workflow does not report, every pull request would wait on it forever; if the workflow
added a job that ci-success does not wait for, that job could fail and the pull request still merge.
"""

import json
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
