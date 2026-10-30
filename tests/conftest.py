"""What every test shares: each ansible-playbook a test starts prints with the default stdout callback, whichever
ansible.cfg it reads; and, for a just test drawn live, each test's start and end.

The repo's ansible.cfg names callback_plugins/homeserver.py, which a scratch tree holding a copy of ansible.cfg
alone does not have, and Ansible stops a run whose stdout callback it cannot load ("Could not load 'homeserver'
callback plugin"). No test reads that callback's output by accident either: a test of a callback, or of a recipe
that chooses one, sets ANSIBLE_STDOUT_CALLBACK itself (tests/unit/test_callback.py, test_status.py).

A just test drawn at a terminal (scripts/live.py, filter_plugins/live_commands.py's pytest reader) names a file in
HOMESERVER_LIVE_PYTEST: pytest writes there, as JSON lines, how many tests it collected, each test's start and its end
with its outcome and, for a failure, what pytest said of it. Only the controller writes, which xdist's workers report
to; without the variable nothing is written, and no process a test starts inherits it.
"""

import json
import os

import pytest

os.environ["ANSIBLE_STDOUT_CALLBACK"] = "default"

# Taken out of the environment as it is read: a pytest a test starts in turn (test_github.py's collection) must not
# write its tests among this run's.
LIVE = os.environ.pop("HOMESERVER_LIVE_PYTEST", "")
# Whether this process is pytest's controller, not one of xdist's workers: set when pytest configures it.
CONTROLLER = []
# Whether the tests collected have been said: under xdist, by the first worker to collect them.
COUNTED = []
# How much of a failure's report the reader is given: its end, where the assertion is.
SAID = 6000


def _say(event):
    if LIVE and CONTROLLER:
        with open(LIVE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")


def pytest_configure(config):
    if not hasattr(config, "workerinput"):
        CONTROLLER.append(True)


def pytest_collection_finish(session):
    # Under xdist the controller collects nothing itself: its workers say what they collected (below).
    if session.items:
        _say({"count": len(session.items)})


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node, ids):
    # Every worker collects the same tests: the first to say so gives the count.
    if not COUNTED:
        COUNTED.append(True)
        _say({"count": len(ids)})


def pytest_runtest_logstart(nodeid, location):
    _say({"start": nodeid})


def pytest_runtest_logreport(report):
    # A test ends with its call, or with a setup that failed or skipped it, or a teardown that failed after it.
    ends = report.when == "call" or (report.when == "setup" and not report.passed)
    if ends or (report.when == "teardown" and report.failed):
        said = report.longreprtext[-SAID:] if report.failed else ""
        _say({"end": report.nodeid, "outcome": report.outcome, "said": said})
