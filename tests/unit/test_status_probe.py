"""roles/status's module status_probe: DNS lookups and one GET per URL, against servers in this process.

The idempotence tests run it inside ansible-playbook against addresses that never answer; these cover what a real
answer is: a status line and its headers read, the body never waited for, a served certificate read without being
trusted, a name that does not resolve, a server that never answers, and one that is not HTTP at all.
"""

import importlib.util
import socket
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

MODULE = Path(__file__).resolve().parents[2] / "roles" / "status" / "library" / "status_probe.py"
_spec = importlib.util.spec_from_file_location("status_probe", MODULE)
status_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(status_probe)


def serve(handle, context=None):
    """A server on a free local port that answers each connection with `handle(connection)`, in a thread."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def loop():
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            if context is not None:
                try:
                    connection = context.wrap_socket(connection, server_side=True)
                except (OSError, ssl.SSLError):
                    connection.close()
                    continue
            threading.Thread(target=handle, args=(connection,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return listener, listener.getsockname()[1]


def answer(lines, then=None):
    def handle(connection):
        request = b""
        while b"\r\n\r\n" not in request:
            chunk = connection.recv(4096)
            if not chunk:
                break
            request += chunk
        answer.requests.append(request.decode())
        try:
            connection.sendall("\r\n".join(lines).encode() + b"\r\n\r\n")
            if then:
                then(connection)
        except OSError:
            pass
        finally:
            connection.close()

    return handle


answer.requests = []


@pytest.fixture
def certificate(tmp_path):
    """A self-signed certificate for *.homeserver.internal, as the cluster's CA would sign one, and its key."""
    key = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "homeserver.internal private CA")])
    not_after = datetime(2027, 1, 18, 18, 31, 30, tzinfo=UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([]))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("homeserver.internal"), x509.DNSName("*.homeserver.internal")]),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    (tmp_path / "crt.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "key.pem").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "crt.pem", tmp_path / "key.pem")
    return context, not_after


def test_a_redirect_read_from_its_headers_alone():
    answer.requests.clear()
    listener, port = serve(answer(["HTTP/1.1 302 Found", "Location: /web/", "Content-Length: 0"]))
    with listener:
        [found] = status_probe.probe_all([{"url": f"http://localhost:{port}/web?x=1", "address": "127.0.0.1"}], 5)
    assert (found["status"], found["location"], found["error"], found["certificate"]) == (302, "/web/", "", None)
    assert "127.0.0.1" in found["resolved"] and found["dns"] == ""
    # The request named the host as the browser would, and asked for the page's path.
    assert answer.requests[0].startswith(f"GET /web?x=1 HTTP/1.1\r\nHost: localhost:{port}\r\n")


def test_the_body_is_never_waited_for():
    def endless(connection):
        for _ in range(100):
            connection.sendall(b"x" * 4096)
            time.sleep(0.1)

    listener, port = serve(answer(["HTTP/1.1 200 OK", "Content-Length: 1000000000"], endless))
    with listener:
        start = time.monotonic()
        [found] = status_probe.probe_all([{"url": f"http://localhost:{port}/", "address": "127.0.0.1"}], 5)
    assert found["status"] == 200 and time.monotonic() - start < 2


def test_the_served_certificate_is_read_not_trusted(certificate):
    context, not_after = certificate
    listener, port = serve(answer(["HTTP/1.1 403 Forbidden", "Content-Length: 9"]), context)
    with listener:
        [found] = status_probe.probe_all(
            [{"url": f"https://jellyfin.homeserver.internal:{port}/", "address": "127.0.0.1"}], 5
        )
    assert found["status"] == 403 and found["error"] == ""
    assert found["certificate"] == {
        "issuer": "CN=homeserver.internal private CA",
        "names": ["*.homeserver.internal", "homeserver.internal"],
        "not_after": not_after.isoformat(),
    }


def test_a_name_that_does_not_resolve():
    [given, alone] = status_probe.probe_all(
        [
            {"url": "https://app.homeserver.invalid/", "address": "127.0.0.1"},
            {"url": "https://app.homeserver.invalid/"},
        ],
        2,
    )
    assert given["resolved"] == [] and given["dns"] and given["address"] == "127.0.0.1"
    assert alone["status"] is None and alone["error"] == "app.homeserver.invalid does not resolve"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_no_answer_refused_or_not_http():
    def silent(connection):
        time.sleep(3)
        connection.close()

    listener, port = serve(silent)
    garbage, garbage_port = serve(lambda c: (c.sendall(b"SSH-2.0-OpenSSH_10.0\r\n\r\n"), c.close()))
    with listener, garbage:
        start = time.monotonic()
        found = status_probe.probe_all(
            [
                {"url": f"http://127.0.0.1:{port}/"},
                {"url": f"http://127.0.0.1:{free_port()}/"},
                {"url": f"http://127.0.0.1:{garbage_port}/"},
                {"url": "rtsp://127.0.0.1:8554"},
            ],
            1,
        )
    assert time.monotonic() - start < 3
    assert [f["status"] for f in found] == [None, None, None, None]
    assert found[0]["error"] == "timed out"
    assert "refused" in found[1]["error"].lower()
    assert found[2]["error"] == "no HTTP status line in the answer"
    assert found[3]["error"] == "not an http or https URL"


def test_nothing_to_ask():
    assert status_probe.probe_all([], 5) == []
