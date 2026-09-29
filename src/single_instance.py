from __future__ import annotations

from pathlib import Path

from filelock import FileLock, Timeout


class SingleInstanceLock:
    def __init__(self, lock: FileLock):
        self._lock = lock
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._lock.release()


def acquire_single_instance(
    lock_directory: Path,
    application_id: str = "DeepbridDownloader",
) -> SingleInstanceLock | None:
    lock_directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(lock_directory / f".{application_id}.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return None
    return SingleInstanceLock(lock)