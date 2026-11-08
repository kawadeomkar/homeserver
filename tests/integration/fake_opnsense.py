"""A stateful stand-in for OPNsense 26.7's HTTPS API and the one web UI page roles/opnsense uses, for the role tests.

It serves what roles/opnsense calls, in the shapes OPNsense's source gives them (opnsense/core stable/26.7:
FirmwareController::infoAction; BackupController's backups and download; www/diag_backup.php's restore, behind the
login form of www/authgui.inc and the CSRF check of www/csrf.inc), and answers anything else 404, so an unexpected call
fails the test. The API takes the key pair as HTTP basic authentication, and a wrong one is answered 401; the web UI
takes root's password through the login form, and refuses any POST without the session's CSRF token, 403. An uploaded
configuration is decrypted with the openssl command, as the router decrypts it, so the role's encryption is proven
against openssl itself. Every request is logged with whether it carried credentials, so a test can prove none reached
a server whose certificate was not the pinned one.

Two ports stand for the router's two addresses: --port, its own, where the router as it is known answers with the
pinned certificate, and --bootstrap-port, where a fresh install answers with a certificate of its own. While the
state says the router is fresh, its own address drops every connection; once a restore has rebooted it, the
bootstrap address does, and the own address drops connections for `reboot_seconds` first.

State lives in a JSON file that is re-read for every request, so a test can change it between runs.

    python fake_opnsense.py --port 8443 --cert cert.pem --key key.pem --state state.json --call-log calls.jsonl \\
        [--bootstrap-port 8444 --bootstrap-cert fresh.pem --bootstrap-key fresh.key]
"""

import argparse
import base64
import email.parser
import email.policy
import html
import json
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

API_KEY = "fake-opnsense-key-for-tests"
# Stand in for the API secret, root's web UI password and a secret the configuration holds; the tests check none of
# them reaches Ansible output or a backup file in the clear.
API_SECRET = "FAKE-OPNSENSE-SECRET-MUST-NOT-LEAK"
ROOT_PASSWORD = "FAKE-ROOT-PASSWORD-MUST-NOT-LEAK"
CONFIG_SECRET = "FAKE-CONFIG-SECRET-MUST-NOT-LEAK"
# The current release when the role was written.
RELEASE = "26.7.8_2"
LOCK = threading.Lock()


def config(hostname, revision_time, description, marker=""):
    """A configuration file as the router keeps it, small, its revision stamped as Config::save() stamps it."""
    return (
        '<?xml version="1.0"?>\n<opnsense>\n  <revision>\n'
        f"    <username>root@192.0.2.5</username>\n    <time>{revision_time}</time>\n"
        f"    <description>{html.escape(description)}</description>\n  </revision>\n"
        f"  <system>\n    <hostname>{hostname}</hostname>\n    <domain>example.internal</domain>\n"
        f"    <marker>{marker}</marker>\n  </system>\n"
        f"  <users><user><name>homeserver</name><password>{CONFIG_SECRET}</password></user></users>\n</opnsense>\n"
    )


def revision(content_host, seconds, description, marker=""):
    """A history entry: the id Config::backup() names the file by, the revision time it stamps (%0.2f) and the file."""
    revision_time = f"{seconds:0.2f}"
    return {
        "id": f"config-{seconds:.4f}.xml",
        "time": revision_time,
        "description": description,
        "username": "root@192.0.2.5",
        "content": config(content_host, revision_time, description, marker),
    }


def initial_state(version=RELEASE, plugins=("os-isc-dhcp",), revisions=3):
    """A router as it is known, on `version` with `plugins`, a few revisions in its history, the newest last."""
    sets = version.split("_", 1)[0]
    start = time.time() - 3600
    return {
        "version": version,
        "series": ".".join(version.split(".")[:2]),
        "packages": {"base": sets, "kernel": sets, "opnsense": version, "php83": "8.3.36", "unbound": "1.24.1"},
        "plugins": {name: "1.0" for name in plugins},
        # A plugin the router offers but has not installed, which the role must not report.
        "available": {"os-tailscale": "1.4", "os-ddclient": "1.29"},
        "fresh": False,
        "history": [
            revision("router", start + 60 * i, f"/system_general.php made changes {i}") for i in range(revisions)
        ],
        "root_password": ROOT_PASSWORD,
        "interface_mismatch": False,
        "reboot_seconds": 1,
        "down_until": 0,
        "restores": [],
    }


