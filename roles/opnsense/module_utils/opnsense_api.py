"""Client for OPNsense's HTTPS API and web UI, shared by this role's modules.

The modules run on the controller. The API (``https://<router>/api/<module>/<controller>/<action>``) takes an API key
and secret as HTTP basic authentication. A configuration file can be restored only through the web UI's legacy page,
which takes a username and password and a CSRF token instead. The router's certificate is self-signed, so its SHA-256
is pinned: nothing is sent, neither the key pair nor a password, until the server has presented that certificate.
"""

from __future__ import annotations

import base64
import hashlib
import html
import http.client
import json
import re
import secrets
import ssl
from urllib.parse import urlencode


def connection_argument_spec():
    """The options every module takes to reach the router: its address and port, and the certificate pin.

    ``api_host`` is checked in code rather than marked required so the role can supply it through
    ``module_defaults``.
    """
    return dict(
        api_host=dict(type="str", default=""),
        api_port=dict(type="int", default=443),
        validate_certs=dict(type="bool", default=False),
        api_cert_sha256=dict(type="str", default=""),
        api_timeout=dict(type="int", default=60),
    )


def api_argument_spec():
    """The connection options, and the API key pair of the modules that call the API, checked in code too."""
    return dict(
        connection_argument_spec(),
        api_key=dict(type="str", default="", no_log=True),
        api_secret=dict(type="str", default="", no_log=True),
    )


class OPNsenseError(Exception):
    """Any failure talking to OPNsense."""


class OPNsenseConnectionError(OPNsenseError):
    """The connection failed or timed out before an answer arrived."""


class OPNsenseCertificateError(OPNsenseError):
    """The server's certificate is not the pinned one."""


class OPNsenseAuthError(OPNsenseError):
    """The router refused the key and secret or the password, or the user lacks the privilege."""


def normalise_fingerprint(value):
    return (value or "").replace(":", "").replace(" ", "").lower()


def format_fingerprint(hexdigest):
    return ":".join(hexdigest[i : i + 2] for i in range(0, len(hexdigest), 2)).upper()


# A release as opnsense-version prints it: 26.7, 26.7.8, or 26.7.8_2 with a hotfix. A development release
# (26.7.a_123) has a letter for its third part, and matches nothing.
_RELEASE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:_(\d+))?$")


def release_of(version):
    """The release without its hotfix suffix (26.7.8 for 26.7.8_2), or "" for a version that is not a release."""
    match = _RELEASE.match(str(version or "").strip())
    if not match:
        return ""
    major, minor, patch = match.group(1, 2, 3)
    return f"{major}.{minor}" + (f".{patch}" if patch is not None else "")


def _context(validate_certs):
    context = ssl.create_default_context()
    if not validate_certs:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def peer_fingerprint(host, port=443, timeout=15):
    """The SHA-256 of the certificate the server at ``host`` presents, sending nothing but the TLS handshake."""
    conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=_context(False))
    try:
        conn.connect()
        der = conn.sock.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as exc:
        raise OPNsenseConnectionError(f"cannot connect to {host}:{port}: {exc}") from exc
    finally:
        conn.close()
    return format_fingerprint(hashlib.sha256(der).hexdigest()) if der else ""


class OPNsenseConnection:
    """HTTPS connections to one server, each checked against the pinned certificate before anything is sent."""

    def __init__(self, host, port=443, validate_certs=False, cert_sha256="", timeout=60, what="the API key"):
        if not host:
            raise OPNsenseError("no OPNsense address given")
        # With neither, the credentials would go to whatever answers on the address.
        if not validate_certs and not cert_sha256:
            raise OPNsenseCertificateError(
                f"certificate validation is off and no certificate is pinned, so nothing proves {host} is the router: "
                f"pin its certificate's SHA-256 (opnsense_api_cert_sha256), or validate certificates; {what} was not "
                "sent"
            )
        self.host = host
        self.port = port
        self.timeout = timeout
        self.what = what
        self.cert_sha256 = normalise_fingerprint(cert_sha256)
        self.context = _context(validate_certs)

    def _connect(self):
        """A connection whose server has presented the pinned certificate; nothing has been sent on it yet."""
        conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.context)
        try:
            conn.connect()
        except (OSError, ssl.SSLError) as exc:
            conn.close()
            raise OPNsenseConnectionError(f"cannot connect to {self.host}:{self.port}: {exc}") from exc
        if self.cert_sha256:
            der = conn.sock.getpeercert(binary_form=True)
            actual = hashlib.sha256(der).hexdigest() if der else ""
            if actual != self.cert_sha256:
                conn.close()
                raise OPNsenseCertificateError(
                    f"the server at {self.host} presented certificate SHA-256 {format_fingerprint(actual)}, not the "
                    f"pinned one; {self.what} was not sent"
                )
        return conn

    def _send(self, method, path, body, headers):
        """(status, response headers, body bytes) of one request."""
        conn = self._connect()
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.getheaders(), response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise OPNsenseConnectionError(f"{method} {path}: connection to {self.host} failed: {exc}") from exc
        finally:
            conn.close()


