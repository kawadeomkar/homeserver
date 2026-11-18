#!/usr/bin/python

DOCUMENTATION = r"""
---
module: status_read
short_description: Run read-only commands and Proxmox API reads at once and return each one's output
description:
  - Runs every command in O(commands) at the same time, at most O(parallel) of them at once, each with
    O(timeout) seconds to finish, and returns each one's exit status, output and duration. Each C(pvesh) call
    starts Proxmox's Perl API afresh, about a second of CPU, so twenty of them one after another, as a loop of
    tasks would run them, take twenty seconds; this module's Proxmox reads go to the API (O(api)) instead, but
    for the two Proxmox lists only to a token that may change what they list.
  - Runs a command only if it has one of the exact forms in the module's own read list, and otherwise refuses
    the whole task before running anything. A tool's name alone is never enough, since most tools also write or
    return secrets, such as C(kubectl get secrets) or C(talosctl get machineconfig).
  - On a Proxmox host it also makes O(api) reads, GETs of Proxmox's API with the token O(api_token_id) and
    O(api_token_secret), in the same pass as the commands. Each read has one of the exact path forms on the module's
    API read list, or the whole task is refused. A GET costs its handler, milliseconds where C(pvesh) starts
    Proxmox's Perl API afresh for each read.
  - An API read never raises and never quotes the token. A failure that is the token's carries C(token), one of
    missing (none, or not a token's shape, so nothing is sent), refused (HTTP 401) or forbidden (HTTP 403).
  - Before it sends the token on a connection, it compares the certificate the API serves with the first one in the
    first of O(api_cert_files) that exists, the node's own, and sends nothing to any other. It checks no chain of
    trust, so neither Proxmox's CA nor a pin that goes stale when Proxmox issues the certificate again is needed.
  - Given O(upgrades_state_paths), it answers C(apt-get -q -s dist-upgrade) from O(upgrades_kept) when nothing
    the simulation's answer depends on has changed since, by a key of the host, the command and the files under
    those paths (dpkg's status and journal, the package lists, apt's configuration) by name, size, times and inode,
    a few milliseconds where the simulation takes one to two seconds. The key goes back with a fresh answer, for the
    caller to keep.
  - Never changes anything, so a dry run runs the commands too.
  - A command or read that fails or times out is reported, not a failure of the task; the caller decides what it
    means.
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
    description: Seconds each command or API read may take before it is stopped and reported as timed out.
    type: int
    default: 30
  timeouts:
    description:
      - Seconds for a command of its own, by its name in O(commands), in place of O(timeout), so one that should
        give up sooner can run in the same pass as slower ones.
    type: dict
    default: {}
  api:
    description:
      - The API reads to make, by a name of the caller's choosing, each as its path under O(api_url) followed by
        its query's C(key=value) pairs, in the order the read list gives them.
      - Each result's stdout is the API's C(data) member as JSON, as C(pvesh get --output-format json) prints it; a
        guest agent command's is the agent's own reply, as C(qm guest cmd) prints it, not the C(result) the API
        wraps it in.
    type: dict
    default: {}
  api_url:
    description:
      - Where Proxmox's API answers, its C(api2/json) root; on the host itself, its loopback address, with Proxmox's
        port 8006. An address without a port means https's own, 443.
    type: str
    default: ""
  api_token_id:
    description: The API token's id, C(<user>@<realm>!<name>).
    type: str
    default: ""
  api_token_secret:
    description: The API token's secret. Empty, no API read is made, and each says so.
    type: str
    default: ""
  upgrades_state_paths:
    description:
      - The files and directories whose names, sizes, times and inodes C(apt-get -q -s dist-upgrade)'s answer is
        keyed by, a directory walked through its symbolic links. Empty, the simulation always runs and no key is
        returned.
    type: list
    elements: str
    default: []
  upgrades_kept:
    description:
      - The simulation's answer as last kept, C({key, stdout}), which answers it while the key is the same.
    type: dict
    default: {}
  upgrades_host:
    description:
      - The host the kept answer belongs to, part of its key; empty takes this machine's hostname.
    type: str
    default: ""
  api_cert_files:
    description:
      - Files that may hold the certificate the API serves, in the order to try them; the first that exists is
        taken, and its first certificate compared, as pveproxy serves C(pveproxy-ssl.pem) where one is installed
        and C(pve-ssl.pem) otherwise.
    type: list
    elements: str
    default: []
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
- name: Read the node's name and which VMs run on it
  status_read:
    commands:
      hostname: [hostname, -s]
      running: [ls, /run/qemu-server]
  register: found

- name: Read the host's API and simulate its upgrades, at once
  status_read:
    commands:
      updates: [apt-get, -q, -s, dist-upgrade]
      content-local-lvm: [pvesh, get, /nodes/pve/storage/local-lvm/content, --output-format, json]
    api:
      permissions: [/access/permissions]
      resources: [/cluster/resources]
      rrd-300: [/nodes/pve/qemu/300/rrddata, timeframe=day, cf=AVERAGE]
      fsinfo-310: [/nodes/pve/qemu/310/agent/get-fsinfo]
    api_url: https://127.0.0.1:8006/api2/json
    api_token_id: status@pve!status
    api_token_secret: "{{ proxmox_token_secret }}"
    api_cert_files: [/etc/pve/local/pveproxy-ssl.pem, /etc/pve/local/pve-ssl.pem]
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
    - Each command's and API read's result, by its name. C(rc) is null for a command that could not start or timed
      out, or an API read that was not made or not answered with its data, and C(error) then says why.
    - A failed API read that is the token's also carries C(token), one of missing, refused or forbidden, and one
      whose API did not answer at all (nothing listening) carries C(unanswered).
    - The simulation's also carries C(key), with O(upgrades_state_paths), and C(kept) when its answer came from
      O(upgrades_kept).
  type: dict
  returned: always
  sample:
    resources: {rc: 0, stdout: "[...]", stderr: "", seconds: 1.21, error: ""}
"""

