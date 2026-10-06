# -*- coding: utf-8 -*-
"""Client for the TrueNAS JSON-RPC 2.0 WebSocket API, shared by this role's modules.

The modules run on the controller and talk to ``wss://<host>/api/current`` with an API key.
The REST API is deprecated and removed in TrueNAS 26, so it is not used. An API key used over
plain HTTP is revoked by TrueNAS, which is why there is no ``ws://`` fallback.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import time
import traceback

try:
    import websocket  # websocket-client, installed from requirements.txt

    WEBSOCKET_IMPORT_ERROR = None
except ImportError:
    websocket = None
    WEBSOCKET_IMPORT_ERROR = traceback.format_exc()

from ansible.module_utils.basic import missing_required_lib

# Defaults for the options below. TrueNAS allows 20 logins per 60-second window per client, so the
# login wait is one window plus slack.
LOGIN_RATE_LIMIT_WAIT = 75
JOB_TIMEOUT = 1800


def api_argument_spec():
    """Connection and timing options every module accepts.

    ``api_host`` and ``api_key`` are checked in code rather than marked required so the role can
    supply them through ``module_defaults``.
    """
    return dict(
        api_host=dict(type="str", default=""),
        api_port=dict(type="int", default=443),
        api_key=dict(type="str", default="", no_log=True),
        validate_certs=dict(type="bool", default=False),
        api_cert_sha256=dict(type="str", default=""),
        api_timeout=dict(type="int", default=120),
        api_connect_wait=dict(type="int", default=60),
        api_job_timeout=dict(type="int", default=JOB_TIMEOUT),
        api_login_wait=dict(type="int", default=LOGIN_RATE_LIMIT_WAIT),
    )


class TrueNASError(Exception):
    """Any failure talking to TrueNAS."""


class TrueNASCallError(TrueNASError):
    """TrueNAS answered the call with an error: the request reached the middleware."""


class TrueNASConnectionError(TrueNASError):
    """The connection failed, timed out or closed before an answer arrived."""


class TrueNASCertificateError(TrueNASError):
    """The server's certificate is not the pinned one."""


def normalise_fingerprint(value):
    return value.replace(":", "").replace(" ", "").lower()


def format_fingerprint(hexdigest):
    return ":".join(hexdigest[i:i + 2] for i in range(0, len(hexdigest), 2)).upper()


def is_rate_limited(exc):
    return "Rate Limit Exceeded" in str(exc)


def _format_extra(extra):
    """Validation errors arrive as ``[[field, message, errno], ...]``."""
    parts = []
    for item in extra or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            parts.append(f"{item[0]}: {item[1]}")
    return "; ".join(parts)


def _format_error(method, error):
    data = error.get("data") or {}
    reason = data.get("reason") or error.get("message") or "unknown error"
    extra = _format_extra(data.get("extra"))
    return f"{method}: {reason}" + (f" ({extra})" if extra else "")


