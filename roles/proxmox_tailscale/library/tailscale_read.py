#!/usr/bin/python

DOCUMENTATION = r"""
---
module: tailscale_read
short_description: Read a node's Tailscale, its tailnet guard and its forwarding, all at once
description:
  - Runs the reads roles/proxmox_tailscale's status entry point works from, at the same time, and returns each one's
    exit status and output, C(tailscale status --json), C(tailscale get --json), C(tailscale serve status --json),
    C(nft list table inet tailnet_guard) and C(sysctl -n net.ipv4.ip_forward), so that they take one round trip to
    the host rather than five.
  - Takes no command to run: these five are the only ones it runs, and none changes anything, so a dry run reads too.
  - A command that is not there, fails or does not finish within O(timeout) seconds is reported, not a failure of
    the task.
options:
  timeout:
    description: Seconds each command may take before it is stopped and reported as timed out.
    type: int
    default: 10
attributes:
  check_mode:
    support: full
  diff_mode:
    support: none
  platform:
    platforms: posix
extends_documentation_fragment:
  - ansible.builtin.action_common_attributes
"""

EXAMPLES = r"""
- name: Read the node's Tailscale
  tailscale_read:
  register: read
  no_log: true
"""

RETURN = r"""
results:
  description:
    - Each read's result, by name (C(status), C(prefs), C(serve), C(table), C(forwarding)), with the command's exit
      status and output. C(rc) is 127 for a command that is not there, and null for one that timed out; C(msg) then
      says why.
  type: dict
  returned: always
  sample:
    forwarding: {rc: 0, stdout: "1\n", stderr: "", msg: ""}
    table: {rc: 1, stdout: "", stderr: "Error: No such file or directory", msg: ""}
"""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

from ansible.module_utils.basic import AnsibleModule

# The reads, by the name each result is returned under.
READS = {
    "status": ["tailscale", "status", "--json"],
    "prefs": ["tailscale", "get", "--json"],
    "serve": ["tailscale", "serve", "status", "--json"],
    "table": ["nft", "list", "table", "inet", "tailnet_guard"],
    "forwarding": ["sysctl", "-n", "net.ipv4.ip_forward"],
}


def run_one(argv, timeout):
    """One read's result, in C and UTF-8 so that nothing is printed translated."""
    env = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL, check=False
        )
    except subprocess.TimeoutExpired:
        return {"rc": None, "stdout": "", "stderr": "", "msg": f"{argv[0]} timed out after {timeout} s"}
    except FileNotFoundError as exc:
        return {"rc": 127, "stdout": "", "stderr": "", "msg": str(exc)}
    except OSError as exc:
        return {"rc": None, "stdout": "", "stderr": "", "msg": str(exc)}
    return {"rc": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "msg": ""}


def run_all(timeout):
    """Every read's result by name, all of them at once."""
    with ThreadPoolExecutor(max_workers=len(READS)) as pool:
        futures = {name: pool.submit(run_one, argv, timeout) for name, argv in READS.items()}
        return {name: future.result() for name, future in futures.items()}


def main():
    module = AnsibleModule(argument_spec=dict(timeout=dict(type="int", default=10)), supports_check_mode=True)
    if module.params["timeout"] < 1:
        module.fail_json(msg="timeout must be at least 1.")
    module.exit_json(changed=False, results=run_all(module.params["timeout"]))


if __name__ == "__main__":
    main()