import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
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
# A Kubernetes object's name, and the kinds kubectl may list: none holds a secret, and Secrets are not among them.
K8S_NAME = r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?"
KINDS = "nodes|pods|services|ingresses|persistentvolumes|kustomizations|helmreleases|fluxinstances|certificates"
KUBECTL = ["kubectl", "--kubeconfig", CONFIG, r"--request-timeout=[1-9][0-9]?s"]
# A repository git can list over HTTPS or SSH, never through a transport that runs a command (ext::) or a local path.
GIT_URL = r"(https://|ssh://)[A-Za-z0-9._~:/@%+=-]+|git@[A-Za-z0-9.-]+:[A-Za-z0-9._~/-]+"
GIT_REF = r"refs/(heads|tags)/[A-Za-z0-9._/-]+"

# Every command this module runs, as one regular expression per argument. Nothing here writes, and nothing returns
# a secret: ls only of the VMs' run directory; pvesh only for the two listings Proxmox gives no read-only token, a
# storage's VM disks and its backups (every other Proxmox read is the API's, below, with the token, so root's pvesh
# and qm guest reads are refused here: a later edit cannot slip back to them); apt-get only simulating; kubectl only
# listing kinds that hold no secret (the Deployments with the times each field was set), one ConfigMap, a node's
# kubelet statistics and Alertmanager's alerts; talosctl only the etcd service's state (talosctl get could read the
# machine configuration, every key of the cluster); and git only the commit a branch points at.
READS = [
    ["hostname", "-s"],
    ["test", "-e", PATH],
    ["ls", QEMU_RUN_DIR],
    ["pvesh", "get", f"/nodes/{NODE}/storage/{STORAGE}/content", *JSON],
    ["pvesh", "get", f"/nodes/{NODE}/storage/{STORAGE}/content", "--content", "backup", *JSON],
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

# Every read of Proxmox's API this module makes, as one regular expression per part: the path, then its query's
# key=value pairs. GETs alone, and nothing that returns a secret: the node and its VMs, their history and pending
# changes, the guest agent's three reads, the backup jobs, their runs and a run's log, when a schedule next fires, and
# the token's own privileges, which say whether it may read all of that.
API_READS = [
    ["/access/permissions"],
    ["/cluster/resources"],
    [f"/nodes/{NODE}/status"],
    [f"/nodes/{NODE}/rrddata", "timeframe=day", "cf=AVERAGE"],
    [f"/nodes/{NODE}/qemu/{VMID}/rrddata", "timeframe=day", "cf=AVERAGE"],
    [f"/nodes/{NODE}/qemu/{VMID}/pending"],
    [f"/nodes/{NODE}/qemu/{VMID}/agent/(get-fsinfo|get-osinfo|network-get-interfaces)"],
    ["/cluster/backup"],
    ["/cluster/jobs/schedule-analyze", f"schedule={SCHEDULE}", "iterations=2"],
    [f"/nodes/{NODE}/tasks", "typefilter=vzdump", "source=all", f"limit={COUNT}"],
    [f"/nodes/{NODE}/tasks/{UPID}/log", f"limit={COUNT}"],
]
_API_READS = [[re.compile(part) for part in form] for form in API_READS]
# A guest agent command: the API wraps the agent's reply in {"result": ...}, which qm guest cmd prints unwrapped.
AGENT = re.compile(r"/nodes/[^/]+/qemu/[0-9]+/agent/[a-z-]+")


def allowed(argv, forms=_READS):
    """Whether an argument list has one of the read list's forms (or the API read list's)."""
    return any(
        len(argv) == len(form) and all(part.fullmatch(arg) for part, arg in zip(form, argv, strict=True))
        for form in forms
    )


def served_certificate(paths):
    """(SHA-256 of the first certificate in the first of `paths` that exists, the file), or (None, why)."""
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="ascii") as handle:
                pem = handle.read()
            end = "-----END CERTIFICATE-----"
            first = pem[pem.index("-----BEGIN CERTIFICATE-----") : pem.index(end) + len(end)] + "\n"
            return hashlib.sha256(ssl.PEM_cert_to_DER_cert(first)).hexdigest(), path
        except (OSError, ValueError) as error:
            return None, f"{path} holds no certificate: {error}"
    return None, f"none of {', '.join(paths) or 'no file'} exists"


