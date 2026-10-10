from __future__ import annotations

import threading
import time
from pathlib import Path
from queue import Queue
from typing import Callable

from .app_events import (
    ApiKeyErrorEvent,
    BackgroundEvent,
    ProgressEvent,
    RefreshEvent,
    StatusEvent,
    WorkerDoneEvent,
)
from .deepbrid_client import DeepbridClient, DeepbridError, safe_filename
from .queue_store import QueueStore
from .queue_types import QueueSource, QueueStatus


class QueueRunner:
    def __init__(
        self,
        store: QueueStore,
        api_key: str,
        output_dir: Path,
        events: Queue[BackgroundEvent],
        stop_event: threading.Event,
        active_cancel_events: dict[int, threading.Event],
        log: Callable[[str], None],
    ):
        self.store = store
        self.api_key = api_key
        self.output_dir = output_dir
        self.events = events
        self.stop_event = stop_event
        self.active_cancel_events = active_cancel_events
        self.log = log

    def run(self) -> None:
        client = DeepbridClient(
            self.api_key,
            log=lambda message: self.log(message.replace(self.api_key, "<REDACTED>")),
        )
        try:
            client.validate_api_key()
        except DeepbridError as error:
            self.events.put(ApiKeyErrorEvent(error.status_code, str(error)))
            self.events.put(WorkerDoneEvent())
            return
        while not self.stop_event.is_set():
            item = self.store.next_item()
            if item is None:
                break
            item_cancel_event = threading.Event()
            self.active_cancel_events[item.id] = item_cancel_event
            guessed_name = item.filename or safe_filename(None, item.url, item.id)
            existing_path = self.output_dir / guessed_name
            if (
                item.source != QueueSource.TORRENT_CLOUD
                and not item.force
                and existing_path.is_file()
            ):
                existing_size = existing_path.stat().st_size
                self.store.update(
                    item.id,
                    status=QueueStatus.SKIPPED,
                    filename=guessed_name,
                    downloaded=existing_size,
                    total=existing_size,
                    error=None,
                    force=0,
                )
                self.log(f"Skipped {guessed_name}; the file already exists in {self.output_dir}.")
                self.events.put(RefreshEvent())
                self.active_cancel_events.pop(item.id, None)
                continue
            self.store.update(item.id, status=QueueStatus.GENERATING, error=None)
            self.log(f"Starting queue item {item.id}.")
            direct_download = item.source == QueueSource.USENET
            torrent_file = (
                item.source == QueueSource.TORRENT_CLOUD
                and item.remote_file_index is not None
            )
            self.events.put(
                StatusEvent(
                    item.id,
                    "Starting direct Usenet download..."
                    if direct_download
                    else "Refreshing torrent download link..."
                    if torrent_file
                    else "Uploading torrent to Deepbrid..."
                    if item.source == QueueSource.TORRENT_CLOUD
                    else "Generating premium link...",
                )
            )
            self.events.put(RefreshEvent())
            generated_url = None
            returned_name = None
            quick_attempts = 0
            blocked = False
            skip_queue_item = False

            if direct_download:
                generated_url = item.url
                returned_name = item.filename
                self.log(f"Using direct Usenet file URL for queue item {item.id}.")
            elif item.source == QueueSource.TORRENT_CLOUD:
                if item.remote_file_index is None:
                    expanded = self._prepare_torrent(client, item, item_cancel_event)
                    self.active_cancel_events.pop(item.id, None)
                    self.events.put(RefreshEvent())
                    if self.stop_event.is_set():
                        break
                    if not expanded:
                        continue
                    continue
                generated_url = self._resolve_torrent_file(
                    client,
                    item,
                    item_cancel_event,
                )
                if not generated_url:
                    self.active_cancel_events.pop(item.id, None)
                    self.events.put(RefreshEvent())
                    if self.stop_event.is_set():
                        self.store.update(item.id, status=QueueStatus.QUEUED)
                        break
                    if item_cancel_event.is_set():
                        self.store.update(item.id, status=QueueStatus.QUEUED)
                    continue
                returned_name = item.filename
            else:
                while (
                    not self.stop_event.is_set()
                    and not item_cancel_event.is_set()
                    and generated_url is None
                ):
                    try:
                        generated_url, returned_name = client.generate_link(item.url)
                    except DeepbridError as error:
                        quick_attempts += 1
                        self.log(f"Link generation attempt {quick_attempts} failed: {error}")
                        if not error.retryable:
                            blocked = True
                            skip_queue_item = error.skip_queue_item
                            self.store.update(
                                item.id,
                                status=QueueStatus.BLOCKED,
                                error=self._stored_error(error),
                            )
                            self.events.put(StatusEvent(item.id, "Blocked by Deepbrid"))
                            if skip_queue_item:
                                self.log(
                                    "Deepbrid does not support this filehost. "
                                    "Skipping this item and continuing the queue."
                                )
                            else:
                                self.log(
                                    "Deepbrid marked this response non-retryable. The queue is paused; "
                                    "contact Deepbrid support."
                                )
                            break
                        if quick_attempts < 5:
                            status = f"Link retry {quick_attempts + 1}/5 in 3s"
                            delay = 3
                        else:
                            status = "Retrying link in 1 hour"
                            delay = 3600
                        self.store.update(
                            item.id,
                            status=QueueStatus.RETRYING,
                            error=self._stored_error(error),
                        )
                        self.events.put(StatusEvent(item.id, status))
                        if item_cancel_event.wait(delay) or self.stop_event.is_set():
                            break

            if self.stop_event.is_set():
                self.store.update(item.id, status=QueueStatus.QUEUED)
                self.active_cancel_events.pop(item.id, None)
                self.log(f"Queue item {item.id} paused.")
                break
            if item_cancel_event.is_set():
                self.store.update(item.id, status=QueueStatus.QUEUED)
                self.active_cancel_events.pop(item.id, None)
                self.log(
                    f"Queue item {item.id} was disabled before transfer; "
                    "moving to the next priority."
                )
                continue
            if blocked:
                self.active_cancel_events.pop(item.id, None)
                self.events.put(RefreshEvent())
                if skip_queue_item:
                    continue
                break
            if not generated_url:
                self.active_cancel_events.pop(item.id, None)
                break

            filename = item.filename or safe_filename(returned_name, item.url, item.id)
            destination = self.output_dir / filename
            self.store.update(
                item.id,
                filename=filename,
                deepbrid_link=generated_url,
                status=QueueStatus.DOWNLOADING,
                error=None,
            )
            if (
                item.source != QueueSource.TORRENT_CLOUD
                and destination.is_file()
                and not item.force
            ):
                existing_size = destination.stat().st_size
                self.store.update(
                    item.id,
                    status=QueueStatus.SKIPPED,
                    downloaded=existing_size,
                    total=existing_size,
                    force=0,
                )
                self.log(f"Skipped {filename}; the file already exists in {self.output_dir}.")
                self.events.put(RefreshEvent())
                self.active_cancel_events.pop(item.id, None)
                continue
            self.log(f"Downloading {filename} to {self.output_dir}")
            self.events.put(StatusEvent(item.id, "Downloading file..."))

            last_database_update = 0.0
            last_ui_update = 0.0
            last_speed_update = time.monotonic()
            last_speed_bytes = item.downloaded

            def progress(downloaded: int, total: int | None) -> None:
                nonlocal last_database_update, last_ui_update, last_speed_update, last_speed_bytes
                now = time.monotonic()
                if now - last_database_update >= 1:
                    self.store.update(
                        item.id,
                        downloaded=downloaded,
                        total=total if total is not None else item.total,
                    )
                    last_database_update = now
                if now - last_ui_update >= 1 or (total and downloaded >= total):
                    elapsed = now - last_speed_update
                    speed = (downloaded - last_speed_bytes) / elapsed if elapsed > 0 else 0
                    if elapsed >= 1:
                        last_speed_update = now
                        last_speed_bytes = downloaded
                    self.events.put(ProgressEvent(item.id, downloaded, total, speed))
                    last_ui_update = now

            size_verified = False

            def record_size_verification(verified: bool) -> None:
                nonlocal size_verified
                size_verified = verified

            def record_download_filename(downloaded_filename: str) -> None:
                nonlocal filename
                filename = downloaded_filename
                self.store.update(item.id, filename=filename)

            try:
                completed = client.download(
                    generated_url,
                    filename,
                    self.output_dir,
                    lambda: self.stop_event.is_set() or item_cancel_event.is_set(),
                    progress,
                    overwrite_existing=item.force,
                    on_size_verified=record_size_verification,
                    on_filename=(
                        record_download_filename
                        if item.source == QueueSource.TORRENT_CLOUD
                        else None
                    ),
                )
                if completed:
                    final_size = (self.output_dir / filename).stat().st_size
                    self.store.update(
                        item.id,
                        status=QueueStatus.COMPLETED,
                        size_verified=size_verified,
                        downloaded=final_size,
                        total=final_size,
                        error=None,
                        force=0,
                    )
                    self.events.put(StatusEvent(item.id, "Completed"))
                    self.log(f"Completed {filename} ({final_size} bytes).")
                else:
                    partial_path = self.output_dir / f".{filename}.part"
                    partial_size = partial_path.stat().st_size if partial_path.exists() else 0
                    self.store.update(
                        item.id,
                        status=QueueStatus.QUEUED,
                        downloaded=partial_size,
                    )
                    self.log(f"Paused {filename} at {partial_size} bytes.")
            except (DeepbridError, OSError) as error:
                self.store.update(
                    item.id,
                    status=QueueStatus.FAILED,
                    error=self._stored_error(error),
                )
                self.events.put(StatusEvent(item.id, "Failed"))
                self.log(f"Download failed for {filename}: {error}")

            self.active_cancel_events.pop(item.id, None)
            self.events.put(RefreshEvent())
            if self.stop_event.is_set():
                break

        self.events.put(WorkerDoneEvent())

    @staticmethod
    def _stored_error(error: Exception) -> str:
        if isinstance(error, DeepbridError) and error.status_code is not None:
            return f"HTTP {error.status_code}"
        return type(error).__name__

    def _prepare_torrent(self, client, item, cancel_event: threading.Event) -> bool:
        job_id = item.remote_job_id
        if job_id is None:
            torrent_data = self.store.torrent_file_data(item.id)
            if torrent_data is None:
                self.store.update(
                    item.id,
                    status=QueueStatus.FAILED,
                    error="Torrent file data unavailable",
                )
                self.events.put(StatusEvent(item.id, "Torrent file unavailable"))
                self.log(f"Torrent source data is missing for queue item {item.id}.")
                return False
            try:
                submitted = client.submit_torrent_file(torrent_data, item.filename or "upload.torrent")
            except DeepbridError as error:
                self.store.update(
                    item.id,
                    status=QueueStatus.BLOCKED if not error.retryable else QueueStatus.FAILED,
                    error=self._stored_error(error),
                )
                self.events.put(StatusEvent(item.id, "Torrent upload failed"))
                self.log(f"Torrent upload failed for queue item {item.id}: {error}")
                return False
            job_id = submitted.id
            self.store.update(item.id, remote_job_id=job_id, torrent_data=None)
            self.log(f"Uploaded torrent queue item {item.id} as remote job {job_id}.")

        while not self.stop_event.is_set() and not cancel_event.is_set():
            try:
                job = client.get_job(job_id)
            except DeepbridError as error:
                if not error.retryable:
                    self.store.update(
                        item.id,
                        status=QueueStatus.BLOCKED,
                        error=self._stored_error(error),
                    )
                    self.events.put(StatusEvent(item.id, "Torrent status unavailable"))
                    self.log(f"Could not query torrent job {job_id}: {error}")
                    return False
                self.store.update(
                    item.id,
                    status=QueueStatus.RETRYING,
                    error=self._stored_error(error),
                )
                self.events.put(StatusEvent(item.id, "Retrying torrent status in 10s"))
                if cancel_event.wait(10):
                    break
                continue

            status = job.status.casefold().replace("-", "_").replace(" ", "_")
            if status in {"downloaded", "complete", "completed", "finished", "success"}:
                if not job.files:
                    self.store.update(
                        item.id,
                        status=QueueStatus.FAILED,
                        error="Torrent job returned no files",
                    )
                    self.events.put(StatusEvent(item.id, "Torrent has no downloadable files"))
                    self.log(f"Torrent job {job_id} completed without any downloadable files.")
                    return False
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
                self.store.expand_torrent_job(item.id, job_id, files)
                self.events.put(
                    StatusEvent(item.id, f"Torrent ready; queued {len(files)} file(s)")
                )
                self.log(f"Torrent job {job_id} is ready; queued {len(files)} file(s).")
                return True
            if status in {
                "error",
                "dead",
                "magnet_error",
                "error_magnet",
                "virus",
            }:
                self.store.update(
                    item.id,
                    status=QueueStatus.FAILED,
                    error=f"Torrent job {status}",
                )
                self.events.put(StatusEvent(item.id, "Torrent processing failed"))
                self.log(f"Torrent job {job_id} reached terminal status {status}.")
                return False

            progress = (
                f" {job.progress:.0f}%"
                if job.progress is not None
                else ""
            )
            self.store.update(item.id, status=QueueStatus.GENERATING, error=None)
            self.events.put(StatusEvent(item.id, f"Torrent {status.replace('_', ' ')}{progress}"))
            if cancel_event.wait(15):
                break

        self.store.update(item.id, status=QueueStatus.QUEUED)
        self.log(f"Paused torrent job {job_id}.")
        return False

    def _resolve_torrent_file(
        self,
        client,
        item,
        cancel_event: threading.Event,
    ) -> str | None:
        if item.remote_job_id is None or item.remote_file_index is None:
            self.store.update(
                item.id,
                status=QueueStatus.FAILED,
                error="Torrent file reference unavailable",
            )
            return None
        while not self.stop_event.is_set() and not cancel_event.is_set():
            try:
                job = client.get_job(item.remote_job_id)
            except DeepbridError as error:
                if not error.retryable:
                    self.store.update(
                        item.id,
                        status=QueueStatus.BLOCKED,
                        error=self._stored_error(error),
                    )
                    self.events.put(StatusEvent(item.id, "Torrent link unavailable"))
                    self.log(f"Could not refresh torrent job {item.remote_job_id}: {error}")
                    return None
                self.store.update(
                    item.id,
                    status=QueueStatus.RETRYING,
                    error=self._stored_error(error),
                )
                self.events.put(StatusEvent(item.id, "Retrying torrent link in 10s"))
                if cancel_event.wait(10):
                    break
                self.store.update(item.id, status=QueueStatus.DOWNLOADING, error=None)
                continue
            status = job.status.casefold().replace("-", "_").replace(" ", "_")
            if status in {"error", "dead", "magnet_error", "error_magnet", "virus"}:
                self.store.update(
                    item.id,
                    status=QueueStatus.FAILED,
                    error=f"Torrent job {status}",
                )
                self.events.put(StatusEvent(item.id, "Torrent processing failed"))
                return None
            if item.remote_file_index < len(job.files):
                return job.files[item.remote_file_index].download_url
            if status in {"downloaded", "complete", "completed", "finished", "success"}:
                self.store.update(
                    item.id,
                    status=QueueStatus.FAILED,
                    error="Torrent file link unavailable",
                )
                self.events.put(StatusEvent(item.id, "Torrent file link unavailable"))
                return None
            self.events.put(StatusEvent(item.id, f"Torrent {status.replace('_', ' ')}"))
            if cancel_event.wait(15):
                break
        self.store.update(item.id, status=QueueStatus.QUEUED)
        return None
