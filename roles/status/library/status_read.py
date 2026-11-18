#!/usr/bin/python

DOCUMENTATION = r"""
---
module: status_read
short_description: Run read-only commands at once and return each one's output
description:
  - Runs every command in O(commands) at the same time, at most O(parallel) of them at once, each with
    O(timeout) seconds to finish, and returns each one's exit status, output and duration. On a Proxmox host
    each C(pvesh) or C(qm) call starts Proxmox's API afresh and takes about a second of CPU, so twenty of them
    one after another, as a loop of tasks would run them, take twenty seconds.
  - Runs a command only if it has one of the exact forms in the module's own read list, and otherwise refuses
    the whole task before running anything. A tool's name alone is never enough, since most tools also write or
    return secrets, such as C(kubectl get secrets) or C(talosctl get machineconfig).
  - Never changes anything, so a dry run runs the commands too.
  - A command that fails or times out is reported, not a failure of the task; the caller decides what it means.
options:
  commands:
    description:
      - The commands to run, by a name of the caller's choosing, each as its argument list.
    type: dict
    required: true
  parallel:
    description:
      - How many commands run at once. Unset, as many as the machine the module runs on has CPUs, 8 at most, since
        each C(pvesh) call is a second of CPU and about 180 MB of memory.
    type: int
  timeout:
    description: Seconds each command may take before it is stopped and reported as timed out.
    type: int
    default: 30
  timeouts:
    description:
      - Seconds for a command of its own, by its name in O(commands), in place of O(timeout), so one that should
        give up sooner can run in the same pass as slower ones.
    type: dict
    default: {}
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
      running: [ls, /run/qemu-server]
      resources: [pvesh, get, /cluster/resources, --output-format, json]
      fsinfo-310: [qm, guest, cmd, "310", get-fsinfo]
  register: found

- name: Read the cluster's nodes and the commit its Flux source points at
  status_read:
    commands:
      nodes: [kubectl, --kubeconfig, /home/me/.talos/homeserver/kubeconfig, --request-timeout=10s, get, nodes, -A,
              -o, json]
      remote: [git, ls-remote, "https://github.com/example/homeserver.git", refs/heads/main]
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
# A vzdump task's id, as Proxmox writes it (UPID:<node>:<pid>:<pstart>:<start time>:vzdump:<vmid or none>:<user>:),
# a count, and a calendar event as Proxmox's schedules are written (02:00, mon..fri 02:00, */30), never an option.
UPID = rf"UPID:{NODE}:[0-9A-F]{{8,}}:[0-9A-F]{{8,}}:[0-9A-F]{{8,}}:vzdump:[0-9]*:[A-Za-z0-9@._!-]+:"
COUNT = r"[1-9][0-9]{0,4}"
SCHEDULE = r"[A-Za-z0-9*][A-Za-z0-9 :.,*/~-]{0,99}"
# The directory where qemu-server keeps a <vmid>.pid for each VM running on the node (/run/qemu-server), wherever
# a test keeps its own: only a directory of that name, through no . or .. part, whose listing is file names alone.
QEMU_RUN_DIR = r"(/(?!\.\.?(/|$))[A-Za-z0-9_.-]+)*/qemu-server"
# A credentials file, wherever its owner keeps it: no shell ever reads an argument, so any absolute path is one word.
CONFIG = r"/[^\x00\n]+"
JSON = ["--output-format", "json"]
DAY = ["--timeframe", "day", "--cf", "AVERAGE"]
# A Kubernetes object's name, and the kinds kubectl may list: none holds a secret, and Secrets are not among them.
K8S_NAME = r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?"
KINDS = "nodes|pods|services|ingresses|persistentvolumes|kustomizations|helmreleases|fluxinstances|certificates"
KUBECTL = ["kubectl", "--kubeconfig", CONFIG, r"--request-timeout=[1-9][0-9]?s"]
# A repository git can list over HTTPS or SSH, never through a transport that runs a command (ext::) or a local path.
GIT_URL = r"(https://|ssh://)[A-Za-z0-9._~:/@%+=-]+|git@[A-Za-z0-9.-]+:[A-Za-z0-9._~/-]+"
GIT_REF = r"refs/(heads|tags)/[A-Za-z0-9._/-]+"

# Every command this module runs, as one regular expression per argument. Nothing here writes, and nothing returns
# a secret: ls only of the VMs' run directory, no other pvesh path (the backup jobs, the backup tasks and their logs,
# and when a schedule next fires among them), no qm guest command beyond these three reads, apt-get only simulating,
# kubectl only listing kinds that hold no secret (the Deployments with the times each field was set), one ConfigMap,
# a node's kubelet statistics and Alertmanager's alerts, talosctl only the etcd service's state (talosctl get could
# read the machine configuration, every key of the cluster), and git only the commit a branch points at.
READS = [
    ["hostname", "-s"],
    ["test", "-e", PATH],
    ["ls", QEMU_RUN_DIR],
    ["pvesh", "get", "/cluster/resources", *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/status", *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/rrddata", *DAY, *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/qemu/{VMID}/rrddata", *DAY, *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/qemu/{VMID}/pending", *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/storage/{STORAGE}/content", *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/storage/{STORAGE}/content", "--content", "backup", *JSON],
    ["pvesh", "get", "/cluster/backup", *JSON],
    ["pvesh", "get", "/cluster/jobs/schedule-analyze", "--schedule", SCHEDULE, "--iterations", "2", *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/tasks", "--typefilter", "vzdump", "--source", "all", "--limit", COUNT, *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/tasks/{UPID}/log", "--limit", COUNT, *JSON],
    ["qm", "guest", "cmd", VMID, "get-fsinfo|get-osinfo|network-get-interfaces"],
    ["apt-get", "-q", "-s", "dist-upgrade"],
    [*KUBECTL, "get", KINDS, "-A", "-o", "json"],
    [*KUBECTL, "get", "deployments", "-A", "-o", "json", "--show-managed-fields"],
    [*KUBECTL, "get", "configmap", K8S_NAME, "-n", K8S_NAME, "-o", "json"],
    [*KUBECTL, "get", "--raw", f"/api/v1/nodes/{K8S_NAME}/proxy/stats/summary"],
    [
        *KUBECTL,
        "exec",
        "-n",
        K8S_NAME,
        K8S_NAME,
        "-c",
        "alertmanager",
        "--",
        "amtool",
        "alert",
        "query",
        "-o",
        "json",
        "--alertmanager.url=http://127.0.0.1:9093",
    ],
    ["talosctl", "--talosconfig", CONFIG, "service", "etcd"],
    ["git", "ls-remote", GIT_URL, GIT_REF],
    # This machine's own Tailscale: where the tailnet's split DNS sends the apps' domain.
    ["tailscale", "dns", "status", "--json"],
]
_READS = [[re.compile(part) for part in form] for form in READS]


def allowed(argv):
    """Whether an argument list has one of the read list's forms."""
    return any(
        len(argv) == len(form) and all(part.fullmatch(arg) for part, arg in zip(form, argv, strict=True))
        for form in _READS
    )


