"""A self-signed certificate and its key, as pveproxy's own is: what the tests' Proxmox API serves, made with
`cryptography` in the test's own process, for the unit tests' in-process server and the integration tests' fake
alike."""

from datetime import UTC, datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def self_signed(work, name):
    """(certificate file, key file) in `work`, named after `name`, valid for a day either side of now."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    (work / f"{name}.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (work / f"{name}.key").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return work / f"{name}.pem", work / f"{name}.key"