def api_read(parts, api, timeout):
    """One API read's result, as run_one gives a command's: rc 0 and the data as JSON, or rc null and why, never an
    exception. The token is sent only on a connection whose certificate is the node's own, and no message ever quotes
    it or anything that could hold it: an error the module did not foresee is named by its type alone. A failure that
    is the token's carries `token`: missing (none, or not a token's shape), refused (HTTP 401) or forbidden (HTTP 403,
    a privilege it lacks), which the caller reads rather than the words; one whose API did not answer at all carries
    `unanswered`. Once Proxmox refuses the token, the reads after it fail the same way without asking: Proxmox holds
    each refusal 3 s, which every read would otherwise wait out in turn."""
    start = time.monotonic()

    def failed(why, token=None, **more):
        result = {"rc": None, "stdout": "", "stderr": "", "seconds": round(time.monotonic() - start, 2), "error": why}
        return result | ({"token": token} if token else {}) | more

    if api.get("problem"):
        return failed(api["problem"], api.get("token"))
    if api["refused"].is_set():
        return failed(f"Proxmox refused the API token {api['token_id']} (HTTP 401)", "refused")
    where = f"{api['host']}:{api['port']}"
    # A task's id holds : @ and !, which a path may carry as they are (RFC 3986's pchar).
    path = api["path"] + urllib.parse.quote(parts[0], safe="/:@!")
    query = urllib.parse.urlencode([tuple(part.split("=", 1)) for part in parts[1:]], quote_via=urllib.parse.quote)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    connection = None
    try:
        connection = http.client.HTTPSConnection(api["host"], api["port"], timeout=timeout, context=context)
        connection.connect()
        served = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
        if served != api["certificate"]:
            return failed(f"the API at {where} serves another certificate than {api['cert_file']}'s: no token sent")
        connection.request(
            "GET",
            path + (f"?{query}" if query else ""),
            headers={"Authorization": f"PVEAPIToken={api['token_id']}={api['token_secret']}"},
        )
        response = connection.getresponse()
        body = response.read()
    except ConnectionRefusedError:
        # Nothing listens: pveproxy, which answers the API and the web UI alike, is down, or listens elsewhere.
        return failed(f"pveproxy does not answer at {where} (connection refused)", unanswered=True)
    except (OSError, http.client.HTTPException) as error:
        # Neither quotes a header: a reset connection, a TLS failure, a malformed answer.
        return failed(f"the API at {where}: {str(error) or type(error).__name__}")
    except Exception as error:  # noqa: BLE001 - anything else may quote what it was given, the header among it
        return failed(f"the API at {where}: the read failed ({type(error).__name__})")
    finally:
        if connection is not None:
            connection.close()
    if response.status == 401:
        api["refused"].set()
        return failed(f"Proxmox refused the API token {api['token_id']} (HTTP 401)", "refused")
    if response.status == 403:
        return failed(f"the API token may not read this (HTTP 403: {response.reason})", "forbidden")
    if response.status != 200:
        return failed(f"HTTP {response.status}: {response.reason}")
    try:
        data = json.loads(body)["data"]
    except (ValueError, KeyError, TypeError):
        data = None
    if data is None:
        return failed("the API answered no data")
    if AGENT.fullmatch(parts[0]) and isinstance(data, dict) and "result" in data:
        data = data["result"]
    return {
        "rc": 0,
        "stdout": json.dumps(data),
        "stderr": "",
        "seconds": round(time.monotonic() - start, 2),
        "error": "",
    }


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


