from __future__ import annotations

import sqlite3
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .secure_store import SecureStore


URL_AAD = b"download-original-url-v1"
DEEPBRID_LINK_AAD = b"download-generated-url-v1"

@dataclass(frozen=True)
class QueueItem:
    id: int
    url: str
    status: str
    downloaded: int
    total: int | None
    filename: str | None
    error: str | None
    deepbrid_link: str | None
    host_status: str
    host_message: str
    enabled: bool
    force: bool
    priority: int


class QueueStore:
    def __init__(self, database_path: Path, secure_store: SecureStore | None = None):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self.secure_store = secure_store or SecureStore(database_path)
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    url_ciphertext BLOB,
                    status TEXT NOT NULL DEFAULT 'queued',
                    downloaded INTEGER NOT NULL DEFAULT 0,
                    total INTEGER,
                    filename TEXT,
                    error TEXT,
                    deepbrid_link TEXT,
                    host_status TEXT NOT NULL DEFAULT 'unknown',
                    host_message TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    force INTEGER NOT NULL DEFAULT 0,
                    priority INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(downloads)")
            }
            migrations = {
                "url_ciphertext": "BLOB",
                "deepbrid_link": "TEXT",
                "host_status": "TEXT NOT NULL DEFAULT 'unknown'",
                "host_message": "TEXT NOT NULL DEFAULT ''",
                "enabled": "INTEGER NOT NULL DEFAULT 1",
                "force": "INTEGER NOT NULL DEFAULT 0",
                "priority": "INTEGER NOT NULL DEFAULT 0",
            }
            for column, definition in migrations.items():
                if column not in columns:
                    connection.execute(f"ALTER TABLE downloads ADD COLUMN {column} {definition}")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, url, url_ciphertext, deepbrid_link, error FROM downloads"
            ).fetchall()
        for row in rows:
            if row["url_ciphertext"] is None:
                original_url = row["url"]
                url_hash = self.secure_store.hash_text(original_url, URL_AAD)
                encrypted_url = self.secure_store.encrypt_text(original_url, URL_AAD)
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE downloads SET url = ?, url_ciphertext = ? WHERE id = ?",
                        (url_hash, encrypted_url, row["id"]),
                    )
            if isinstance(row["deepbrid_link"], str):
                encrypted_link = self.secure_store.encrypt_text(row["deepbrid_link"], DEEPBRID_LINK_AAD)
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE downloads SET deepbrid_link = ? WHERE id = ?",
                        (encrypted_link, row["id"]),
                    )
            elif isinstance(row["deepbrid_link"], bytes):
                self.secure_store.decrypt_text(row["deepbrid_link"], DEEPBRID_LINK_AAD)
            if isinstance(row["error"], str) and row["error"]:
                match = re.search(r"HTTP\s+(\d{3})", row["error"])
                safe_error = f"HTTP {match.group(1)}" if match else "Previous error details cleared"
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE downloads SET error = ? WHERE id = ?",
                        (safe_error, row["id"]),
                    )
        with self._connect() as connection:
            interrupted = connection.execute(
                "SELECT 1 FROM downloads WHERE status IN ('generating', 'downloading', 'retrying') LIMIT 1"
            ).fetchone()
            self.recovered_work = interrupted is not None
            connection.execute(
                "UPDATE downloads SET status = 'queued' WHERE status IN ('generating', 'downloading', 'retrying')"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def add(
        self,
        url: str,
        host_status: str = "up",
        host_message: str = "",
    ) -> bool:
        url_hash = self.secure_store.hash_text(url, URL_AAD)
        encrypted_url = self.secure_store.encrypt_text(url, URL_AAD)
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO downloads (url, url_ciphertext, host_status, host_message) "
                "VALUES (?, ?, ?, ?)",
                (url_hash, encrypted_url, host_status, host_message),
            )
            return cursor.rowcount > 0

    def list_items(self) -> list[QueueItem]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM downloads ORDER BY id"
            ).fetchall()
        return [self._to_item(row) for row in rows]

    def next_item(self) -> QueueItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM downloads WHERE status = 'queued' AND enabled = 1 "
                "AND host_status != 'down' ORDER BY priority, id LIMIT 1"
            ).fetchone()
        return self._to_item(row) if row else None

    def set_priority_order(self, item_ids: list[int]) -> None:
        with self._connect() as connection:
            connection.executemany(
                "UPDATE downloads SET priority = ? WHERE id = ?",
                ((priority, item_id) for priority, item_id in enumerate(item_ids)),
            )

    def retry_item(self, item_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE downloads SET status = 'queued', error = NULL "
                "WHERE id = ? AND status IN ('failed', 'blocked')",
                (item_id,),
            )

    def remove_item(self, item_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM downloads WHERE id = ? AND status != 'downloading'",
                (item_id,),
            )
            return cursor.rowcount > 0

    def set_force_redownload(self, item_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE downloads SET status = 'queued', downloaded = 0, total = NULL, "
                "deepbrid_link = NULL, error = NULL, force = 1 "
                "WHERE id = ? AND status != 'downloading'",
                (item_id,),
            )

    def force_redownload(self, item_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE downloads SET status = 'queued', downloaded = 0, total = NULL, "
                "deepbrid_link = NULL, error = NULL WHERE id = ? AND status != 'downloading'",
                (item_id,),
            )

    def update(self, item_id: int, **fields: object) -> None:
        allowed = {
            "status", "downloaded", "total", "filename", "error", "deepbrid_link",
            "host_status", "host_message", "enabled",
            "force",
        }
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Unsupported queue fields")
        fields = dict(fields)
        if isinstance(fields.get("deepbrid_link"), str):
            fields["deepbrid_link"] = self.secure_store.encrypt_text(
                fields["deepbrid_link"], DEEPBRID_LINK_AAD
            )
        assignments = ", ".join(f"{field} = ?" for field in fields)
        values = [*fields.values(), item_id]
        with self._connect() as connection:
            connection.execute(
                f"UPDATE downloads SET {assignments} WHERE id = ?", values
            )

    def _to_item(self, row: sqlite3.Row) -> QueueItem:
        return QueueItem(
            id=row["id"],
            url=self.secure_store.decrypt_text(bytes(row["url_ciphertext"]), URL_AAD),
            status=row["status"],
            downloaded=row["downloaded"],
            total=row["total"],
            filename=row["filename"],
            error=row["error"],
            deepbrid_link=(
                self.secure_store.decrypt_text(bytes(row["deepbrid_link"]), DEEPBRID_LINK_AAD)
                if row["deepbrid_link"] is not None
                else None
            ),
            host_status=row["host_status"],
            host_message=row["host_message"],
            enabled=bool(row["enabled"]),
            force=bool(row["force"]),
            priority=row["priority"],
        )