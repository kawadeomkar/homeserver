"""Client for OPNsense's HTTPS API, shared by this role's modules.

The modules run on the controller and call ``https://<router>/api/<module>/<controller>/<action>`` with an API key
and secret as HTTP basic authentication. The router's certificate is self-signed, so its SHA-256 is pinned: the key
and secret are sent only once the server has presented that certificate.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import ssl


def api_argument_spec():
    """Connection options every module accepts.

    ``api_host``, ``api_key`` and ``api_secret`` are checked in code rather than marked required so the role can
    supply them through ``module_defaults``.
    """
    return dict(
        api_host=dict(type="str", default=""),
        api_port=dict(type="int", default=443),
        api_key=dict(type="str", default="", no_log=True),
        api_secret=dict(type="str", default="", no_log=True),
        validate_certs=dict(type="bool", default=False),
        api_cert_sha256=dict(type="str", default=""),
        api_timeout=dict(type="int", default=60),
    )


class OPNsenseError(Exception):
    """Any failure talking to OPNsense."""


class OPNsenseConnectionError(OPNsenseError):
    """The connection failed or timed out before an answer arrived."""


class OPNsenseCertificateError(OPNsenseError):
    """The server's certificate is not the pinned one."""


class OPNsenseAuthError(OPNsenseError):
    """The router refused the key and secret, or the key's user lacks the privilege."""


def normalise_fingerprint(value):
    return value.replace(":", "").replace(" ", "").lower()


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


class OPNsenseClient:
    def __init__(self, host, api_key, api_secret, port=443, validate_certs=False, cert_sha256="", timeout=60):
        if not host:
            raise OPNsenseError("no OPNsense address given")
        if not api_key or not api_secret:
            raise OPNsenseError("no OPNsense API key and secret given")
        # With neither, the key and secret would go to whatever answers on the address.
        if not validate_certs and not cert_sha256:
            raise OPNsenseCertificateError(
                f"certificate validation is off and no certificate is pinned, so nothing proves {host} is the router: "
                "pin its certificate's SHA-256 (opnsense_api_cert_sha256), or validate certificates; the API key was "
                "not sent"
            )
        self.host = host
        self.port = port
        self.timeout = timeout
        self.cert_sha256 = normalise_fingerprint(cert_sha256)
        self.context = ssl.create_default_context()
        if not validate_certs:
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
        self._authorization = f"Basic {token}"

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
                    "pinned one; the API key was not sent"
                )
        return conn

    def call(self, method, path, body=None):
        """Send one request to ``path`` (``/api/...``) and return its JSON answer."""
        conn = self._connect()
        headers = {"Authorization": self._authorization, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=data, headers=headers)
            response = conn.getresponse()
            status, raw = response.status, response.read().decode(errors="replace")
        except (OSError, http.client.HTTPException) as exc:
            raise OPNsenseConnectionError(f"{method} {path}: connection to {self.host} failed: {exc}") from exc
        finally:
            conn.close()
        if status in (401, 403):
            raise OPNsenseAuthError(
                f"{method} {path}: the router answered {status}: it refused the API key and secret, or their user "
                "lacks the privilege for this page (scripts/runbooks/opnsense.md)"
            )
        if status == 404:
            raise OPNsenseError(f"{method} {path}: the router has no such endpoint (404); is its plugin installed?")
        if status >= 400:
            raise OPNsenseError(f"{method} {path}: the router answered {status}: {raw[:300]}")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise OPNsenseError(f"{method} {path}: the router's answer is not JSON: {raw[:300]!r}") from exc

    def get(self, path):
        return self.call("GET", path)

    def post(self, path, body=None):
        return self.call("POST", path, {} if body is None else body)


def open_client(module):
    """A client from the module's connection options. Raises OPNsenseError."""
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
