from __future__ import annotations

import queue
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.app import DownloaderApp
from src.app_events import ApiKeyErrorEvent, WorkerDoneEvent
from src.deepbrid_client import DeepbridError
from src.queue_runner import QueueRunner
from src.queue_store import QueueStore
from src.queue_types import QueueSource, QueueStatus
from src.remote_jobs import RemoteJob, RemoteJobFile
from src.usenet_finder import file_size_bytes


class QueueStoreTests(unittest.TestCase):
    def test_torrent_upload_bytes_are_encrypted_and_duplicate_uploads_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            torrent_bytes = b"d4:infod4:name4:testee"

            self.assertTrue(store.add_torrent_file("test.torrent", torrent_bytes))
            self.assertFalse(store.add_torrent_file("copy.torrent", torrent_bytes))

            item = store.list_items()[0]
            self.assertEqual(item.source, QueueSource.TORRENT_CLOUD)
            self.assertEqual(item.filename, "test.torrent")
            self.assertEqual(store.torrent_file_data(item.id), torrent_bytes)
            with closing(sqlite3.connect(store.database_path)) as connection:
                stored_bytes = connection.execute(
                    "SELECT torrent_data FROM downloads"
                ).fetchone()[0]
            self.assertNotIn(torrent_bytes, bytes(stored_bytes))

    def test_completed_torrent_job_expands_to_file_queue_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            store.add_torrent_file("collection.torrent", b"torrent-data")
            item = store.list_items()[0]

            store.expand_torrent_job(
                item.id,
                "remote-42",
                [
                    ("https://deepbrid.example/file/1", "collection-file-1", 12),
                    ("https://deepbrid.example/file/2", "collection-file-2", 34),
                ],
            )

            files = store.list_items()
            self.assertEqual(len(files), 2)
            self.assertEqual([file.source for file in files], [QueueSource.TORRENT_CLOUD] * 2)
            self.assertEqual([file.remote_job_id for file in files], ["remote-42"] * 2)
            self.assertEqual([file.remote_file_index for file in files], [0, 1])
            self.assertEqual([file.deepbrid_link for file in files], [
                "https://deepbrid.example/file/1",
                "https://deepbrid.example/file/2",
            ])
            self.assertEqual([file.total for file in files], [12, 34])

    def test_existing_queue_database_is_migrated_without_losing_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "queue.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """CREATE TABLE downloads (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'queued',
                        downloaded INTEGER NOT NULL DEFAULT 0,
                        total INTEGER,
                        filename TEXT,
                        error TEXT,
                        deepbrid_link TEXT,
                        host_message TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                connection.execute(
                    "INSERT INTO downloads (url, error, deepbrid_link, host_message) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        "https://supported.example/file.zip",
                        "HTTP 403\nRequest: curl --data link=https%3A%2F%2Fsupported.example%2Ffile.zip",
                        "https://premium.example/d/legacy-token",
                        "Usenet Finder",
                    ),
                )
                connection.commit()

            store = QueueStore(database_path)
            item = store.list_items()[0]

            self.assertEqual(item.url, "https://supported.example/file.zip")
            self.assertEqual(item.source, QueueSource.USENET)
            self.assertTrue(item.enabled)
            self.assertEqual(item.host_status, "unknown")
            self.assertFalse(item.size_verified)
            with closing(sqlite3.connect(database_path)) as connection:
                stored_url = connection.execute(
                    "SELECT url, url_ciphertext FROM downloads"
                ).fetchone()
            self.assertNotIn(b"https://supported.example/file.zip", bytes(stored_url[1]))
            self.assertNotEqual(stored_url[0], "https://supported.example/file.zip")
            stored_error = store.list_items()[0].error
            self.assertEqual(stored_error, "HTTP 403")
            self.assertEqual(store.list_items()[0].deepbrid_link, "https://premium.example/d/legacy-token")

    def test_new_queue_items_default_to_premium_link_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")

            self.assertTrue(store.add("https://supported.example/file.zip"))

            item = store.next_item()
            assert item is not None
            self.assertEqual(item.source, QueueSource.PREMIUM_LINK)

    def test_queue_store_rejects_unknown_sources_and_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            with self.assertRaises(ValueError):
                store.add("https://example.com/file.zip", source="usenet")  # type: ignore[arg-type]
            store.add("https://example.com/file.zip")
            item = store.next_item()
            assert item is not None

            with self.assertRaises(ValueError):
                store.update(item.id, status="remote_job_processing")

            store.update(item.id, status=QueueStatus.COMPLETED)
            self.assertEqual(store.list_items()[0].status, QueueStatus.COMPLETED)


    def test_interrupted_item_is_requeued_and_remembers_partial_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "queue.sqlite3"
            store = QueueStore(database_path)
            self.assertTrue(store.add("https://example.com/file.zip"))
            item = store.next_item()
            assert item is not None
            store.update(
                item.id,
                status="downloading",
                downloaded=12,
                filename="file.zip",
            )

            recovered_store = QueueStore(database_path)
            recovered_item = recovered_store.next_item()

            self.assertTrue(recovered_store.recovered_work)
            self.assertIsNotNone(recovered_item)
            assert recovered_item is not None
            self.assertEqual(recovered_item.status, "queued")
            self.assertEqual(recovered_item.downloaded, 12)
            self.assertEqual(recovered_item.filename, "file.zip")

    def test_original_and_generated_urls_are_encrypted_in_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "queue.sqlite3"
            store = QueueStore(database_path)
            original_url = "https://supported.example/file?private=value"
            generated_url = "https://premium.example/d/secret-token"
            self.assertTrue(store.add(original_url, host_status="up"))
            item = store.next_item()
            assert item is not None
            store.update(item.id, deepbrid_link=generated_url, size_verified=True)

            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    "SELECT url, url_ciphertext, deepbrid_link FROM downloads"
                ).fetchone()
            raw_values = b" ".join(bytes(value) if isinstance(value, bytes) else value.encode() for value in row)
            self.assertNotIn(original_url.encode(), raw_values)
            self.assertNotIn(generated_url.encode(), raw_values)
            loaded = store.list_items()[0]
            self.assertEqual(loaded.url, original_url)
            self.assertEqual(loaded.deepbrid_link, generated_url)
            self.assertTrue(loaded.size_verified)

    def test_queue_item_keeps_optional_finder_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            self.assertTrue(
                store.add(
                    "https://usenet.example/download/123",
                    host_status="up",
                    host_message="Usenet Finder",
                    filename="episode.mkv",
                    source=QueueSource.USENET,
                )
            )
            item = store.next_item()
            assert item is not None
            self.assertEqual(item.filename, "episode.mkv")
            self.assertEqual(item.host_message, "Usenet Finder")

    def test_queue_item_keeps_resolved_finder_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            self.assertTrue(
                store.add(
                    "https://usenet.example/download/123",
                    host_status="up",
                    host_message="Usenet Finder",
                    filename="episode.mkv",
                    total=1_100_000_000,
                    source=QueueSource.USENET,
                )
            )
            item = store.next_item()
            assert item is not None
            self.assertEqual(item.total, 1_100_000_000)
            self.assertEqual(item.downloaded, 0)