# The simulation whose answer can be kept: what apt would upgrade, from the lists as last refreshed.
SIMULATION = ["apt-get", "-q", "-s", "dist-upgrade"]
# Raised whenever what the key covers changes, so an answer kept under the old key never stands for the new one.
KEY_FORMAT = 2


def upgrades_key(paths, host, argv):
    """What the simulation's answer depends on, as a digest: the key's format, the host and the command, then each file
    under `paths`, a directory walked through its symbolic links (each directory once), by its name, size,
    modification and change times and inode, the file a link points at taken for the link. Metadata alone, under a
    millisecond for dpkg's status and journal, the lists and /etc/apt on a Proxmox host, which the simulation itself
    leaves as they are; the change time and inode catch a file rewritten with its old modification time, as a restore
    or `cp -p` leaves it. Names are hashed as bytes, so a file whose name is not UTF-8 is keyed like any other."""
    digest = hashlib.sha256(f"{KEY_FORMAT}\n{host}\n{' '.join(argv)}\n".encode())

    def add(path):
        try:
            info = os.stat(path)
        except OSError:
            try:
                info = os.lstat(path)
            except OSError:
                digest.update(os.fsencode(path) + b" -\n")
                return
        line = f" {info.st_size} {info.st_mtime_ns} {info.st_ctime_ns} {info.st_ino}\n"
        digest.update(os.fsencode(path) + line.encode())

    for top in paths:
        if not os.path.isdir(top):
            add(top)
            continue
        seen = set()
        for folder, names, files in os.walk(top, followlinks=True):
            here = os.stat(folder)
            if (here.st_dev, here.st_ino) in seen:
                names[:] = []
                continue
            seen.add((here.st_dev, here.st_ino))
            names.sort()
            for name in sorted(files):
                add(os.path.join(folder, name))
    return digest.hexdigest()


def simulate(argv, timeout, state_paths, kept, host):
    """The simulation's result, its kept answer while nothing it depends on has changed, with the key to keep it by.
    A key that cannot be made (a file that vanishes mid-walk, say) only means the simulation runs, unkept."""
    if not state_paths:
        return run_one(argv, timeout)
    try:
        key = upgrades_key(state_paths, host or socket.gethostname(), argv)
    except Exception:  # noqa: BLE001 - the kept answer is only ever a shortcut
        return run_one(argv, timeout)
    if isinstance(kept, dict) and kept.get("key") == key and isinstance(kept.get("stdout"), str):
        return {"rc": 0, "stdout": kept["stdout"], "stderr": "", "seconds": 0.0, "error": "", "kept": True, "key": key}
    return run_one(argv, timeout) | {"key": key}


def run_all(commands, parallel, timeout, timeouts=None, reads=None, api=None, state_paths=(), kept=None, host=""):
    """Every command's and API read's result by name, at most `parallel` at a time, each command within its own of
    `timeouts` or else `timeout`; the simulation answered from `kept` while its key holds."""
    timeouts = timeouts or {}
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        futures = {
            name: pool.submit(simulate, argv, timeouts.get(name, timeout), state_paths, kept, host)
            if argv == SIMULATION
            else pool.submit(run_one, argv, timeouts.get(name, timeout))
            for name, argv in commands.items()
        }
        futures |= {name: pool.submit(api_read, parts, api, timeout) for name, parts in (reads or {}).items()}
        return {name: future.result() for name, future in futures.items()}


