from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


KEYRING_SERVICE = "DeepBridDownloader"
KEYRING_USERNAME = "sqlite-encryption-key"


class SecureStorageError(RuntimeError):
    pass


class SecureStore:
    LEGACY_SECRET_SETTINGS = {
        "usenet_username",
        "usenet_password",
    }

    def __init__(self, database_path: Path):
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS app_settings (name TEXT PRIMARY KEY, value BLOB NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get_api_key(self) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE name = 'api_key'"
            ).fetchone()
        if row is None:
            return None
        encrypted = bytes(row[0])
        if len(encrypted) < 13:
            raise SecureStorageError("The encrypted API key in the database is incomplete.")
        nonce, ciphertext = encrypted[:12], encrypted[12:]
        encryption_key = self._read_encryption_key()
        if encryption_key is None:
            legacy_key = self._get_legacy_encryption_key()
            if legacy_key is None:
                raise SecureStorageError(
                    "The database encryption key is missing and no legacy key is available."
                )
            try:
                legacy_api_key = AESGCM(legacy_key).decrypt(
                    nonce, ciphertext, b"deepbrid-api-key-v1"
                ).decode("utf-8")
            except (InvalidTag, UnicodeDecodeError, ValueError) as error:
                raise SecureStorageError("Could not migrate the legacy encrypted API key.") from error
            self.save_api_key(legacy_api_key)
            if self.get_api_key() != legacy_api_key:
                raise SecureStorageError("Could not verify the portable API-key migration.")
            self._delete_legacy_encryption_key()
            return legacy_api_key
        try:
            return AESGCM(encryption_key).decrypt(
                nonce, ciphertext, b"deepbrid-api-key-v1"
            ).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, ValueError) as error:
            raise SecureStorageError(
                "Could not decrypt the stored API key. The operating-system key may be unavailable."
            ) from error

    def save_api_key(self, api_key: str) -> None:
        if not api_key:
            with self._connect() as connection:
                connection.execute("DELETE FROM app_settings WHERE name = 'api_key'")
            return
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._get_encryption_key()).encrypt(
            nonce, api_key.encode("utf-8"), b"deepbrid-api-key-v1"
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO app_settings (name, value) VALUES ('api_key', ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (nonce + ciphertext,),
            )

    def delete_legacy_usenet_credentials(self) -> None:
        with self._connect() as connection:
            connection.executemany(
                "DELETE FROM app_settings WHERE name = ?",
                ((name,) for name in self.LEGACY_SECRET_SETTINGS),
            )

    def encrypt_text(self, value: str, purpose: bytes) -> bytes:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._get_encryption_key()).encrypt(
            nonce, value.encode("utf-8"), purpose
        )
        return nonce + ciphertext

    def decrypt_text(self, encrypted: bytes, purpose: bytes) -> str:
        if len(encrypted) < 13:
            raise SecureStorageError("An encrypted database value is incomplete.")
        try:
            return AESGCM(self._get_encryption_key()).decrypt(
                encrypted[:12], encrypted[12:], purpose
            ).decode("utf-8")
        except (InvalidTag, UnicodeDecodeError, ValueError) as error:
            raise SecureStorageError("Could not decrypt a database value.") from error

    def hash_text(self, value: str, purpose: bytes) -> str:
        return hmac.new(
            self._get_encryption_key(), purpose + b"\0" + value.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def get_setting(self, name: str) -> str | None:
        if name in {"api_key", "encryption_key"} | self.LEGACY_SECRET_SETTINGS:
            raise ValueError("Secret database values cannot be read as plain settings.")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE name = ?", (name,)
            ).fetchone()
        return bytes(row[0]).decode("utf-8") if row else None

    def set_setting(self, name: str, value: str) -> None:
        if name in {"api_key", "encryption_key"} | self.LEGACY_SECRET_SETTINGS:
            raise ValueError("Secret database values must use their secure storage methods.")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO app_settings (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (name, value.encode("utf-8")),
            )

    def _get_encryption_key(self) -> bytes:
        encryption_key = self._read_encryption_key()
        if encryption_key is None:
            encryption_key = os.urandom(32)
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO app_settings (name, value) VALUES ('encryption_key', ?)",
                    (encryption_key,),
                )
        if len(encryption_key) != 32:
            raise SecureStorageError("The SQLite encryption key is invalid.")
        return encryption_key

    def _read_encryption_key(self) -> bytes | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE name = 'encryption_key'"
            ).fetchone()
        return bytes(row[0]) if row else None

    @staticmethod
    def _get_legacy_encryption_key() -> bytes | None:
        try:
            import keyring

            encoded_key = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
            if encoded_key:
                key = base64.urlsafe_b64decode(encoded_key.encode("ascii"))
                return key if len(key) == 32 else None
        except Exception:
            return None
        return None

    @staticmethod
    def _delete_legacy_encryption_key() -> None:
        try:
            import keyring

            keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
        except Exception:
            pass