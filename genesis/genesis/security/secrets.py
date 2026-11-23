"""Credential storage.

Primary backend: the OS keychain via ``keyring`` (Windows Credential Manager on
Windows). Fallback: a Fernet-encrypted file whose key is protected with
Windows DPAPI when available (so only the same Windows user can decrypt it),
otherwise stored with owner-only permissions.

OAuth tokens and API keys are never written to logs (see redaction.py) or to
the SQLite database in plaintext.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
from pathlib import Path
from typing import Optional

from ..core.logging_setup import get_logger

log = get_logger("security")
SERVICE = "Genesis-Desktop-Assistant"


class SecretStore:
    def get(self, key: str) -> Optional[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def set(self, key: str, value: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def delete(self, key: str) -> None:  # pragma: no cover
        raise NotImplementedError

    backend_name = "abstract"


class MemorySecretStore(SecretStore):
    """For tests."""

    backend_name = "memory"

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


class KeyringSecretStore(SecretStore):
    backend_name = "os-keyring"

    def __init__(self) -> None:
        import keyring  # noqa: F401

        self._kr = keyring
        # Windows Credential Manager limits blobs to ~2.5KB; chunk larger values.

    def get(self, key: str) -> Optional[str]:
        count = self._kr.get_password(SERVICE, f"{key}::chunks")
        if count is None:
            return self._kr.get_password(SERVICE, key)
        parts = [self._kr.get_password(SERVICE, f"{key}::{i}") or "" for i in range(int(count))]
        return "".join(parts)

    def set(self, key: str, value: str) -> None:
        self.delete(key)
        size = 1000
        if len(value) <= size:
            self._kr.set_password(SERVICE, key, value)
            return
        chunks = [value[i:i + size] for i in range(0, len(value), size)]
        for i, c in enumerate(chunks):
            self._kr.set_password(SERVICE, f"{key}::{i}", c)
        self._kr.set_password(SERVICE, f"{key}::chunks", str(len(chunks)))

    def delete(self, key: str) -> None:
        import keyring.errors as kerr

        count = None
        try:
            count = self._kr.get_password(SERVICE, f"{key}::chunks")
        except Exception:
            pass
        names = [key]
        if count:
            names += [f"{key}::{i}" for i in range(int(count))] + [f"{key}::chunks"]
        for n in names:
            try:
                self._kr.delete_password(SERVICE, n)
            except kerr.PasswordDeleteError:
                pass
            except Exception:
                pass


def _dpapi_protect(data: bytes) -> Optional[bytes]:
    if not sys.platform.startswith("win"):
        return None
    try:
        import win32crypt  # type: ignore

        return win32crypt.CryptProtectData(data, "Genesis", None, None, None, 0)
    except Exception:
        return None


def _dpapi_unprotect(data: bytes) -> Optional[bytes]:
    try:
        import win32crypt  # type: ignore

        return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]
    except Exception:
        return None


class EncryptedFileSecretStore(SecretStore):
    backend_name = "encrypted-file"

    def __init__(self, directory: Path) -> None:
        from cryptography.fernet import Fernet

        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        key_file = self.dir / "vault.key"
        if key_file.exists():
            raw = key_file.read_bytes()
            if raw.startswith(b"DPAPI:"):
                key = _dpapi_unprotect(base64.b64decode(raw[6:])) or b""
            else:
                key = raw
        else:
            key = Fernet.generate_key()
            protected = _dpapi_protect(key)
            if protected:
                key_file.write_bytes(b"DPAPI:" + base64.b64encode(protected))
            else:
                key_file.write_bytes(key)
            try:
                os.chmod(key_file, 0o600)
            except OSError:
                pass
        self._fernet = Fernet(key)
        self._vault = self.dir / "vault.bin"

    def _load(self) -> dict:
        if not self._vault.exists():
            return {}
        try:
            return json.loads(self._fernet.decrypt(self._vault.read_bytes()).decode("utf-8"))
        except Exception:
            log.error("secret vault could not be decrypted; starting empty")
            return {}

    def _save(self, data: dict) -> None:
        self._vault.write_bytes(self._fernet.encrypt(json.dumps(data).encode("utf-8")))
        try:
            os.chmod(self._vault, 0o600)
        except OSError:
            pass

    def get(self, key: str) -> Optional[str]:
        with self._lock:
            return self._load().get(key)

    def set(self, key: str, value: str) -> None:
        with self._lock:
            data = self._load()
            data[key] = value
            self._save(data)

    def delete(self, key: str) -> None:
        with self._lock:
            data = self._load()
            data.pop(key, None)
            self._save(data)


def create_secret_store(credentials_dir: Path) -> SecretStore:
    if os.getenv("GENESIS_SECRET_BACKEND", "").lower() != "file":
        try:
            store = KeyringSecretStore()
            probe = "genesis-probe"
            store.set(probe, "ok")
            if store.get(probe) == "ok":
                store.delete(probe)
                return store
        except Exception as exc:
            log.warning("OS keyring unavailable (%s); using encrypted file store", type(exc).__name__)
    return EncryptedFileSecretStore(credentials_dir)
