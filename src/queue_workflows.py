from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import Callable, Protocol

from .app_events import BackgroundEvent, StatusEvent
from .deepbrid_client import DeepbridError, safe_filename
from .queue_store import QueueItem, QueueStore
from .queue_types import QueueSource, QueueStatus
from .remote_jobs import RemoteJob


class QueueWorkflowClient(Protocol):
    def generate_link(self, original_url: str) -> tuple[str, str | None]: ...
    def submit_torrent_file(self, torrent_file: bytes, filename: str) -> RemoteJob: ...
    def get_job(self, job_id: str) -> RemoteJob: ...


@dataclass(frozen=True)
class SourceResolution:
    download_url: str | None = None
    filename: str | None = None
    pause_queue: bool = False
    continue_queue: bool = False
    requeue_on_stop: bool = True


@dataclass(frozen=True)
class QueueWorkflowContext:
    store: QueueStore
    events: Queue[BackgroundEvent]
    stop_event: threading.Event
    log: Callable[[str], None]


class QueueSourceWorkflow(Protocol):
    def starting_message(self, item: QueueItem) -> str: ...

    def resolve(
        self,
        client: QueueWorkflowClient,
        item: QueueItem,
        cancel_event: threading.Event,
    ) -> SourceResolution: ...


class PremiumLinkWorkflow:
    def __init__(self, context: QueueWorkflowContext):
        self.context = context

    def starting_message(self, _item: QueueItem) -> str:
        return "Generating premium link..."

    def resolve(
        self,
        client: QueueWorkflowClient,
        item: QueueItem,
        cancel_event: threading.Event,
    ) -> SourceResolution:
        quick_attempts = 0
        while (
            not self.context.stop_event.is_set()
            and not cancel_event.is_set()
        ):
            try:
                download_url, filename = client.generate_link(item.url)
                return SourceResolution(download_url, filename)
            except DeepbridError as error:
                quick_attempts += 1
                self.context.log(
                    f"Link generation attempt {quick_attempts} failed: {error}"
                )
                if not error.retryable:
                    self.context.store.update(
                        item.id,
                        status=QueueStatus.BLOCKED,
                        error=stored_error(error),
                    )
                    self.context.events.put(StatusEvent(item.id, "Blocked by Deepbrid"))
                    if error.skip_queue_item:
                        self.context.log(
                            "Deepbrid does not support this filehost. "
                            "Skipping this item and continuing the queue."
                        )
                    else:
                        self.context.log(
                            "Deepbrid marked this response non-retryable. The queue is paused; "
                            "contact Deepbrid support."
                        )
                    return SourceResolution(
                        pause_queue=not error.skip_queue_item,
                        continue_queue=error.skip_queue_item,
                    )
                if quick_attempts < 5:
                    status = f"Link retry {quick_attempts + 1}/5 in 3s"
                    delay = 3
                else:
                    status = "Retrying link in 1 hour"
                    delay = 3600
                self.context.store.update(
                    item.id,
                    status=QueueStatus.RETRYING,
                    error=stored_error(error),
                )
                self.context.events.put(StatusEvent(item.id, status))
                if cancel_event.wait(delay):
                    break
        return SourceResolution(
            pause_queue=self.context.stop_event.is_set(),
            requeue_on_stop=self.context.stop_event.is_set(),
        )


class UsenetWorkflow:
    def __init__(self, context: QueueWorkflowContext):
        self.context = context

    def starting_message(self, _item: QueueItem) -> str:
        return "Starting direct Usenet download..."

    def resolve(
        self,
        _client: QueueWorkflowClient,
        item: QueueItem,
        _cancel_event: threading.Event,
    ) -> SourceResolution:
        self.context.log(f"Using direct Usenet file URL for queue item {item.id}.")
        return SourceResolution(item.url, item.filename)


