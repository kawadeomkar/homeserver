"""make status's stdout callback: one line on what was read, then the report, and none of Ansible's task output."""

DOCUMENTATION = r"""
name: homeserver_status
type: stdout
short_description: make status's report, without Ansible's task output
description:
  - Prints one line saying, for each host the reading play ran on, whether it answered and how long it took, then
    the text of the task that shows the report (status.yml, roles/status's report entry point).
  - Prints nothing on standard output when STATUS_QUIET is set, for make status JSON=1, which prints the report's
    file instead.
  - Prints every failure and every unreachable host on standard error, and nothing else of Ansible's.
requirements:
  - Chosen with ANSIBLE_STDOUT_CALLBACK=homeserver_status, as make status does; Ansible finds it beside status.yml.
"""

import os
import time

from ansible.plugins.callback import CallbackBase

# What the report calls each inventory group's host.
LABELS = {"proxmox": "proxmox", "nas": "nas", "kubernetes": "cluster"}
# The task whose message is the report, and the play that reads the hosts.
REPORT_TASK = "Show the report"
READING_PLAY = "Read the machines"


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "homeserver_status"

    def __init__(self):
        super().__init__()
        self._quiet = bool(os.environ.get("STATUS_QUIET"))
        self._reading = False
        self._started = 0.0
        self._hosts = {}

    def _label(self, host):
        groups = [g.get_name() for g in host.get_groups()]
        return next((LABELS[g] for g in groups if g in LABELS), host.get_name())

    def _seen(self, result, state):
        if not self._reading:
            return
        host = result._host
        entry = self._hosts.setdefault(host.get_name(), {"label": self._label(host), "state": "ok"})
        entry["seconds"] = time.monotonic() - self._started
        if entry["state"] == "ok":
            entry["state"] = state

    def _flush(self):
        if self._reading and self._hosts and not self._quiet:
            parts = [f"{h['label']} {h['state']} {h['seconds']:.1f} s" for h in self._hosts.values()]
            self._display.display("reading  " + " · ".join(parts) + "\n")
        self._reading, self._hosts = False, {}

    def v2_playbook_on_play_start(self, play):
        self._flush()
        self._reading = play.get_name() == READING_PLAY
        self._started = time.monotonic()

    def v2_runner_on_ok(self, result):
        self._seen(result, "ok")
        if result._task.get_name().endswith(REPORT_TASK) and not self._quiet:
            self._display.display(result._result.get("msg", ""))

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if ignore_errors:
            self._seen(result, "ok")
            return
        self._seen(result, "failed")
        message = result._result.get("msg") or result._result.get("stderr") or "failed"
        self._display.display(f"{result._host.get_name()}: {result._task.get_name()}: {message}", stderr=True)

    def v2_runner_on_unreachable(self, result):
        self._seen(result, "did not answer")
        message = result._result.get("msg", "unreachable")
        self._display.display(f"{result._host.get_name()} did not answer: {message}", stderr=True)

    def v2_playbook_on_stats(self, stats):
        self._flush()