def run_one(argv, timeout):
    """One command's result. C and UTF-8, so that tools whose output is parsed never print it translated, and git
    never asks for a password, which would wait on a terminal that is not there."""
    env = {**os.environ, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0"}
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


def run_all(commands, parallel, timeout, timeouts=None):
    """Every command's result by name, the commands run at most `parallel` at a time, each within its own of
    `timeouts` or else `timeout`."""
    timeouts = timeouts or {}
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = {name: pool.submit(run_one, argv, timeouts.get(name, timeout)) for name, argv in commands.items()}
        return {name: future.result() for name, future in futures.items()}


def main():
    module = AnsibleModule(
        argument_spec=dict(
            commands=dict(type="dict", required=True),
            parallel=dict(type="int"),
            timeout=dict(type="int", default=30),
            timeouts=dict(type="dict", default={}),
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
    # Unset, one per CPU, but never more than main's 8: each pvesh call holds about 180 MB while it runs.
    parallel = module.params["parallel"] if module.params["parallel"] is not None else min(os.cpu_count() or 1, 8)
    timeouts = module.params["timeouts"]
    if not all(isinstance(s, int) and not isinstance(s, bool) for s in timeouts.values()):
        module.fail_json(msg="timeouts must give each command a whole number of seconds.")
    if parallel < 1 or module.params["timeout"] < 1 or any(s < 1 for s in timeouts.values()):
        module.fail_json(msg="parallel, timeout and timeouts must be at least 1.")
    module.exit_json(changed=False, results=run_all(commands, parallel, module.params["timeout"], timeouts))


if __name__ == "__main__":
    main()