def fresh_state(**more):
    """A freshly installed router: no API key, root's password from the installer, a factory configuration."""
    state = initial_state(**more)
    state["fresh"] = True
    state["history"] = [revision("OPNsense", time.time() - 60, "Factory configuration")]
    return state


def save_revision(state, description, marker=""):
    """A change made on the router: a new revision, as any save in the web UI makes one."""
    seconds = max(float(h["id"][7:-4]) for h in state["history"]) + 1
    state["history"].append(revision("router", seconds, description, marker))
    return state


def running(state):
    """The newest history entry, which is the running configuration."""
    return max(state["history"], key=lambda h: float(h["id"][7:-4]))


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


def backups(state):
    """BackupController::backupsAction: every history entry, newest first, without its file."""
    items = []
    for h in sorted(state["history"], key=lambda h: float(h["id"][7:-4]), reverse=True):
        when = datetime.fromtimestamp(float(h["time"]), UTC).isoformat()
        items.append(
            {
                "time": h["time"],
                "time_iso": when,
                "description": h["description"],
                "username": h["username"],
                "filesize": len(h["content"]),
                "id": h["id"],
            }
        )
    return {"items": items}


def openssl_decrypt(text, password):
    """What Base::decrypt() does: the base64 body after the headers, through openssl enc -d with the headers' own
    cipher, hash and iterations. None when openssl refuses it."""
    head, body, started = {}, [], False
    for line in text.splitlines():
        if line.startswith("---- BEGIN config.xml") or line.startswith("---- END config.xml"):
            continue
        if not started and ":" in line:
            name, _, value = line.partition(":")
            head[name.strip().lower()] = value.strip()
            continue
        started = True
        body.append(line.strip())
    with tempfile.TemporaryDirectory() as work:
        secret, sealed, opened = Path(work, "pass"), Path(work, "enc"), Path(work, "dec")
        secret.write_text(password)
        sealed.write_bytes(base64.b64decode("".join(body)))
        command = ["openssl", "enc", "-d", f"-{head.get('cipher', '').lower()}", "-md", head.get("hash", "").lower()]
        command += [
            "-pbkdf2",
            "-iter",
            head.get("pbkdf2", "0"),
            "-in",
            sealed,
            "-out",
            opened,
            "-pass",
            f"file:{secret}",
        ]
        if subprocess.run(command, capture_output=True).returncode:
            return None
        return opened.read_text()


# The pages, as the PHP renders the parts the role reads: the CSRF input the output handler adds to each POST form,
# the login form, and the alert boxes of print_input_errors() and print_info_box().
def csrf_input(session):
    return f'<input type="hidden" name="{session["key"]}" value="{session["token"]}" autocomplete="new-password" />'


def login_page(session, error=""):
    alert = f'<div class="alert alert-danger" role="alert">{html.escape(error)}</div>' if error else ""
    return (
        f'<html><head></head><body class="page-login">{alert}<form method="post">{csrf_input(session)}'
        '<input id="usernamefld" type="text" name="usernamefld" /><input id="passwordfld" type="password" '
        'name="passwordfld" /><button type="submit" name="login" value="1">Login</button></form></body></html>'
    )


