from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class RemoteJobFile:
    name: str
    download_url: str
    size: int | None = None


@dataclass(frozen=True)
class RemoteJob:
    id: str
    name: str
    status: str
    progress: float | None = None
    speed: str | None = None
    seeders: int | None = None
    files: tuple[RemoteJobFile, ...] = ()


class RemoteJobProvider(Protocol):
    def submit_magnet(self, magnet: str) -> RemoteJob: ...
    def submit_torrent_file(self, torrent_file: bytes, filename: str) -> RemoteJob: ...
    def get_job(self, job_id: str) -> RemoteJob: ...
    def list_jobs(self) -> Sequence[RemoteJob]: ...
    def delete_jobs(self, job_ids: Sequence[str] | None = None) -> None: ...
