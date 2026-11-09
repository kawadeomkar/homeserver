"""OPNsense's own encrypted configuration format, as its Restore page and installer read it.

OPNsense encrypts a configuration file with ``openssl enc -e -aes-256-cbc -md sha512 -pbkdf2 -iter 100000`` and wraps
the result, base64 in 76-column lines, between ``---- BEGIN config.xml ----`` and ``---- END config.xml ----`` after a
few headers (opnsense/core, ``OPNsense/Backup/Base.php``). This module writes and reads the same format with Python's
standard library and the cryptography package, which ansible-core depends on, so a backup made here is restored on
the router's own page with its password, and one made there is read here.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BEGIN = "---- BEGIN config.xml ----"
END = "---- END config.xml ----"
# OPNsense's current defaults (Base.php's encrypt()).
ITERATIONS = 100000
SALTED = b"Salted__"


class CryptError(Exception):
    """A configuration file that cannot be read as OPNsense's encrypted format, or a wrong password."""


def _key_iv(password, salt, iterations):
    """openssl enc's -pbkdf2 key and IV: PBKDF2-HMAC-SHA512, 32 bytes of key then 16 of IV."""
    derived = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, iterations, 48)
    return derived[:32], derived[32:]


def encrypt(data, password, version="", iterations=ITERATIONS):
    """`data` (bytes) in OPNsense's encrypted format, as text."""
    if not password:
        raise CryptError("no password to encrypt the configuration with")
    salt = os.urandom(8)
    key, iv = _key_iv(password, salt, iterations)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    blob = SALTED + salt + encryptor.update(padded) + encryptor.finalize()
    text = base64.b64encode(blob).decode()
    lines = [text[i : i + 76] for i in range(0, len(text), 76)]
    header = [BEGIN, f"Version: {version}", "Cipher: AES-256-CBC", f"PBKDF2: {iterations}", "Hash: SHA512", ""]
    return "\n".join(header + lines + [END]) + "\n"


def headers(text):
    """The headers of an encrypted configuration file, by lower-case name."""
    found = {}
    for line in text.splitlines():
        if line.startswith(BEGIN):
            continue
        if not line.strip() or line.startswith(END):
            break
        name, _, value = line.partition(":")
        found[name.strip().lower()] = value.strip()
    return found


def is_encrypted(text):
    return text.lstrip().startswith(BEGIN)


def decrypt(text, password):
    """The configuration (bytes) an encrypted file holds. Only AES-256-CBC with PBKDF2 and SHA512, which OPNsense has
    written since 21.7, is read."""
    if not is_encrypted(text):
        raise CryptError("not an encrypted OPNsense configuration file")
    found = headers(text)
    if found.get("cipher", "").upper() != "AES-256-CBC" or found.get("hash", "").upper() != "SHA512":
        raise CryptError(f"an encryption this module does not read: {found.get('cipher')}, {found.get('hash')}")
    try:
        iterations = int(found.get("pbkdf2", ""))
    except ValueError:
        raise CryptError("no PBKDF2 iteration count: a file from before OPNsense 21.7") from None
    body = []
    started = False
    for line in text.splitlines():
        if line.startswith(BEGIN):
            continue
        if line.startswith(END):
            break
        if not started:
            started = not line.strip()
            continue
        body.append(line.strip())
    try:
        blob = base64.b64decode("".join(body), validate=True)
    except ValueError as exc:
        raise CryptError("the encrypted configuration's body is not base64") from exc
    if not blob.startswith(SALTED) or len(blob) < 32 or (len(blob) - 16) % 16:
        raise CryptError("the encrypted configuration is not openssl's salted format")
    key, iv = _key_iv(password, blob[8:16], iterations)
    decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    padded = decryptor.update(blob[16:]) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    try:
        data = unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise CryptError("the configuration could not be decrypted: the password is wrong") from None
    # A wrong password still passes the padding check about once in 256 tries.
    if b"<opnsense" not in data[:4096]:
        raise CryptError("the configuration could not be decrypted: the password is wrong")
    return data
