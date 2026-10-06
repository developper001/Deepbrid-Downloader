from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tkinter import StringVar, Toplevel, ttk
from typing import TypeAlias

from .app_info import LatestRelease, ReleaseAsset


@dataclass(frozen=True)
class LogEvent:
    timestamp: str
    message: str
    is_exception: bool = False


@dataclass(frozen=True)
class RefreshEvent:
    pass


@dataclass(frozen=True)
class WorkerDoneEvent:
    pass


@dataclass(frozen=True)
class StatusEvent:
    item_id: int
    message: str


@dataclass(frozen=True)
class ProgressEvent:
    item_id: int
    downloaded: int
    total: int | None
    speed: float


@dataclass(frozen=True)
class ApiKeyErrorEvent:
    status_code: int | None
    message: str


@dataclass(frozen=True)
class ApiKeyCheckEvent:
    button: ttk.Button | None
    status: StringVar | None
    message: str


@dataclass(frozen=True)
class StartupApiKeyProblemEvent:
    message: str


@dataclass(frozen=True)
class HostsLoadedEvent:
    hosts: dict[str, str]
    limits: dict[str, str]
    limits_error: str | None
    limits_error_status: int | None


@dataclass(frozen=True)
class HostsErrorEvent:
    message: str
    status_code: int | None


@dataclass(frozen=True)
class StartupUpdateEvent:
    release: LatestRelease
    asset: ReleaseAsset | None


@dataclass(frozen=True)
class UpdateCheckEvent:
    popup: Toplevel
    update_status: StringVar
    check_button: ttk.Button
    release_button: ttk.Button
    update_progress: ttk.Progressbar
    message: str
    release: LatestRelease | None
    asset: ReleaseAsset | None


@dataclass(frozen=True)
class UpdateProgressEvent:
    popup: Toplevel
    update_status: StringVar
    update_progress: ttk.Progressbar
    version: str
    received: int
    total: int


@dataclass(frozen=True)
class UpdateDownloadedEvent:
    popup: Toplevel
    version: str
    staged_path: Path


@dataclass(frozen=True)
class UpdateDownloadFailedEvent:
    popup: Toplevel
    update_status: StringVar
    check_button: ttk.Button | None
    release_button: ttk.Button | None
    update_progress: ttk.Progressbar
    message: str


BackgroundEvent: TypeAlias = (
    LogEvent
    | RefreshEvent
    | WorkerDoneEvent
    | StatusEvent
    | ProgressEvent
    | ApiKeyErrorEvent
    | ApiKeyCheckEvent
    | StartupApiKeyProblemEvent
    | HostsLoadedEvent
    | HostsErrorEvent
    | StartupUpdateEvent
    | UpdateCheckEvent
    | UpdateProgressEvent
    | UpdateDownloadedEvent
    | UpdateDownloadFailedEvent
)