class TrueNASClient:
    def __init__(
        self,
        host,
        api_key,
        port=443,
        validate_certs=False,
        timeout=120,
        connect_wait=60,
        connect_timeout=10,
        login_wait=LOGIN_RATE_LIMIT_WAIT,
        job_timeout=JOB_TIMEOUT,
        cert_sha256="",
    ):
        if not host:
            raise TrueNASError("no TrueNAS address given")
        if not api_key:
            raise TrueNASError("no TrueNAS API key given")
        self.host = host
        self.url = f"wss://{host}:{port}/api/current"
        self.timeout = timeout
        self.job_timeout = job_timeout
        self._id = 0
        self.ws = None
        sslopt = {} if validate_certs else {"cert_reqs": ssl.CERT_NONE, "check_hostname": False}

        # Retry refused or unreachable connections for up to connect_wait seconds: the web
        # server restarts briefly after UI settings change, and the address moves when the
        # interface is reconfigured.
        deadline = time.monotonic() + max(connect_wait, 0)
        while True:
            try:
                self.ws = websocket.create_connection(self.url, sslopt=sslopt, timeout=connect_timeout)
                break
            except (OSError, websocket.WebSocketException) as exc:
                if time.monotonic() >= deadline:
                    raise TrueNASConnectionError(f"cannot connect to {self.url}: {exc}") from exc
                time.sleep(2)
        self.ws.settimeout(timeout)

        # With a pinned certificate, nothing is sent until the server has proved it is the NAS. This
        # matters because certificate validation is usually off (the default certificate is
        # self-signed) and the API key would otherwise go to whatever answers on the address.
        if cert_sha256:
            der = self.ws.sock.getpeercert(binary_form=True)
            actual = hashlib.sha256(der).hexdigest() if der else ""
            if actual != normalise_fingerprint(cert_sha256):
                self.close()
                raise TrueNASCertificateError(
                    f"the server at {host} presented certificate SHA-256 {format_fingerprint(actual)}, not the "
                    "pinned one; the API key was not sent"
                )

        # TrueNAS allows 20 logins per minute from one address, counted in fixed 60-second
        # windows. Every module run logs in once, so a busy play can hit that; wait for the
        # window to reset rather than fail.
        login_deadline = time.monotonic() + max(login_wait, 0)
        while True:
            try:
                logged_in = self.call("auth.login_with_api_key", api_key)
                break
            except TrueNASCallError as exc:
                if is_rate_limited(exc) and time.monotonic() < login_deadline:
                    time.sleep(10)
                    continue
                self.close()
                raise TrueNASError(f"TrueNAS at {host} refused the login: {exc}") from exc
        if logged_in is not True:
            self.close()
            raise TrueNASError(f"TrueNAS at {host} rejected the API key")

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001 - closing a dead socket must not mask the real error
                pass
            self.ws = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, method, *params, timeout=None):
        """Call ``method`` and return its result. ``timeout`` overrides the default for this call."""
        if self.ws is None:
            raise TrueNASConnectionError(f"{method}: not connected")
        self._id += 1
        request_id = self._id
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": list(params)}
        if timeout is not None:
            self.ws.settimeout(timeout)
        try:
            self.ws.send(json.dumps(request))
            while True:
                message = json.loads(self.ws.recv())
                # The server also pushes notifications (no id) and may answer other ids.
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise TrueNASCallError(_format_error(method, message["error"]))
                return message.get("result")
        except (OSError, websocket.WebSocketException, ValueError) as exc:
            raise TrueNASConnectionError(f"{method}: connection to {self.host} failed: {exc}") from exc
        finally:
            if timeout is not None and self.ws is not None:
                self.ws.settimeout(self.timeout)

    def job(self, method, *params, timeout=None, poll=2):
        """Call a job method and wait for it. Returns the job's result, raises on failure."""
        timeout = self.job_timeout if timeout is None else timeout
        job_id = self.call(method, *params)
        deadline = time.monotonic() + timeout
        while True:
            jobs = self.call("core.get_jobs", [["id", "=", job_id]])
            if not jobs:
                raise TrueNASError(f"{method}: job {job_id} disappeared")
            job = jobs[0]
            state = job.get("state")
            if state == "SUCCESS":
                return job.get("result")
            if state in ("FAILED", "ABORTED"):
                extra = _format_extra((job.get("exc_info") or {}).get("extra"))
                error = (job.get("error") or state).strip()
                raise TrueNASCallError(f"{method}: {error}" + (f" ({extra})" if extra else ""))
            if time.monotonic() >= deadline:
                raise TrueNASError(f"{method}: job {job_id} still {state} after {timeout}s")
            time.sleep(poll)


def require_websocket(module):
    if websocket is None:
        module.fail_json(msg=missing_required_lib("websocket-client"), exception=WEBSOCKET_IMPORT_ERROR)


def open_client(module, host=None, connect_wait=None, connect_timeout=10, login_wait=None):
    """Open a client from the module's connection options. Raises TrueNASError."""
    require_websocket(module)
    params = module.params
    return TrueNASClient(
        host or params["api_host"],
        params["api_key"],
        port=params["api_port"],
        validate_certs=params["validate_certs"],
        timeout=params["api_timeout"],
        connect_wait=params["api_connect_wait"] if connect_wait is None else connect_wait,
        connect_timeout=connect_timeout,
        login_wait=params["api_login_wait"] if login_wait is None else login_wait,
        job_timeout=params["api_job_timeout"],
        cert_sha256=params["api_cert_sha256"],
    )


def connect(module, host=None, connect_wait=None):
    """Open a client from the module's connection options, failing the module on error."""
    try:
        return open_client(module, host=host, connect_wait=connect_wait)
    except TrueNASError as exc:
        module.fail_json(msg=str(exc))


def _canonical(value):
    """Lists of scalars compare as sets: TrueNAS does not promise to keep their order."""
    if isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
        return sorted(json.dumps(item, sort_keys=True) for item in value)
    return value


def differences(current, desired):
    """Return the entries of ``desired`` that differ from ``current``.

    Only the keys of ``desired`` are compared. A nested dict is compared the same way, and when it
    differs the returned value is the current dict with the desired keys laid over it, because
    TrueNAS replaces a nested object (a schedule, say) rather than merging into it.
    """
    changed = {}
    current = current if isinstance(current, dict) else {}
    for key, want in desired.items():
        have = current.get(key)
        if isinstance(want, dict) and isinstance(have, dict):
            if differences(have, want):
                changed[key] = {**have, **want}
        elif _canonical(have) != _canonical(want):
            changed[key] = want
    return changed


def pick(source, keys):
    """The subset of ``source`` named by ``keys``; used so modules never return whole objects,
    some of which (``system.general.config``) carry private keys."""
    return {key: source.get(key) for key in keys if isinstance(source, dict) and key in source}
