"""A stateful stand-in for OPNsense 26.7's HTTPS API, for the role tests.

It serves only what roles/opnsense calls, in the shapes OPNsense's source gives them (opnsense/core stable/26.7,
FirmwareController::infoAction), and answers anything else 404, so an unexpected call fails the test. Requests carry
the API key and secret as HTTP basic authentication, as the real API takes them; a wrong pair is answered 401. Every
request is logged, with whether it carried credentials, so a test can prove none reached a server whose certificate
was not the pinned one.

State lives in a JSON file that is re-read for every request, so a test can change it between runs.

    python fake_opnsense.py --port 8443 --cert cert.pem --key key.pem --state state.json --call-log calls.jsonl
"""

import argparse
import base64
import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API_KEY = "fake-opnsense-key-for-tests"
# Stands in for the API secret; the tests check it never reaches Ansible output.
API_SECRET = "FAKE-OPNSENSE-SECRET-MUST-NOT-LEAK"
# The current release when the role was written.
RELEASE = "26.7.8_2"


def initial_state(version=RELEASE, plugins=("os-isc-dhcp",)):
    """A router on `version` with `plugins` installed, and the kernel and base sets of its release."""
    sets = version.split("_", 1)[0]
    return {
        "version": version,
        "series": ".".join(version.split(".")[:2]),
        "packages": {"base": sets, "kernel": sets, "opnsense": version, "php83": "8.3.36", "unbound": "1.24.1"},
        "plugins": {name: "1.0" for name in plugins},
        # A plugin the router offers but has not installed, which the role must not report.
        "available": {"os-tailscale": "1.4", "os-ddclient": "1.29"},
    }


def entry(name, version, installed, repository="OPNsense"):
    """A row of firmware/info's package or plugin list: every value a string, as the PHP builds it."""
    return {
        "name": name,
        "version": version,
        "comment": f"{name} package",
        "flatsize": "1.00 MiB",
        "locked": "N/A",
        "automatic": "N/A",
        "license": "BSD2CLAUSE",
        "repository": repository,
        "origin": f"opnsense/{name}",
        "provided": "1",
        "installed": "1" if installed else "0",
        "path": f"{repository}/opnsense/{name}",
        "configured": "1" if installed else "0",
    }


def firmware_info(state):
    packages = [entry(n, v, True) for n, v in state["packages"].items()]
    packages += [entry(n, v, True) for n, v in state["plugins"].items()]
    packages += [entry(n, v, False) for n, v in state["available"].items()]
    plugins = [entry(n, v, True) | {"tier": "2"} for n, v in state["plugins"].items()]
    plugins += [entry(n, v, False) | {"tier": "3"} for n, v in state["available"].items()]
    return {
        "product_id": "opnsense",
        "product_version": state["version"],
        "package": sorted(packages, key=lambda p: p["name"]),
        "plugin": plugins,
        "changelog": [{"series": state["series"], "version": state["version"], "date": "2026-10-31"}],
        "product": {
            "product_abi": state["series"],
            "product_arch": "amd64",
            "product_name": "OPNsense",
            "product_nickname": "Sensible Shrike",
            "product_series": state["series"],
            "product_version": state["version"],
            "product_latest": state["version"],
            # The real record names its mirror; the role must never return it.
            "product_mirror": "https://pkg.opnsense.org/${ABI}",
            "product_check": None,
        },
    }


ROUTES = {("GET", "/api/core/firmware/info"): firmware_info}


def handler(state_path, call_log):
    expected = "Basic " + base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def answer(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            authorization = self.headers.get("Authorization")
            with Path(call_log).open("a") as log:
                record = {"method": self.command, "path": self.path, "credentials": authorization is not None}
                log.write(json.dumps(record) + "\n")
            if authorization != expected:
                self.answer(401, {"status": 401, "message": "Authentication Failed"})
                return
            route = ROUTES.get((self.command, self.path))
            if route is None:
                self.answer(404, {"errorMessage": "Endpoint not found", "errorTitle": "Error"})
                return
            self.answer(200, route(json.loads(Path(state_path).read_text())))

        do_GET = serve
        do_POST = serve

        def log_message(self, *args):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--call-log", required=True)
    args = parser.parse_args()
    if not Path(args.state).exists():
        Path(args.state).write_text(json.dumps(initial_state(), indent=1))
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(args.state, args.call_log))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
