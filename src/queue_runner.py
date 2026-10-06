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
            if not item.force and existing_path.is_file():
                existing_size = existing_path.stat().st_size
                self.store.update(
                    item.id,
                    status="skipped",
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
            self.store.update(item.id, status="generating", error=None)
            self.log(f"Starting queue item {item.id}.")
            direct_download = item.host_message == "Usenet Finder"
            self.events.put(
                StatusEvent(
                    item.id,
                    "Starting direct Usenet download..."
                    if direct_download
                    else "Generating premium link...",
                )
            )
            self.events.put(RefreshEvent())
            generated_url = None
            returned_name = None
            quick_attempts = 0
            blocked = False

            if direct_download:
                generated_url = item.url
                returned_name = item.filename
                self.log(f"Using direct Usenet file URL for queue item {item.id}.")
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
                            self.store.update(
                                item.id,
                                status="blocked",
                                error=self._stored_error(error),
                            )
                            self.events.put(StatusEvent(item.id, "Blocked by Deepbrid"))
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
                            status="retrying",
                            error=self._stored_error(error),
                        )
                        self.events.put(StatusEvent(item.id, status))
                        if item_cancel_event.wait(delay) or self.stop_event.is_set():
                            break

            if self.stop_event.is_set():
                self.store.update(item.id, status="queued")
                self.active_cancel_events.pop(item.id, None)
                self.log(f"Queue item {item.id} paused.")
                break
            if item_cancel_event.is_set():
                self.store.update(item.id, status="queued")
                self.active_cancel_events.pop(item.id, None)
                self.log(
                    f"Queue item {item.id} was disabled before transfer; "
                    "moving to the next priority."
                )
                continue
            if blocked:
                self.active_cancel_events.pop(item.id, None)
                self.events.put(RefreshEvent())
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
                status="downloading",
                error=None,
            )
            if destination.is_file() and not item.force:
                existing_size = destination.stat().st_size
                self.store.update(
                    item.id,
                    status="skipped",
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
                    self.store.update(item.id, downloaded=downloaded, total=total)
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

            try:
                completed = client.download(
                    generated_url,
                    filename,
                    self.output_dir,
                    lambda: self.stop_event.is_set() or item_cancel_event.is_set(),
                    progress,
                    overwrite_existing=item.force,
                    on_size_verified=record_size_verification,
                )
                if completed:
                    final_size = (self.output_dir / filename).stat().st_size
                    self.store.update(
                        item.id,
                        status="completed",
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
                    self.store.update(item.id, status="queued", downloaded=partial_size)
                    self.log(f"Paused {filename} at {partial_size} bytes.")
            except (DeepbridError, OSError) as error:
                self.store.update(
                    item.id,
                    status="failed",
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
