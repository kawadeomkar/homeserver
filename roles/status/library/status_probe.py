#!/usr/bin/python

DOCUMENTATION = r"""
---
module: status_probe
short_description: Look up host names and ask each URL for its status line, without trusting any certificate
description:
  - For each target, looks its host name up in DNS as this machine does, then sends one C(GET) for its URL to the
    target's address (or, without one, to the address its name resolves to), with the host name as the TLS server
    name and the C(Host) header, and closes the connection once the status line and the headers have arrived, so no
    page is downloaded.
  - Checks no certificate and sends no credentials, so it needs neither the cluster's CA nor any secret. It reads the
    certificate an C(https) URL is served, its issuer, names and expiry, for the caller to compare.
  - Every target is asked at once, each within O(timeout) seconds. A name that does not resolve, a connection
    refused or a timeout is reported, not a failure of the task.
  - Changes nothing, so a dry run asks too.
options:
  targets:
    description:
      - The URLs to ask, each C(url) (C(http) or C(https), its host a name or an IPv4 address) with an optional
        C(address), the IPv4 address to connect to instead of the one its name resolves to.
    type: list
    elements: dict
    required: true
  timeout:
    description: Seconds each lookup and each request may take.
    type: int
    default: 5
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
- name: Ask the apps, through Traefik's address, whatever DNS says
  status_probe:
    targets:
      - {url: "https://jellyfin.homeserver.internal/", address: 192.0.2.40}
      - {url: "http://jellyfin.homeserver.internal/", address: 192.0.2.40}
  register: probed
"""

RETURN = r"""
results:
  description:
    - One per target, in order. C(resolved) is what the host name resolves to here (empty when it does not, C(dns)
      then saying why), C(status) the HTTP status (null when there was no answer, C(error) then saying why), and
      C(certificate) what an C(https) URL was served.
  type: list
  elements: dict
  returned: always
  sample:
    - url: "https://jellyfin.homeserver.internal/"
      address: 192.0.2.40
      resolved: [192.0.2.40]
      dns: ""
      status: 302
      location: /web/
      error: ""
      seconds: 0.07
      certificate: {issuer: "CN=homeserver.internal private CA,O=homeserver", names: ["*.homeserver.internal"],
                    not_after: "2027-01-18T18:31:30+00:00"}
"""

import ipaddress
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from ansible.module_utils.basic import AnsibleModule

try:
    from cryptography import x509
except ImportError:  # pragma: no cover - ansible-core installs it
    x509 = None

# The most of an answer read before giving up on finding the end of its headers.
HEADERS_MAX = 65536
USER_AGENT = "homeserver-status"


def resolve(host, timeout):
    """(addresses, why) for a host name as this machine resolves it; an address resolves to itself."""
    try:
        ipaddress.IPv4Address(host)
        return [host], ""
    except ValueError:
        pass
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(socket.getaddrinfo, host, None, socket.AF_INET, socket.SOCK_STREAM)
        try:
            found = future.result(timeout=timeout)
        except TimeoutError:
            return [], f"no answer within {timeout} s"
        except OSError as exc:
            return [], exc.strerror or str(exc)
    return sorted({entry[4][0] for entry in found}), ""


def certificate(der):
    """What a served certificate says, read without trusting it."""
    if not der or x509 is None:
        return None
    try:
        cert = x509.load_der_x509_certificate(der)
    except ValueError:
        return None
    try:
        names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
            x509.DNSName
        )
    except x509.ExtensionNotFound:
        names = []
    return {
        "issuer": cert.issuer.rfc4514_string(),
        "names": sorted(names),
        "not_after": cert.not_valid_after_utc.isoformat(),
    }


def headers(sock):
    """The status line and headers, read until the blank line that ends them; never the body."""
    data = b""
    while b"\r\n\r\n" not in data and len(data) < HEADERS_MAX:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    lines = data.split(b"\r\n\r\n", 1)[0].decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise ValueError("no HTTP status line in the answer")
    fields = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        fields.setdefault(name.strip().lower(), value.strip())
    return int(parts[1]), fields


def ask(url, address, timeout):
    """One GET for `url`, sent to `address`, closed once the headers have arrived."""
    split = urlsplit(url)
    host, secure = split.hostname or "", split.scheme == "https"
    port = split.port or (443 if secure else 80)
    path = (split.path or "/") + (f"?{split.query}" if split.query else "")
    out = {"status": None, "location": "", "error": "", "certificate": None}
    start = time.monotonic()
    raw = sock = None
    try:
        raw = sock = socket.create_connection((address, port), timeout=timeout)
        if secure:
            context = ssl.create_default_context()
            # Python's default already refuses anything older; said here too, for code scanning.
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            sock = context.wrap_socket(raw, server_hostname=host)
            out["certificate"] = certificate(sock.getpeercert(binary_form=True))
        authority = host if split.port is None else f"{host}:{port}"
        sock.sendall(
            f"GET {path} HTTP/1.1\r\nHost: {authority}\r\nUser-Agent: {USER_AGENT}\r\nAccept: */*\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        out["status"], fields = headers(sock)
        out["location"] = fields.get("location", "")
    except (OSError, ValueError) as exc:
        out["error"] = "timed out" if isinstance(exc, TimeoutError) else str(exc)
    finally:
        for each in {id(s): s for s in (sock, raw) if s is not None}.values():
            each.close()
    out["seconds"] = round(time.monotonic() - start, 2)
    return out


def probe(target, timeout):
    """A target's lookup and answer."""
    url = str(target.get("url", ""))
    split = urlsplit(url)
    host = split.hostname or ""
    resolved, why = resolve(host, timeout) if host else ([], "no host name")
    address = str(target.get("address") or "") or (resolved[0] if resolved else "")
    out = {"url": url, "address": address, "resolved": resolved, "dns": why}
    unasked = {"status": None, "location": "", "certificate": None, "seconds": 0.0}
    if split.scheme not in ("http", "https") or not host:
        return out | unasked | {"error": "not an http or https URL"}
    if not address:
        return out | unasked | {"error": f"{host} does not resolve"}
    return out | ask(url, address, timeout)


def probe_all(targets, timeout):
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
        return list(pool.map(lambda t: probe(t, timeout), targets))


def main():
    module = AnsibleModule(
        argument_spec=dict(
            targets=dict(type="list", elements="dict", required=True),
            timeout=dict(type="int", default=5),
        ),
        supports_check_mode=True,
    )
    if module.params["timeout"] < 1:
        module.fail_json(msg="timeout must be at least 1.")
    module.exit_json(changed=False, results=probe_all(module.params["targets"], module.params["timeout"]))


if __name__ == "__main__":
    main()
