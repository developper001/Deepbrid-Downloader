from __future__ import annotations

import sqlite3
import re
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .secure_store import SecureStore
from .queue_types import QueueSource, QueueStatus


URL_AAD = b"download-original-url-v1"
DEEPBRID_LINK_AAD = b"download-generated-url-v1"
TORRENT_DATA_AAD = b"torrent-file-data-v1"
MAX_TORRENT_FILE_SIZE = 5 * 1024 * 1024

@dataclass(frozen=True)
class QueueItem:
    id: int
    url: str
    status: str
    size_verified: bool
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
    source: QueueSource
    remote_job_id: str | None
    remote_file_index: int | None


class QueueStore:
    def __init__(self, database_path: Path, secure_store: SecureStore | None = None):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = database_path
        self.secure_store = secure_store or SecureStore(database_path)
        with self._connect() as connection:
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    url_ciphertext BLOB,
                    source TEXT NOT NULL DEFAULT '{QueueSource.PREMIUM_LINK.value}',
                    remote_job_id TEXT,
                    remote_file_index INTEGER,
                    torrent_data BLOB,
                    status TEXT NOT NULL DEFAULT '{QueueStatus.QUEUED}',
                    size_verified INTEGER NOT NULL DEFAULT 0,
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
                "source": f"TEXT NOT NULL DEFAULT '{QueueSource.PREMIUM_LINK.value}'",
                "remote_job_id": "TEXT",
                "remote_file_index": "INTEGER",
                "torrent_data": "BLOB",
                "size_verified": "INTEGER NOT NULL DEFAULT 0",
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
            connection.execute(
                "UPDATE downloads SET source = ? WHERE host_message = ? AND source = ?",
                (
                    QueueSource.USENET.value,
                    "Usenet Finder",
                    QueueSource.PREMIUM_LINK.value,
                ),
            )
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
                "SELECT 1 FROM downloads WHERE status IN (?, ?, ?) LIMIT 1",
                tuple(QueueStatus.INTERRUPTED),
            ).fetchone()
            self.recovered_work = interrupted is not None
            connection.execute(
                "UPDATE downloads SET status = ? WHERE status IN (?, ?, ?)",
                (QueueStatus.QUEUED, *QueueStatus.INTERRUPTED),
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
        filename: str | None = None,
        total: int | None = None,
        source: QueueSource = QueueSource.PREMIUM_LINK,
    ) -> bool:
        if total is not None and (
            isinstance(total, bool) or not isinstance(total, int) or total < 0
        ):
            raise ValueError("Queue item total must be a non-negative integer.")
        if not isinstance(source, QueueSource):
            raise ValueError(f"Unsupported queue source: {source!r}")
        url_hash = self.secure_store.hash_text(url, URL_AAD)
        encrypted_url = self.secure_store.encrypt_text(url, URL_AAD)
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO downloads "
                "(url, url_ciphertext, source, host_status, host_message, filename, total) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    url_hash,
                    encrypted_url,
                    source.value,
                    host_status,
                    host_message,
                    filename,
                    total,
                ),
            )
            return cursor.rowcount > 0

    def add_torrent_file(self, filename: str, contents: bytes) -> bool:
        if not filename.lower().endswith(".torrent"):
            raise ValueError("Torrent files must have a .torrent extension.")
        if not contents or len(contents) > MAX_TORRENT_FILE_SIZE:
            raise ValueError("Torrent files must be between 1 byte and 5 MiB.")
        identity = f"torrent-upload:{hashlib.sha256(contents).hexdigest()}"
        url_hash = self.secure_store.hash_text(identity, URL_AAD)
        encrypted_url = self.secure_store.encrypt_text(identity, URL_AAD)
        encrypted_contents = self.secure_store.encrypt_bytes(contents, TORRENT_DATA_AAD)
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO downloads "
                "(url, url_ciphertext, source, host_status, host_message, filename, torrent_data) "
                "VALUES (?, ?, ?, 'up', 'Torrent cloud', ?, ?)",
                (
                    url_hash,
                    encrypted_url,
                    QueueSource.TORRENT_CLOUD.value,
                    filename,
                    encrypted_contents,
                ),
            )
            return cursor.rowcount > 0

    def torrent_file_data(self, item_id: int) -> bytes | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT torrent_data FROM downloads WHERE id = ?", (item_id,)
            ).fetchone()
        if row is None or row["torrent_data"] is None:
            return None
        return self.secure_store.decrypt_bytes(bytes(row["torrent_data"]), TORRENT_DATA_AAD)

    def expand_torrent_job(
        self,
        item_id: int,
        job_id: str,
        files: list[tuple[str, str, int | None]],
    ) -> None:
        if not files:
            raise ValueError("A completed torrent job must contain downloadable files.")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT url, source FROM downloads WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None or row["source"] != QueueSource.TORRENT_CLOUD.value:
                raise ValueError("Torrent queue item was not found.")
            for index, (link, filename, size) in enumerate(files):
                identity = f"torrent-job:{job_id}:file:{index}"
                url_hash = self.secure_store.hash_text(identity, URL_AAD)
                encrypted_url = self.secure_store.encrypt_text(identity, URL_AAD)
                encrypted_link = self.secure_store.encrypt_text(link, DEEPBRID_LINK_AAD)
                if index == 0:
                    connection.execute(
                        "UPDATE downloads SET url = ?, url_ciphertext = ?, filename = ?, "
                        "total = ?, downloaded = 0, deepbrid_link = ?, remote_job_id = ?, "
                        "remote_file_index = 0, torrent_data = NULL, status = ?, error = NULL "
                        "WHERE id = ?",
                        (
                            url_hash,
                            encrypted_url,
                            filename,
                            size,
                            encrypted_link,
                            job_id,
                            QueueStatus.QUEUED,
                            item_id,
                        ),
                    )
                else:
                    connection.execute(
                        "INSERT INTO downloads "
                        "(url, url_ciphertext, source, remote_job_id, remote_file_index, "
                        "status, total, filename, deepbrid_link, host_status, host_message) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'up', 'Torrent cloud')",
                        (
                            url_hash,
                            encrypted_url,
                            QueueSource.TORRENT_CLOUD.value,
                            job_id,
                            index,
                            QueueStatus.QUEUED,
                            size,
                            filename,
                            encrypted_link,
                        ),
                    )

    def list_items(self) -> list[QueueItem]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM downloads ORDER BY id"
            ).fetchall()
        return [self._to_item(row) for row in rows]

    def next_item(self) -> QueueItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM downloads WHERE status = ? AND enabled = 1 "
                "AND host_status != 'down' ORDER BY priority, id LIMIT 1",
                (QueueStatus.QUEUED,),
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
                "UPDATE downloads SET status = ?, error = NULL "
                "WHERE id = ? AND status IN (?, ?)",
                (QueueStatus.QUEUED, item_id, QueueStatus.FAILED, QueueStatus.BLOCKED),
            )

    def remove_item(self, item_id: int) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM downloads WHERE id = ? AND status != ?",
                (item_id, QueueStatus.DOWNLOADING),
            )
            return cursor.rowcount > 0

    def set_force_redownload(self, item_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE downloads SET status = ?, downloaded = 0, total = NULL, "
                "deepbrid_link = NULL, error = NULL, force = 1, size_verified = 0 "
                "WHERE id = ? AND status != ?",
                (QueueStatus.QUEUED, item_id, QueueStatus.DOWNLOADING),
            )

    def force_redownload(self, item_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE downloads SET status = ?, downloaded = 0, total = NULL, "
                "deepbrid_link = NULL, error = NULL, size_verified = 0 "
                "WHERE id = ? AND status != ?",
                (QueueStatus.QUEUED, item_id, QueueStatus.DOWNLOADING),
            )

    def update(self, item_id: int, **fields: object) -> None:
        allowed = {
            "status", "downloaded", "total", "filename", "error", "deepbrid_link",
            "host_status", "host_message", "enabled", "size_verified",
            "force", "remote_job_id", "remote_file_index", "torrent_data",
        }
        if not fields or not fields.keys() <= allowed:
            raise ValueError("Unsupported queue fields")
        fields = dict(fields)
        if "status" in fields:
            status = fields["status"]
            if not isinstance(status, str) or status not in QueueStatus.ALL:
                raise ValueError(f"Unsupported queue status: {status!r}")
        if isinstance(fields.get("deepbrid_link"), str):
            fields["deepbrid_link"] = self.secure_store.encrypt_text(
                fields["deepbrid_link"], DEEPBRID_LINK_AAD
            )
        if fields.get("torrent_data") is not None:
            torrent_data = fields["torrent_data"]
            if not isinstance(torrent_data, bytes):
                raise ValueError("Torrent data must be bytes or None.")
            fields["torrent_data"] = self.secure_store.encrypt_bytes(
                torrent_data,
                TORRENT_DATA_AAD,
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
            size_verified=bool(row["size_verified"]),
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
            source=QueueSource(row["source"]),
            remote_job_id=row["remote_job_id"],
            remote_file_index=row["remote_file_index"],
        )