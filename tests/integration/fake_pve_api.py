"""A fake of Proxmox VE's API as just status reads it, on the host's loopback: GETs answered from fake_pve.py's own
routes and state, so a test seeds the host once for pvesh, qm and the API alike.

Written from what the home host's API answered (Proxmox VE 9.2, 2026-11-18): every read's data is what pvesh get
--output-format json prints for the same path, the guest agent's reads wrap the agent's reply in {"result": ...},
which qm guest cmd prints unwrapped (PVE/API2/Qemu/Agent.pm), a request without a valid token is answered 401, and a
read that fails is an error status whose reason is Proxmox's message. Only GET: anything else is 501. Each request is
logged as a JSON line, its path and whether it carried the token, so a test can tell where the token went.

The token's privileges are what /access/permissions answers, {path: {privilege: 1}}: the runbook's grant, its role's
four privileges on /, unless --privileges gives others ({path: [privilege, ...]}); a path --forbid matches is answered
403, as Proxmox answers a read the token lacks a privilege for. Neither narrows what the other reads return:
/cluster/resources is the whole host's, which is why just status reads the privileges rather than trusting what it
sees.

    python fake_pve_api.py --port 8006 --cert cert.pem --key key.pem --state pve.json --token-id ID --token-secret S
        --log calls.jsonl [--privileges '{"/": ["Sys.Audit"]}'] [--forbid '/agent/']
"""

import argparse
import contextlib
import io
import json
import os
import re
import ssl
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_pve

# One request at a time: each reads the state, may count a read in it, and writes it back whole.
LOCK = threading.Lock()
# The runbook's role, Homeserver (scripts/runbooks/proxmox-token.md): the four privileges just status reads with.
GRANT = ["Datastore.Audit", "Sys.Audit", "VM.Audit", "VM.GuestAgent.Audit"]
AGENT = re.compile(r"/nodes/[^/]+/qemu/([0-9]+)/agent/(get-fsinfo|get-osinfo|network-get-interfaces)")


def answer(path, query):
    """(HTTP status, reason, body) for one GET, from fake_pve's routes and state."""
    agent = AGENT.fullmatch(path)
    out, err = io.StringIO(), io.StringIO()
    with LOCK, fake_pve.locked():
        state = fake_pve.load()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                if agent:
                    fake_pve.qm(state, ["guest", "cmd", agent[1], agent[2]])
                else:
                    options = [part for key, value in query for part in (f"--{key}", value)]
                    fake_pve.pvesh(state, ["get", path, *options, "--output-format", "json"])
        except SystemExit:
            message = (err.getvalue().strip().splitlines() or ["error"])[-1]
            return 500, message, {"data": None}
        fake_pve.save(state)
    data = json.loads(out.getvalue())
    return 200, "OK", {"data": {"result": data} if agent else data}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _log(self, path, token):
        with open(self.server.call_log, "a") as log:
            log.write(json.dumps({"path": path, "token": token}) + "\n")

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(url.path).removeprefix("/api2/json")
        token = self.headers.get("Authorization") == f"PVEAPIToken={self.server.token}"
        self._log(path, token)
        if not token:
            status, reason, body = 401, "permission denied - invalid PVE ticket", {"data": None}
        elif path == "/access/permissions":
            privileges = {where: dict.fromkeys(names, 1) for where, names in self.server.privileges.items()}
            status, reason, body = 200, "OK", {"data": privileges}
        elif self.server.forbid and re.search(self.server.forbid, path):
            status, reason, body = 403, f"Permission check failed ({path})", {"data": None}
        else:
            status, reason, body = answer(path, urllib.parse.parse_qsl(url.query))
        payload = json.dumps(body).encode()
        self.send_response(status, reason)
        self.send_header("Content-Type", "application/json;charset=UTF-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _refuse(self):
        self._log(urllib.parse.urlsplit(self.path).path, False)
        self.send_response(501, "just status only reads")
        self.end_headers()

    do_POST = do_PUT = do_DELETE = _refuse


def main():
    parser = argparse.ArgumentParser()
    for option in ("--port", "--cert", "--key", "--state", "--token-id", "--token-secret", "--log"):
        parser.add_argument(option, required=True)
    parser.add_argument("--privileges", default=json.dumps({"/": GRANT}))
    parser.add_argument("--forbid", default="")
    args = parser.parse_args()
    os.environ["FAKE_PVE_STATE"] = args.state
    server = ThreadingHTTPServer(("127.0.0.1", int(args.port)), Handler)
    server.token = f"{args.token_id}={args.token_secret}"
    server.call_log = args.log
    server.privileges = json.loads(args.privileges)
    server.forbid = args.forbid
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