def backup_page(session, info=(), errors=()):
    boxes = ""
    if errors:
        items = "".join(f"<li>{html.escape(e)}</li>" for e in errors)
        boxes += (
            '<div class="col-xs-12"><div class="alert alert-danger" role="alert">\n<button type="button" '
            'class="close" data-dismiss="alert"><span aria-hidden="true">&times;</span><span class="sr-only">'
            "Close</span></button>\n<p>The following input errors were detected:</p>\n<ul>"
            f"{items}</ul></div></div>"
        )
    for line in info:
        boxes += (
            '<div class="col-xs-12">\n  <div class="alert alert-info" role="alert" style="min-height: 65px;">\n    \n'
            f'    <div style="margin-top: 8px;">{line}</div>\n  </div>\n</div>\n'
        )
    return (
        f"<html><head></head><body>{boxes}"
        f'<form method="post" enctype="multipart/form-data">{csrf_input(session)}<input name="conffile" type="file" />'
        '<input name="restore" type="submit" value="Restore configuration" /></form></body></html>'
    )


def handler(state_path, call_log, sessions, address):
    expected = "Basic " + base64.b64encode(f"{API_KEY}:{API_SECRET}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def load(self):
            return json.loads(Path(state_path).read_text())

        def save(self, state):
            Path(state_path).write_text(json.dumps(state, indent=1))

        def log(self, **record):
            with LOCK, Path(call_log).open("a") as log:
                log.write(json.dumps({"address": address, "method": self.command, "path": self.path} | record) + "\n")

        def send(self, status, body, content_type="application/json", headers=()):
            data = body if isinstance(body, bytes) else body.encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def answers(self, state):
            """Whether this address answers now: the router's own unless it is fresh or rebooting, the bootstrap
            address only while it is fresh."""
            if address == "bootstrap":
                return state["fresh"]
            return not state["fresh"] and time.time() >= state["down_until"]

        def session(self):
            cookie = self.headers.get("Cookie") or ""
            sid = dict(p.strip().split("=", 1) for p in cookie.split(";") if "=" in p).get("PHPSESSID")
            if sid not in sessions:
                sid = secrets.token_hex(8)
                sessions[sid] = {"key": secrets.token_urlsafe(12), "token": secrets.token_urlsafe(12), "user": None}
            return sid, sessions[sid]

        def serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            with LOCK:
                state = self.load()
            if not self.answers(state):
                # As an address nothing answers on: the connection closes before any answer.
                self.close_connection = True
                self.log(credentials=False, dropped=True)
                return
            if self.path.startswith("/api/"):
                self.api(state)
            elif self.path == "/diag_backup.php":
                self.web(state, body)
            else:
                self.log(credentials=False)
                self.send(404, "<html>Not found</html>", "text/html")

        def api(self, state):
            authorization = self.headers.get("Authorization")
            self.log(credentials=authorization is not None)
            if state["fresh"] or authorization != expected:
                self.send(401, json.dumps({"status": 401, "message": "Authentication Failed"}))
                return
            if self.command == "GET" and self.path == "/api/core/firmware/info":
                self.send(200, json.dumps(firmware_info(state)))
            elif self.command == "GET" and self.path == "/api/core/backup/backups/this":
                self.send(200, json.dumps(backups(state)))
            elif self.command == "GET" and self.path.startswith("/api/core/backup/download/this/"):
                wanted = self.path.rsplit("/", 1)[-1]
                found = next((h for h in state["history"] if h["id"] == wanted), None)
                # downloadAction sends nothing for a backup it does not have.
                self.send(200, found["content"] if found else b"", "application/octet-stream")
            else:
                self.send(404, json.dumps({"errorMessage": "Endpoint not found", "errorTitle": "Error"}))

        def web(self, state, body):
            sid, session = self.session()
            cookie = (("Set-Cookie", f"PHPSESSID={sid}; path=/; secure; HttpOnly"),)
            if self.command == "GET":
                self.log(credentials=False)
                page = backup_page(session) if session["user"] else login_page(session)
                self.send(200, page, "text/html", cookie)
                return
            fields, files = self.form(body)
            if fields.get(session["key"]) != session["token"]:
                self.log(credentials="passwordfld" in fields or "decrypt_password" in fields, csrf=False)
                self.send(403, "<html><head><title>CSRF check failed</title></head></html>", "text/html")
                return
            if "login" in fields:
                self.log(credentials=True, login=fields.get("usernamefld"))
                if fields.get("usernamefld") == "root" and fields.get("passwordfld") == state["root_password"]:
                    # session_regenerate_id(): a new id, the same session data.
                    sessions.pop(sid)
                    sid = secrets.token_hex(8)
                    sessions[sid] = session | {"user": "root"}
                    headers = (("Set-Cookie", f"PHPSESSID={sid}; path=/; secure; HttpOnly"), ("Location", self.path))
                    self.send(302, "", "text/html", headers)
                else:
                    self.send(200, login_page(session, "Wrong username or password."), "text/html", cookie)
                return
            if not session["user"]:
                self.log(credentials=False)
                self.send(200, login_page(session), "text/html", cookie)
                return
            self.log(credentials=True, restore="restore" in fields)
            self.restore(state, session, fields, files, cookie)

        def form(self, body):
            kind = self.headers.get("Content-Type") or ""
            if kind.startswith("multipart/form-data"):
                message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                    f"Content-Type: {kind}\r\n\r\n".encode() + body
                )
                fields, files = {}, {}
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    data = part.get_payload(decode=True) or b""
                    if part.get_filename() is not None:
                        files[name] = data
                    else:
                        fields[name] = data.decode()
                return fields, files
            return {k: v[0] for k, v in parse_qs(body.decode()).items()}, {}

        def restore(self, state, session, fields, files, cookie):
            text = files.get("conffile", b"").decode(errors="replace")
            data = text
            if fields.get("decrypt"):
                if not fields.get("decrypt_password"):
                    errors = ["You must supply the password for decryption."]
                    self.send(200, backup_page(session, errors=errors), "text/html", cookie)
                    return
                data = openssl_decrypt(text, fields["decrypt_password"])
            if data is None or "<opnsense>" not in data:
                errors = [
                    "The uploaded file could not be decrypted."
                    if data is None
                    else "The configuration could not be restored."
                ]
                self.send(200, backup_page(session, errors=errors), "text/html", cookie)
                return
            said = ["The configuration has been restored."]
            reboot = bool(fields.get("rebootafterrestore"))
            with LOCK:
                state = self.load()
                state["restores"].append(
                    {
                        "address": address,
                        "content": data,
                        "keepconsole": bool(fields.get("keepconsole")),
                        "flush_history": bool(fields.get("flush_history")),
                        "reboot": reboot,
                    }
                )
                if state["interface_mismatch"]:
                    said[0] += " Interfaces do not seem to match, please check the assignments now for missing devices."
                    if reboot:
                        said[0] += " Postponing reboot."
                    reboot = False
                if fields.get("flush_history"):
                    # system flush config_history, then write_config('System restore flushed local history').
                    seconds = time.time()
                    entry_ = revision("router", seconds, "System restore flushed local history")
                    entry_["content"] = data
                    state["history"] = [entry_]
                if reboot:
                    said[0] += " The system is rebooting now. This may take one minute."
                    state["fresh"] = False
                    state["down_until"] = time.time() + state["reboot_seconds"]
                self.save(state)
            self.send(200, backup_page(session, info=said), "text/html", cookie)

        do_GET = serve
        do_POST = serve

        def log_message(self, *args):
            pass

    return Handler


def server(port, cert, key, state, call_log, sessions, address):
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler(state, call_log, sessions, address))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    return httpd


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--call-log", required=True)
    parser.add_argument("--bootstrap-port", type=int)
    parser.add_argument("--bootstrap-cert")
    parser.add_argument("--bootstrap-key")
    args = parser.parse_args()
    if not Path(args.state).exists():
        Path(args.state).write_text(json.dumps(initial_state(), indent=1))
    sessions = {}
    if args.bootstrap_port:
        fresh = server(
            args.bootstrap_port,
            args.bootstrap_cert,
            args.bootstrap_key,
            args.state,
            args.call_log,
            sessions,
            "bootstrap",
        )
        threading.Thread(target=fresh.serve_forever, daemon=True).start()
    server(args.port, args.cert, args.key, args.state, args.call_log, sessions, "router").serve_forever()


if __name__ == "__main__":
    main()
