#!/usr/bin/env python3
"""Encrypt or decrypt bot state without ever printing the key or state contents."""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


MAGIC = b"SNAP-STATE-AESGCM-1\x00"
NONCE_BYTES = 12


def encryption_key() -> bytes:
    encoded = os.getenv("STATE_ENCRYPTION_KEY", "").strip()
    if not encoded:
        raise ValueError("missing key")
    try:
        key = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid key encoding") from exc
    if len(key) != 32:
        raise ValueError("invalid key length")
    return key


def encrypt(source: Path, destination: Path) -> None:
    nonce = os.urandom(NONCE_BYTES)
    plaintext = source.read_bytes()
    ciphertext = AESGCM(encryption_key()).encrypt(nonce, plaintext, MAGIC)
    destination.write_bytes(MAGIC + nonce + ciphertext)


def decrypt(source: Path, destination: Path) -> None:
    encrypted = source.read_bytes()
    minimum_size = len(MAGIC) + NONCE_BYTES + 16
    if len(encrypted) < minimum_size or not encrypted.startswith(MAGIC):
        raise ValueError("invalid encrypted state")
    nonce_start = len(MAGIC)
    nonce_end = nonce_start + NONCE_BYTES
    nonce = encrypted[nonce_start:nonce_end]
    ciphertext = encrypted[nonce_end:]
    plaintext = AESGCM(encryption_key()).decrypt(nonce, ciphertext, MAGIC)
    destination.write_bytes(plaintext)


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in {"encrypt", "decrypt"}:
        print("Usage: state_crypto.py encrypt|decrypt SOURCE DESTINATION", file=sys.stderr)
        return 2
    operation, source_raw, destination_raw = sys.argv[1:]
    try:
        if operation == "encrypt":
            encrypt(Path(source_raw), Path(destination_raw))
        else:
            decrypt(Path(source_raw), Path(destination_raw))
    except Exception:
        print("State encryption/decryption failed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