class OPNsenseClient(OPNsenseConnection):
    """The API, with a key pair."""

    def __init__(self, host, api_key, api_secret, port=443, validate_certs=False, cert_sha256="", timeout=60):
        if not api_key or not api_secret:
            raise OPNsenseError("no OPNsense API key and secret given")
        super().__init__(host, port, validate_certs, cert_sha256, timeout)
        token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self._authorization = f"Basic {token}"

    def _request(self, method, path, body=None):
        headers = {"Authorization": self._authorization, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        status, _, raw = self._send(method, path, data, headers)
        if status in (401, 403):
            raise OPNsenseAuthError(
                f"{method} {path}: the router answered {status}: it refused the API key and secret, or their user "
                "lacks the privilege for this page (scripts/runbooks/opnsense.md)"
            )
        if status == 404:
            raise OPNsenseError(f"{method} {path}: the router has no such endpoint (404); is its plugin installed?")
        if status >= 400:
            raise OPNsenseError(f"{method} {path}: the router answered {status}: {raw[:300]!r}")
        return raw

    def call(self, method, path, body=None):
        """Send one request to ``path`` (``/api/...``) and return its JSON answer."""
        raw = self._request(method, path, body)
        try:
            return json.loads(raw.decode(errors="replace"))
        except ValueError as exc:
            raise OPNsenseError(f"{method} {path}: the router's answer is not JSON: {raw[:300]!r}") from exc

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body=None):
        return self.call("POST", path, {} if body is None else body)

    def download(self, path):
        """The raw bytes ``path`` answers with, such as a configuration file."""
        return self._request("GET", path)


# What the legacy pages' CSRF handler adds to every POST form (www/csrf.inc): the session's token under a random name.
_CSRF = re.compile(r'<input type="hidden" name="([^"]+)" value="([^"]+)" autocomplete="new-password" />')
# The login form's fields (www/authgui.inc), which a page shows instead of itself to a session not logged in.
_LOGIN_FORM = 'name="usernamefld"'


def csrf_token(page):
    """(name, value) of the CSRF token a legacy page carries, or None."""
    match = _CSRF.search(page)
    return (html.unescape(match.group(1)), html.unescape(match.group(2))) if match else None


def alerts(page, kind):
    """The text of each message the legacy page shows in its alert boxes of `kind` (danger, info, warning): an input
    error list's items (print_input_errors), or a box's message (print_alert_box), without its close button."""
    found = []
    page = re.sub(r"<button\b.*?</button>", "", page, flags=re.S)
    for box in re.findall(rf'<div class="alert alert-{kind}\b[^"]*"[^>]*>(.*?)</div>\s*</div>', page, re.S):
        items = re.findall(r"<li>(.*?)</li>", box, re.S) or [box]
        for item in items:
            text = html.unescape(re.sub(r"<[^>]+>", " ", item))
            text = " ".join(text.split())
            if text:
                found.append(text)
    return found


class OPNsenseWeb(OPNsenseConnection):
    """The web UI, with a username and password: a session cookie and the legacy pages' CSRF token."""

    def __init__(self, host, port=443, validate_certs=False, cert_sha256="", timeout=60):
        super().__init__(host, port, validate_certs, cert_sha256, timeout, what="the password")
        self.cookies = {}

    def request(self, method, path, body=None, content_type=None):
        """(status, headers, text) of one request in the session, keeping the cookies it sets."""
        headers = {"Accept": "text/html"}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if content_type:
            headers["Content-Type"] = content_type
        status, response_headers, raw = self._send(method, path, body, headers)
        for name, value in response_headers:
            if name.lower() == "set-cookie":
                cookie = value.split(";", 1)[0]
                if "=" in cookie:
                    key, _, val = cookie.partition("=")
                    self.cookies[key.strip()] = val.strip()
        return status, dict((k.lower(), v) for k, v in response_headers), raw.decode(errors="replace")

    def login(self, username, password, path):
        """Log in through the login form `path` shows to a new session, and stay logged in."""
        _, _, page = self.request("GET", path)
        token = csrf_token(page)
        if token is None or _LOGIN_FORM not in page:
            raise OPNsenseError(f"{self.host} shows no login form at {path}: is it OPNsense's web UI?")
        form = {token[0]: token[1], "usernamefld": username, "passwordfld": password, "login": "1"}
        status, _, page = self.request("POST", path, urlencode(form).encode(), "application/x-www-form-urlencoded")
        if status != 302:
            raise OPNsenseAuthError(
                f"the router at {self.host} refused the web UI login of {username} (wrong password, two-factor "
                "authentication, or a locked account)"
            )

    def page(self, path):
        """A page of the logged-in session, and its CSRF token."""
        status, _, page = self.request("GET", path)
        if _LOGIN_FORM in page:
            raise OPNsenseAuthError(f"the router's session ended: {path} shows the login form")
        if status != 200:
            raise OPNsenseError(f"GET {path}: the router answered {status}")
        token = csrf_token(page)
        if token is None:
            raise OPNsenseError(f"{path} carries no CSRF token: the page changed, or its user lacks the privilege")
        return page, token

    def post_form(self, path, fields, files):
        """POST `fields` and `files` ({name: (filename, bytes)}) as multipart/form-data; the answer's text."""
        boundary = "----homeserver" + secrets.token_hex(16)
        parts = []
        for name, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
                + str(value).encode()
                + b"\r\n"
            )
        for name, (filename, data) in files.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n".encode()
                + data
                + b"\r\n"
            )
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        status, _, page = self.request("POST", path, body, f"multipart/form-data; boundary={boundary}")
        if status == 403:
            raise OPNsenseError(f"POST {path}: the router answered 403: its CSRF check failed")
        if status != 200:
            raise OPNsenseError(f"POST {path}: the router answered {status}")
        return page


def open_client(module):
    """An API client from the module's connection options. Raises OPNsenseError."""
    params = module.params
    return OPNsenseClient(
        params["api_host"],
        params["api_key"],
        params["api_secret"],
        port=params["api_port"],
        validate_certs=params["validate_certs"],
        cert_sha256=params["api_cert_sha256"],
        timeout=params["api_timeout"],
    )


def pick(source, keys):
    """The subset of ``source`` named by ``keys``, so a module never returns a whole object."""
    return {key: source.get(key) for key in keys if isinstance(source, dict) and key in source}
