"""Quick Junction -- backup encryption at rest.

Uses `cryptography`'s Fernet (AES-128-CBC + HMAC-SHA256, authenticated
symmetric encryption) -- a maintained, audited implementation, not a
hand-rolled scheme. The key never travels inside a backup file or its
metadata; only a SHA-256 fingerprint of the key is recorded, so an operator
can tell which key a backup needs without the key itself being readable
from anything that gets backed up or committed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from scripts._backup_common import BackupError


def generate_key_file(path: Path) -> None:
    if path.exists():
        raise BackupError(f"Refusing to overwrite an existing key file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        import os

        os.chmod(path, 0o600)  # best-effort; Windows ACLs differ, this is harmless there
    except OSError:
        pass


def load_key(path: Path) -> bytes:
    if not path.is_file():
        raise BackupError(
            f"Encryption key file not found: {path}. "
            "Generate one with 'python scripts/backup_database.py --generate-key "
            "<path>', or pass --no-encryption for an unencrypted backup."
        )
    key = path.read_bytes().strip()
    try:
        Fernet(key)
    except Exception as exc:  # cryptography raises various ValueError subtypes
        raise BackupError(f"{path} does not contain a valid Fernet key.") from exc
    return key


def key_fingerprint(key: bytes) -> str:
    """A non-reversible identifier for which key a backup needs -- never
    the key, never anything the key can be recovered from."""
    return hashlib.sha256(key).hexdigest()


def encrypt_bytes(data: bytes, key: bytes) -> bytes:
    return Fernet(key).encrypt(data)


def decrypt_bytes(data: bytes, key: bytes) -> bytes:
    try:
        return Fernet(key).decrypt(data)
    except InvalidToken as exc:
        raise BackupError(
            "Decryption failed: the key does not match this backup, or the "
            "backup file is corrupted."
        ) from exc