# A token's id as Proxmox makes it (<user>@<realm>!<name>, its name two characters at least) and its secret, a UUID. A
# value of any other shape is refused before anything is sent, and never quoted: a line break in either would make the
# HTTP library's own error quote the whole Authorization header.
TOKEN_ID = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9._-]+![A-Za-z][A-Za-z0-9._-]+")
TOKEN_SECRET = re.compile(r"[0-9A-Fa-f-]{16,64}")


def api_settings(params):
    """What every API read needs, worked out once: where, the token, the node's own certificate, or the problem that
    leaves every read unmade (with `token` when it is the token's)."""
    # Set once Proxmox refuses the token, so the reads after it do not each wait out its 3 s.
    api = {"token_id": params["api_token_id"], "token_secret": params["api_token_secret"], "refused": threading.Event()}
    try:
        url = urllib.parse.urlsplit(params["api_url"])
        host, port = url.hostname, url.port
    except ValueError:
        url, host, port = None, None, None
    if url is None or url.scheme != "https" or not host or port == 0:
        return api | {"problem": "no https API address with a host, and a port from 1 to 65535 if it names one"}
    # Without a port, https's own, as any https address means; Proxmox's 8006 is the caller's to name.
    api |= {"host": host, "port": port or 443, "path": url.path.rstrip("/")}
    # The words name no option and no variable: the caller says where its token comes from.
    if not params["api_token_id"] or not params["api_token_secret"]:
        return api | {"problem": "no API token", "token": "missing"}
    if not TOKEN_ID.fullmatch(params["api_token_id"]):
        return api | {"problem": "the API token's id is not <user>@<realm>!<name>", "token": "missing"}
    if not TOKEN_SECRET.fullmatch(params["api_token_secret"]):
        why = "the API token's secret is not a token's: it holds a character Proxmox's never do, a line break perhaps"
        return api | {"problem": why, "token": "missing"}
    certificate, where = served_certificate(params["api_cert_files"])
    if certificate is None:
        return api | {"problem": f"the node's certificate: {where}"}
    return api | {"certificate": certificate, "cert_file": where}


def main():
    module = AnsibleModule(
        argument_spec=dict(
            commands=dict(type="dict", required=True),
            parallel=dict(type="int"),
            timeout=dict(type="int", default=30),
            timeouts=dict(type="dict", default={}),
            api=dict(type="dict", default={}),
            api_url=dict(type="str", default=""),
            api_token_id=dict(type="str", default=""),
            api_token_secret=dict(type="str", default="", no_log=True),
            api_cert_files=dict(type="list", elements="str", default=[]),
            upgrades_state_paths=dict(type="list", elements="str", default=[]),
            upgrades_kept=dict(type="dict", default={}),
            upgrades_host=dict(type="str", default=""),
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
    reads = {}
    for name, parts in module.params["api"].items():
        if not isinstance(parts, list) or not all(isinstance(a, str | int) for a in parts):
            problems.append(f"{name}: not a list of a path and its query")
            continue
        parts = [str(a) for a in parts]
        if not allowed(parts, _API_READS):
            problems.append(f"{name}: GET {' '.join(parts)}")
            continue
        reads[name] = parts
    if problems:
        module.fail_json(msg="Not on status_read's read list, so nothing was run: " + "; ".join(problems))
    # Unset, one per CPU, but never more than main's 8: each pvesh call holds about 180 MB while it runs.
    parallel = module.params["parallel"] if module.params["parallel"] is not None else min(os.cpu_count() or 1, 8)
    timeouts = module.params["timeouts"]
    if not all(isinstance(s, int) and not isinstance(s, bool) for s in timeouts.values()):
        module.fail_json(msg="timeouts must give each command a whole number of seconds.")
    if parallel < 1 or module.params["timeout"] < 1 or any(s < 1 for s in timeouts.values()):
        module.fail_json(msg="parallel, timeout and timeouts must be at least 1.")
    api = api_settings(module.params) if reads else None
    results = run_all(
        commands,
        parallel,
        module.params["timeout"],
        timeouts,
        reads,
        api,
        module.params["upgrades_state_paths"],
        module.params["upgrades_kept"],
        module.params["upgrades_host"],
    )
    module.exit_json(changed=False, results=results)


if __name__ == "__main__":
    main()
