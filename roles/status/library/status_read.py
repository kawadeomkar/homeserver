#!/usr/bin/python

DOCUMENTATION = r"""
---
module: status_read
short_description: Run read-only commands at once and return each one's output
description:
  - Runs every command in O(commands) at the same time, at most O(parallel) of them at once, each with
    O(timeout) seconds to finish, and returns each one's exit status, output and duration. On a Proxmox host
    each C(pvesh) or C(qm) call starts Proxmox's API afresh and takes about a second, so twenty of them one
    after another, as a loop of tasks would run them, take twenty seconds.
  - Runs a command only if it has one of the exact forms in the module's own read list, and otherwise refuses
    the whole task before running anything. A tool's name alone is never enough, since most tools also write.
  - Never changes anything, so a dry run runs the commands too.
  - A command that fails or times out is reported, not a failure of the task; the caller decides what it means.
options:
  commands:
    description:
      - The commands to run, by a name of the caller's choosing, each as its argument list.
    type: dict
    required: true
  parallel:
    description: How many commands run at once.
    type: int
    default: 8
  timeout:
    description: Seconds each command may take before it is stopped and reported as timed out.
    type: int
    default: 30
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
- name: Read the host and its guests
  status_read:
    commands:
      resources: [pvesh, get, /cluster/resources, --output-format, json]
      fsinfo-310: [qm, guest, cmd, "310", get-fsinfo]
  register: found
"""

RETURN = r"""
results:
  description:
    - Each command's result, by its name. C(rc) is null for a command that could not start or timed out, and
      C(error) then says why.
  type: dict
  returned: always
  sample:
    resources: {rc: 0, stdout: "[...]", stderr: "", seconds: 1.21, error: ""}
"""

import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from ansible.module_utils.basic import AnsibleModule

# The parts of an argument list, each matched whole against one argument.
NODE = r"[A-Za-z0-9][A-Za-z0-9.-]*"
VMID = r"[1-9][0-9]{2,8}"
STORAGE = r"[A-Za-z][A-Za-z0-9_.-]*"
PATH = r"/[A-Za-z0-9_./-]+"
JSON = ["--output-format", "json"]
DAY = ["--timeframe", "day", "--cf", "AVERAGE"]

# Every command this module runs, as one regular expression per argument. Nothing here writes, and nothing returns
# a secret: no other pvesh path, no qm guest command beyond these three reads, and apt-get only simulating.
READS = [
    ["hostname", "-s"],
    ["test", "-e", PATH],
    ["pvesh", "get", "/cluster/resources", *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/status", *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/rrddata", *DAY, *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/qemu/{VMID}/rrddata", *DAY, *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/qemu/{VMID}/pending", *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/storage/{STORAGE}/content", *JSON],
    ["qm", "guest", "cmd", VMID, "get-fsinfo|get-osinfo|network-get-interfaces"],
    ["apt-get", "-q", "-s", "dist-upgrade"],
]
_READS = [[re.compile(part) for part in form] for form in READS]


def allowed(argv):
    """Whether an argument list has one of the read list's forms."""
    return any(
        len(argv) == len(form) and all(part.fullmatch(arg) for part, arg in zip(form, argv, strict=True))
        for form in _READS
    )


def run_one(argv, timeout):
    """One command's result. C and UTF-8, so that tools whose output is parsed never print it translated."""
    env = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"}
    start = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL, check=False
        )
    except subprocess.TimeoutExpired:
        seconds = round(time.monotonic() - start, 2)
        return {"rc": None, "stdout": "", "stderr": "", "seconds": seconds, "error": f"timed out after {timeout} s"}
    except OSError as exc:
        return {"rc": None, "stdout": "", "stderr": "", "seconds": 0.0, "error": str(exc)}
    return {
        "rc": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "seconds": round(time.monotonic() - start, 2),
        "error": "",
    }


def run_all(commands, parallel, timeout):
    """Every command's result by name, the commands run at most `parallel` at a time."""
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = {name: pool.submit(run_one, argv, timeout) for name, argv in commands.items()}
        return {name: future.result() for name, future in futures.items()}


def main():
    module = AnsibleModule(
        argument_spec=dict(
            commands=dict(type="dict", required=True),
            parallel=dict(type="int", default=8),
            timeout=dict(type="int", default=30),
        ),
        supports_check_mode=True,
    )
    commands, problems = {}, []
    for name, argv in module.params["commands"].items():
        if not isinstance(argv, list) or not all(isinstance(a, str | int) for a in argv):
            problems.append(f"{name}: not a list of arguments")
            continue
        argv = [str(a) for a in argv]
        if not allowed(argv):
            problems.append(f"{name}: {' '.join(argv)}")
            continue
        commands[name] = argv
    if problems:
        module.fail_json(msg="Not on status_read's read list, so nothing was run: " + "; ".join(problems))
    if module.params["parallel"] < 1 or module.params["timeout"] < 1:
        module.fail_json(msg="parallel and timeout must be at least 1.")
    module.exit_json(changed=False, results=run_all(commands, module.params["parallel"], module.params["timeout"]))


if __name__ == "__main__":
    main()