class QueueRunnerTests(unittest.TestCase):
    def test_runner_does_not_retry_unsupported_filehost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            store.add("https://unsupported.example/file.zip")
            events: queue.Queue = queue.Queue()
            generate_link_calls: list[str] = []

            class FakeClient:
                def __init__(self, *_args, **_kwargs):
                    pass

                def validate_api_key(self) -> None:
                    pass

                def generate_link(self, url: str) -> tuple[str, str]:
                    generate_link_calls.append(url)
                    raise DeepbridError("Filehoster not supported", retryable=False)

            with patch("src.queue_runner.DeepbridClient", FakeClient):
                QueueRunner(
                    store,
                    "test-key",
                    Path(temporary_directory),
                    events,
                    threading.Event(),
                    {},
                    lambda _message: None,
                ).run()

            item = store.list_items()[0]
            self.assertEqual(generate_link_calls, ["https://unsupported.example/file.zip"])
            self.assertEqual(item.status, "blocked")
            self.assertEqual(item.error, "DeepbridError")

    def test_runner_continues_after_unsupported_filehost_and_downloads_usenet_items(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            store = QueueStore(output_dir / "queue.sqlite3")
            store.add("https://unsupported.example/file.zip")
            usenet_urls = [
                "https://usenet.example/download/first",
                "https://usenet.example/download/second",
            ]
            for index, url in enumerate(usenet_urls, start=1):
                store.add(
                    url,
                    host_status="up",
                    host_message="Usenet Finder",
                    filename=f"episode-{index}.mkv",
                    source=QueueSource.USENET,
                )
            events: queue.Queue = queue.Queue()
            generated_urls: list[str] = []
            downloaded_urls: list[str] = []

            class FakeClient:
                def __init__(self, *_args, **_kwargs):
                    pass

                def validate_api_key(self) -> None:
                    pass

                def generate_link(self, url: str) -> tuple[str, str]:
                    generated_urls.append(url)
                    raise DeepbridError(
                        "Filehoster not supported",
                        retryable=False,
                        skip_queue_item=True,
                    )

                def download(
                    self,
                    url,
                    filename,
                    directory,
                    _should_stop,
                    _on_progress,
                    **kwargs,
                ) -> bool:
                    downloaded_urls.append(url)
                    (directory / filename).write_bytes(b"file")
                    kwargs["on_size_verified"](True)
                    return True

            with patch("src.queue_runner.DeepbridClient", FakeClient):
                QueueRunner(
                    store,
                    "test-key",
                    output_dir,
                    events,
                    threading.Event(),
                    {},
                    lambda _message: None,
                ).run()

            items = store.list_items()
            self.assertEqual(generated_urls, ["https://unsupported.example/file.zip"])
            self.assertEqual(downloaded_urls, usenet_urls)
            self.assertEqual([item.status for item in items], ["blocked", "completed", "completed"])

    def test_runner_stops_when_api_key_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            store.add("https://supported.example/file.zip")
            events: queue.Queue = queue.Queue()
            stop_event = threading.Event()
            active_cancel_events: dict[int, threading.Event] = {}

            def reject_key() -> None:
                raise DeepbridError("rejected", status_code=401)

            client = SimpleNamespace(validate_api_key=reject_key)

            with patch("src.queue_runner.DeepbridClient", return_value=client):
                QueueRunner(
                    store,
                    "test-key",
                    Path(temporary_directory),
                    events,
                    stop_event,
                    active_cancel_events,
                    lambda _message: None,
                ).run()

            self.assertIsInstance(events.get_nowait(), ApiKeyErrorEvent)
            self.assertIsInstance(events.get_nowait(), WorkerDoneEvent)

    def test_runner_uploads_torrent_once_then_downloads_every_returned_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            store = QueueStore(output_dir / "queue.sqlite3")
            store.add_torrent_file("bundle.torrent", b"torrent-data")
            events: queue.Queue = queue.Queue()
            submitted: list[bytes] = []
            requested_jobs: list[str] = []
            downloaded: list[str] = []

            class FakeClient:
                def __init__(self, *_args, **_kwargs):
                    pass

                def validate_api_key(self) -> None:
                    pass

                def submit_torrent_file(self, torrent_data, _filename):
                    submitted.append(torrent_data)
                    return RemoteJob("job-17", "bundle", "queued")

                def get_job(self, job_id):
                    requested_jobs.append(job_id)
                    return RemoteJob(
                        job_id,
                        "bundle",
                        "downloaded",
                        files=(
                            RemoteJobFile(
                                "file-1",
                                "https://deepbrid.example/file/1",
                                3,
                            ),
                            RemoteJobFile(
                                "file-2",
                                "https://deepbrid.example/file/2",
                                4,
                            ),
                        ),
                    )

                def download(
                    self,
                    _url,
                    filename,
                    directory,
                    _should_stop,
                    _on_progress,
                    **kwargs,
                ):
                    downloaded.append(filename)
                    (directory / filename).write_bytes(b"data")
                    kwargs["on_size_verified"](True)
                    return True

            with patch("src.queue_runner.DeepbridClient", FakeClient):
                QueueRunner(
                    store,
                    "test-key",
                    output_dir,
                    events,
                    threading.Event(),
                    {},
                    lambda _message: None,
                ).run()

            items = store.list_items()
            self.assertEqual(submitted, [b"torrent-data"])
            self.assertEqual(requested_jobs, ["job-17", "job-17", "job-17"])
            self.assertEqual(len(downloaded), 2)
            self.assertTrue(all(item.status == QueueStatus.COMPLETED for item in items))
            self.assertTrue(all(item.remote_job_id == "job-17" for item in items))
            self.assertTrue(all(item.downloaded == 4 for item in items))
            self.assertFalse(any(store.torrent_file_data(item.id) for item in items))

    def test_runner_completes_queue_item_with_preserved_finder_filename(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            store = QueueStore(output_dir / "queue.sqlite3")
            source_url = "https://usenet.example/download/123"
            store.add(
                source_url,
                host_status="up",
                host_message="Usenet Finder",
                filename="finder-file.mkv",
                source=QueueSource.USENET,
            )
            events: queue.Queue = queue.Queue()
            stop_event = threading.Event()
            active_cancel_events: dict[int, threading.Event] = {}
            requested_urls: list[str] = []
            downloaded_names: list[str] = []

            class FakeClient:
                def __init__(self, *_args, **_kwargs):
                    pass

                def validate_api_key(self) -> None:
                    pass

                def generate_link(self, _url: str) -> tuple[str, str]:
                    raise AssertionError("Usenet Finder URLs must not go through link generation.")

                def download(
                    self,
                    url,
                    filename,
                    directory,
                    _should_stop,
                    _on_progress,
                    **kwargs,
                ) -> bool:
                    requested_urls.append(url)
                    downloaded_names.append(filename)
                    (directory / filename).write_bytes(b"file")
                    kwargs["on_size_verified"](True)
                    return True

            with patch("src.queue_runner.DeepbridClient", FakeClient):
                QueueRunner(
                    store,
                    "test-key",
                    output_dir,
                    events,
                    stop_event,
                    active_cancel_events,
                    lambda _message: None,
                ).run()

            item = store.list_items()[0]
            self.assertEqual(item.status, "completed")
            self.assertEqual(requested_urls, [source_url])
            self.assertEqual(downloaded_names, ["finder-file.mkv"])
            self.assertEqual(item.deepbrid_link, source_url)
            self.assertEqual(item.downloaded, 4)
            self.assertTrue(item.size_verified)
            self.assertIsInstance(events.queue[-1], WorkerDoneEvent)
            self.assertFalse(active_cancel_events)

    def test_runner_preserves_resolved_size_when_download_has_no_content_length(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            store = QueueStore(output_dir / "queue.sqlite3")
            store.add(
                "https://usenet.example/download/123",
                host_status="up",
                host_message="Usenet Finder",
                filename="finder-file.mkv",
                total=1000,
                source=QueueSource.USENET,
            )
            stop_event = threading.Event()

            class FakeClient:
                def __init__(self, *_args, **_kwargs):
                    pass

                def validate_api_key(self) -> None:
                    pass

                def download(
                    self,
                    _url,
                    filename,
                    directory,
                    _should_stop,
                    on_progress,
                    **_kwargs,
                ) -> bool:
                    (directory / f".{filename}.part").write_bytes(b"x" * 100)
                    on_progress(100, None)
                    stop_event.set()
                    return False

            with patch("src.queue_runner.DeepbridClient", FakeClient):
                QueueRunner(
                    store,
                    "test-key",
                    output_dir,
                    queue.Queue(),
                    stop_event,
                    {},
                    lambda _message: None,
                ).run()

            item = store.list_items()[0]
            self.assertEqual(item.status, "queued")
            self.assertEqual(item.downloaded, 100)
            self.assertEqual(item.total, 1000)


class UsenetFinderQueueIntegrationTests(unittest.TestCase):
    def test_accessible_finder_links_enter_the_normal_queue(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            app = DownloaderApp.__new__(DownloaderApp)
            app.store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            app._refresh_rows = Mock()
            app._save_visible_queue_order = Mock()
            app._log = Mock()
            link = "https://usenet.example/download/123?ticket=private"

            counts = app.add_usenet_links(
                [
                    (link, "..\\..\\episode.mkv", file_size_bytes("1.1 GB")),
                    (link, "episode.mkv"),
                    ("file:///private/file", "private-file"),
                ]
            )

            self.assertEqual(counts, (1, 1, 1))
            item = app.store.next_item()
            assert item is not None
            self.assertEqual(item.url, link)
            self.assertEqual(item.filename, "_.._episode.mkv")
            self.assertEqual(item.host_message, "Usenet Finder")
            self.assertEqual(item.total, 1_100_000_000)
            app._apply_host_statuses({})
            item = app.store.next_item()
            assert item is not None
            self.assertEqual(item.host_message, "Usenet Finder")
            self.assertEqual(item.host_status, "up")
            app._refresh_rows.assert_called_once()
            app._save_visible_queue_order.assert_called_once()


class TorrentQueueUiTests(unittest.TestCase):
    def test_torrent_drop_collects_multiple_files_and_folder_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            folder = root / "folder with spaces"
            nested = folder / "nested"
            nested.mkdir(parents=True)
            first = root / "first.torrent"
            second = nested / "second.TORRENT"
            ignored = nested / "notes.txt"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            ignored.write_text("ignore", encoding="utf-8")

            paths = DownloaderApp._torrent_paths_from_drop(
                (str(folder), str(first), str(ignored))
            )

            self.assertEqual(paths, [first, second])

    def test_torrent_drop_queues_parsed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            first = Path(temporary_directory) / "first file.torrent"
            second = Path(temporary_directory) / "second.torrent"
            first.touch()
            second.touch()
            dropped_paths = (str(first), str(second))
            app = DownloaderApp.__new__(DownloaderApp)
            app.root = SimpleNamespace(
                tk=SimpleNamespace(splitlist=lambda _data: dropped_paths)
            )
            app.theme_colors = {"surface": "#ffffff"}
            app._add_torrent_paths = Mock()
            dialog = Mock()
            drop_zone = Mock()
            event = SimpleNamespace(data="tcl-list-data")

            result = app._handle_torrent_drop(event, dialog, drop_zone)

            self.assertEqual(result, "copy")
            drop_zone.configure.assert_called_once_with(background="#ffffff")
            dialog.destroy.assert_called_once_with()
            app._add_torrent_paths.assert_called_once_with([first, second])

    def test_canceling_torrent_file_picker_does_not_show_no_files_popup(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        parent = Mock()

        with (
            patch("src.app.filedialog.askopenfilenames", return_value=()),
            patch("src.app.messagebox.showinfo") as showinfo,
            patch.object(app, "_add_torrent_paths") as add_paths,
        ):
            app._choose_torrent_files(parent)

        parent.destroy.assert_not_called()
        add_paths.assert_not_called()
        showinfo.assert_not_called()

    def test_empty_selected_torrent_folder_reports_no_matching_files(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app.root = object()

        with patch("src.app.messagebox.showinfo") as showinfo:
            app._add_torrent_paths([])

        showinfo.assert_called_once_with(
            "No torrent files found",
            "The selected folder does not contain any .torrent files.",
            parent=app.root,
        )

    def test_canceling_torrent_folder_picker_does_not_show_popup(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        parent = Mock()

        with (
            patch("src.app.filedialog.askdirectory", return_value=""),
            patch("src.app.messagebox.showinfo") as showinfo,
            patch.object(app, "_add_torrent_folder") as add_folder,
        ):
            app._choose_torrent_folder(parent)

        parent.destroy.assert_not_called()
        add_folder.assert_not_called()
        showinfo.assert_not_called()

    def test_delete_shortcut_uses_existing_remove_confirmation(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app._remove_link = Mock()

        result = app._handle_remove_shortcut(object())

        self.assertEqual(result, "break")
        app._remove_link.assert_called_once_with()

    def test_add_torrent_paths_counts_duplicates_and_rejects_oversized_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = root / "first.torrent"
            duplicate = root / "duplicate.torrent"
            oversized = root / "oversized.torrent"
            first.write_bytes(b"metainfo")
            duplicate.write_bytes(b"metainfo")
            oversized.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
            app = DownloaderApp.__new__(DownloaderApp)
            app.root = object()
            app.store = QueueStore(root / "queue.sqlite3")
            app.status_text = SimpleNamespace(set=Mock())
            app._refresh_rows = Mock()
            app._save_visible_queue_order = Mock()
            app._log = Mock()

            with patch("src.app.messagebox.showwarning") as showwarning:
                app._add_torrent_paths([first, duplicate, oversized])

            self.assertEqual(len(app.store.list_items()), 1)
            self.assertEqual(
                app.status_text.set.call_args.args[0],
                "Added 1; skipped 1 duplicate(s) and 1 invalid file(s)",
            )
            showwarning.assert_called_once()
            app._refresh_rows.assert_called_once()
            app._save_visible_queue_order.assert_called_once()

    def test_add_torrent_folder_selects_nested_torrent_files_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            folder = root / "torrents"
            nested = folder / "nested"
            nested.mkdir(parents=True)
            (folder / "one.torrent").write_bytes(b"one")
            (nested / "two.TORRENT").write_bytes(b"two")
            (nested / "ignore.txt").write_text("not a torrent", encoding="utf-8")
            app = DownloaderApp.__new__(DownloaderApp)
            app.root = object()
            app.store = QueueStore(root / "queue.sqlite3")
            app.status_text = SimpleNamespace(set=Mock())
            app._refresh_rows = Mock()
            app._save_visible_queue_order = Mock()
            app._log = Mock()

            with patch("src.app.filedialog.askdirectory", return_value=str(folder)):
                app._add_torrent_folder()

            self.assertEqual(
                [item.filename for item in app.store.list_items()],
                ["one.torrent", "two.TORRENT"],
            )
            self.assertEqual(
                [item.source for item in app.store.list_items()],
                [QueueSource.TORRENT_CLOUD, QueueSource.TORRENT_CLOUD],
            )