class TorrentWorkflow:
    COMPLETE_STATUSES = frozenset(
        {"downloaded", "complete", "completed", "finished", "success"}
    )
    FAILED_STATUSES = frozenset(
        {"error", "dead", "magnet_error", "error_magnet", "virus"}
    )

    def __init__(self, context: QueueWorkflowContext):
        self.context = context

    def starting_message(self, item: QueueItem) -> str:
        if item.remote_file_index is not None:
            return "Refreshing torrent download link..."
        return "Uploading torrent to Deepbrid..."

    def resolve(
        self,
        client: QueueWorkflowClient,
        item: QueueItem,
        cancel_event: threading.Event,
    ) -> SourceResolution:
        if item.remote_file_index is None:
            self._prepare_torrent(client, item, cancel_event)
            return SourceResolution(continue_queue=True, requeue_on_stop=False)
        download_url = self._resolve_torrent_file(client, item, cancel_event)
        return SourceResolution(
            download_url,
            item.filename,
            continue_queue=download_url is None,
        )

    def _prepare_torrent(
        self,
        client: QueueWorkflowClient,
        item: QueueItem,
        cancel_event: threading.Event,
    ) -> None:
        job_id = item.remote_job_id
        if job_id is None:
            torrent_data = self.context.store.torrent_file_data(item.id)
            if torrent_data is None:
                self.context.store.update(
                    item.id,
                    status=QueueStatus.FAILED,
                    error="Torrent file data unavailable",
                )
                self.context.events.put(StatusEvent(item.id, "Torrent file unavailable"))
                self.context.log(f"Torrent source data is missing for queue item {item.id}.")
                return
            try:
                submitted = client.submit_torrent_file(
                    torrent_data,
                    item.filename or "upload.torrent",
                )
            except DeepbridError as error:
                self.context.store.update(
                    item.id,
                    status=(
                        QueueStatus.BLOCKED
                        if not error.retryable
                        else QueueStatus.FAILED
                    ),
                    error=stored_error(error),
                )
                self.context.events.put(StatusEvent(item.id, "Torrent upload failed"))
                self.context.log(f"Torrent upload failed for queue item {item.id}: {error}")
                return
            job_id = submitted.id
            self.context.store.update(item.id, remote_job_id=job_id, torrent_data=None)
            self.context.log(f"Uploaded torrent queue item {item.id} as remote job {job_id}.")

        while not self.context.stop_event.is_set() and not cancel_event.is_set():
            try:
                job = client.get_job(job_id)
            except DeepbridError as error:
                if not error.retryable:
                    self.context.store.update(
                        item.id,
                        status=QueueStatus.BLOCKED,
                        error=stored_error(error),
                    )
                    self.context.events.put(StatusEvent(item.id, "Torrent status unavailable"))
                    self.context.log(f"Could not query torrent job {job_id}: {error}")
                    return
                self.context.store.update(
                    item.id,
                    status=QueueStatus.RETRYING,
                    error=stored_error(error),
                )
                self.context.events.put(StatusEvent(item.id, "Retrying torrent status in 10s"))
                if cancel_event.wait(10):
                    break
                continue

            status = normalize_status(job.status)
            if status in self.COMPLETE_STATUSES:
                if not job.files:
                    self.context.store.update(
                        item.id,
                        status=QueueStatus.FAILED,
                        error="Torrent job returned no files",
                    )
                    self.context.events.put(
                        StatusEvent(item.id, "Torrent has no downloadable files")
                    )
                    self.context.log(
                        f"Torrent job {job_id} completed without any downloadable files."
                    )
                    return
                base_name = Path(item.filename or job.name).stem or f"torrent-{job_id}"
                files = [
                    (
                        remote_file.download_url,
                        safe_filename(
                            f"{base_name}-file-{index + 1}",
                            remote_file.download_url,
                            item.id,
                        ),
                        remote_file.size,
                    )
                    for index, remote_file in enumerate(job.files)
                ]
                self.context.store.expand_torrent_job(item.id, job_id, files)
                self.context.events.put(
                    StatusEvent(item.id, f"Torrent ready; queued {len(files)} file(s)")
                )
                self.context.log(
                    f"Torrent job {job_id} is ready; queued {len(files)} file(s)."
                )
                return
            if status in self.FAILED_STATUSES:
                self.context.store.update(
                    item.id,
                    status=QueueStatus.FAILED,
                    error=f"Torrent job {status}",
                )
                self.context.events.put(StatusEvent(item.id, "Torrent processing failed"))
                self.context.log(f"Torrent job {job_id} reached terminal status {status}.")
                return

            progress = f" {job.progress:.0f}%" if job.progress is not None else ""
            self.context.store.update(item.id, status=QueueStatus.GENERATING, error=None)
            self.context.events.put(
                StatusEvent(item.id, f"Torrent {status.replace('_', ' ')}{progress}")
            )
            if cancel_event.wait(15):
                break

        self.context.store.update(item.id, status=QueueStatus.QUEUED)
        self.context.log(f"Paused torrent job {job_id}.")

    def _resolve_torrent_file(
        self,
        client: QueueWorkflowClient,
        item: QueueItem,
        cancel_event: threading.Event,
    ) -> str | None:
        if item.remote_job_id is None or item.remote_file_index is None:
            self.context.store.update(
                item.id,
                status=QueueStatus.FAILED,
                error="Torrent file reference unavailable",
            )
            return None
        while not self.context.stop_event.is_set() and not cancel_event.is_set():
            try:
                job = client.get_job(item.remote_job_id)
            except DeepbridError as error:
                if not error.retryable:
                    self.context.store.update(
                        item.id,
                        status=QueueStatus.BLOCKED,
                        error=stored_error(error),
                    )
                    self.context.events.put(StatusEvent(item.id, "Torrent link unavailable"))
                    self.context.log(
                        f"Could not refresh torrent job {item.remote_job_id}: {error}"
                    )
                    return None
                self.context.store.update(
                    item.id,
                    status=QueueStatus.RETRYING,
                    error=stored_error(error),
                )
                self.context.events.put(StatusEvent(item.id, "Retrying torrent link in 10s"))
                if cancel_event.wait(10):
                    break
                self.context.store.update(
                    item.id,
                    status=QueueStatus.DOWNLOADING,
                    error=None,
                )
                continue
            status = normalize_status(job.status)
            if status in self.FAILED_STATUSES:
                self.context.store.update(
                    item.id,
                    status=QueueStatus.FAILED,
                    error=f"Torrent job {status}",
                )
                self.context.events.put(StatusEvent(item.id, "Torrent processing failed"))
                return None
            if item.remote_file_index < len(job.files):
                return job.files[item.remote_file_index].download_url
            if status in self.COMPLETE_STATUSES:
                self.context.store.update(
                    item.id,
                    status=QueueStatus.FAILED,
                    error="Torrent file link unavailable",
                )
                self.context.events.put(StatusEvent(item.id, "Torrent file link unavailable"))
                return None
            self.context.events.put(
                StatusEvent(item.id, f"Torrent {status.replace('_', ' ')}")
            )
            if cancel_event.wait(15):
                break
        self.context.store.update(item.id, status=QueueStatus.QUEUED)
        return None


def stored_error(error: Exception) -> str:
    if isinstance(error, DeepbridError) and error.status_code is not None:
        return f"HTTP {error.status_code}"
    return type(error).__name__


def normalize_status(status: str) -> str:
    return status.casefold().replace("-", "_").replace(" ", "_")
