from __future__ import annotations

import hashlib
import json
import queue
import threading
import tempfile
import sqlite3
import shutil
import struct
import unittest
import uuid
import urllib.error
import urllib.parse
from datetime import datetime
from contextlib import closing
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from src.deepbrid_client import APP_USER_AGENT, DeepbridClient, DeepbridError
from src.app_events import (
    ApiKeyCheckEvent,
    ApiKeyErrorEvent,
    HostsErrorEvent,
    HostsLoadedEvent,
    StartupApiKeyProblemEvent,
    StartupUpdateEvent,
    WorkerDoneEvent,
)
from src.app_config import AppConfiguration
from src.app_info import (
    GITHUB_LATEST_RELEASE_URL,
    LatestRelease,
    ReleaseAsset,
    UpdateCheckError,
    fetch_latest_release,
    fetch_latest_release_details,
    is_newer_version,
    platform_release_asset,
)
from src.app import (
    API_KEY_DASHBOARD_URL,
    APP_DATA_DIR,
    DATABASE_PATH,
    COLUMN_ORDER,
    DEFAULT_COLUMNS,
    DownloaderApp,
    LEGACY_DATABASE_PATH,
    LEGACY_QUEUE_DATABASE_PATH,
    OUTPUT_DIR,
    _ConsoleStream,
    append_output_log_line,
    default_download_directory,
    _filter_and_sort_host_rows,
    _parse_linked_image_badge,
    format_bytes,
    format_duration,
    format_item_eta,
    progress_indicator_values,
    size_verification_label,
    _setting_is_enabled,
    migrate_legacy_database,
    migrate_legacy_databases,
    redact_log_urls,
    remove_legacy_storage,
    smooth_rate,
    validate_api_key,
    validate_output_folder,
)
from src.link_utils import extract_supported_links, supported_link_status
from src.queue_store import QueueStore
from src.queue_runner import QueueRunner
from src.secure_store import SecureStore
from src.single_instance import acquire_single_instance
from src.startup_manager import (
    MACOS_LOGIN_ITEMS_URL,
    StartupConfigurationError,
    StartupManager,
)
from src.update_manager import download_update_asset
from src.usenet_finder import (
    FinderFile,
    FinderPackage,
    FinderResult,
    FinderSearchPage,
    UsenetFinderClient,
    UsenetFinderError,
)
from src.usenet_browser import UsenetBrowserSession
from src.usenet_finder_dialog import UsenetFinderDialog
from src.usenet_finder_state import (
    CACHE_DURATION_SETTING,
    CACHE_TTL_SECONDS,
    DEFAULT_CACHE_DURATION_HOURS,
    UsenetFinderState,
    valid_cache_duration_hours,
)


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        body, self.body = self.body, b""
        return body

    def getcode(self) -> int:
        return self.status


class FakeSelectionTable:
    def __init__(self, row_ids: list[str]):
        self.row_ids = row_ids
        self.selected: list[str] = []
        self.has_widget_focus = False

    def get_children(self, _parent: str = "") -> tuple[str, ...]:
        return tuple(self.row_ids)

    def identify_region(self, _x: int, _y: int) -> str:
        return "cell" if 0 <= _y < len(self.row_ids) else "nothing"

    def identify_row(self, y: int) -> str:
        return self.row_ids[y] if 0 <= y < len(self.row_ids) else ""

    def selection(self) -> tuple[str, ...]:
        return tuple(self.selected)

    def selection_add(self, *row_ids: str) -> None:
        for row_id in row_ids:
            if row_id not in self.selected:
                self.selected.append(row_id)

    def selection_remove(self, *row_ids: str) -> None:
        self.selected = [row_id for row_id in self.selected if row_id not in row_ids]

    def selection_set(self, *row_ids: str) -> None:
        self.selected = list(row_ids)

    def focus(self, _row_id: str) -> None:
        pass

    def focus_set(self) -> None:
        self.has_widget_focus = True


class QueueStoreTests(unittest.TestCase):
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
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )"""
                )
                connection.execute(
                    "INSERT INTO downloads (url, error, deepbrid_link) VALUES (?, ?, ?)",
                    (
                        "https://supported.example/file.zip",
                        "HTTP 403\nRequest: curl --data link=https%3A%2F%2Fsupported.example%2Ffile.zip",
                        "https://premium.example/d/legacy-token",
                    ),
                )
                connection.commit()

            store = QueueStore(database_path)
            item = store.list_items()[0]

            self.assertEqual(item.url, "https://supported.example/file.zip")
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
                )
            )
            item = store.next_item()
            assert item is not None
            self.assertEqual(item.filename, "episode.mkv")
            self.assertEqual(item.host_message, "Usenet Finder")


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
                    (link, "..\\..\\episode.mkv"),
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
            app._apply_host_statuses({})
            item = app.store.next_item()
            assert item is not None
            self.assertEqual(item.host_message, "Usenet Finder")
            self.assertEqual(item.host_status, "up")
            app._refresh_rows.assert_called_once()
            app._save_visible_queue_order.assert_called_once()


class AppConfigurationTests(unittest.TestCase):
    def test_configuration_loads_defaults_and_persists_column_migrations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database = Path(temporary_directory) / "settings.sqlite3"
            store = SecureStore(database)
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "INSERT INTO app_settings (name, value) VALUES (?, ?)",
                    ("usenet_password", b"obsolete-encrypted-password"),
                )
                connection.execute(
                    "INSERT INTO app_settings (name, value) VALUES (?, ?)",
                    ("usenet_username", b"obsolete-encrypted-username"),
                )
            downloads = Path(temporary_directory) / "downloads"
            default_log_path = Path(temporary_directory) / "deepbrid-output.log"
            config = AppConfiguration.load(
                store,
                COLUMN_ORDER,
                DEFAULT_COLUMNS,
                lambda: downloads,
                lambda: None,
                lambda: None,
                lambda: default_log_path,
            )

            self.assertTrue(config.auto_check_updates)
            self.assertTrue(config.auto_start_downloads)
            self.assertEqual(
                config.usenet_finder_cache_duration_hours,
                DEFAULT_CACHE_DURATION_HOURS,
            )
            self.assertFalse(config.append_output_log_enabled)
            self.assertEqual(config.append_output_log_path, default_log_path)
            with closing(sqlite3.connect(database)) as connection:
                remaining_credentials = connection.execute(
                    "SELECT name FROM app_settings "
                    "WHERE name IN ('usenet_username', 'usenet_password')"
                ).fetchall()
            self.assertEqual(remaining_credentials, [])
            self.assertEqual(config.output_dir, downloads)
            self.assertIn("progress", config.visible_columns)
            self.assertNotIn("progress_percentage", config.visible_columns)
            self.assertEqual(
                config.column_order[config.column_order.index("progress") + 1],
                "progress_percentage",
            )

            store.set_setting("auto_start_downloads_on_startup", "false")
            store.set_setting("dark_theme", "true")
            store.set_setting("hosts", json.dumps({"example.com": "up"}))
            store.set_setting("output_directory", str(downloads / "custom"))
            store.set_setting("visible_columns", json.dumps(["filename", "progress"]))
            store.set_setting(CACHE_DURATION_SETTING, "48")
            log_path = downloads / "diagnostics.log"
            store.set_setting("append_output_log_enabled", "true")
            store.set_setting("append_output_log_path", str(log_path))
            config = AppConfiguration.load(
                store,
                COLUMN_ORDER,
                DEFAULT_COLUMNS,
                lambda: downloads,
                lambda: None,
                lambda: None,
                lambda: default_log_path,
            )
            self.assertFalse(config.auto_start_downloads)
            self.assertTrue(config.dark_theme)
            self.assertEqual(config.hosts, {"example.com": "up"})
            self.assertEqual(config.output_dir, downloads / "custom")
            self.assertEqual(config.visible_columns, ["filename", "progress"])
            self.assertEqual(config.usenet_finder_cache_duration_hours, 48)
            self.assertTrue(config.append_output_log_enabled)
            self.assertEqual(config.append_output_log_path, log_path)


class ApplicationShutdownTests(unittest.TestCase):
    def test_close_signals_download_cancellation_and_waits_for_worker(self) -> None:
        scheduled: list[tuple[int, object]] = []
        destroyed: list[bool] = []
        worker_states = iter((True, False))
        stop_event = threading.Event()
        cancel_event = threading.Event()
        app = object.__new__(DownloaderApp)
        app.closing = False
        app.worker = SimpleNamespace(is_alive=lambda: next(worker_states))
        app.stop_event = stop_event
        app.active_cancel_events = {1: cancel_event}
        app.root = SimpleNamespace(
            after=lambda delay, callback: scheduled.append((delay, callback)),
            destroy=lambda: destroyed.append(True),
        )
        app.status_text = SimpleNamespace(set=lambda _message: None)
        app._persist_theme = lambda: None
        app._log = lambda _message: None
        app._restore_console_capture = lambda: None

        app._close()

        self.assertTrue(stop_event.is_set())
        self.assertTrue(cancel_event.is_set())
        self.assertTrue(app.closing)
        self.assertEqual(destroyed, [])
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0], 100)

        scheduled.pop()[1]()

        self.assertEqual(destroyed, [True])

    def test_close_destroys_immediately_when_worker_is_not_running(self) -> None:
        destroyed: list[bool] = []
        stop_event = threading.Event()
        app = object.__new__(DownloaderApp)
        app.closing = False
        app.worker = None
        app.stop_event = stop_event
        app.active_cancel_events = {}
        app.root = SimpleNamespace(destroy=lambda: destroyed.append(True))
        app._persist_theme = lambda: None
        app._restore_console_capture = lambda: None

        app._close()

        self.assertTrue(stop_event.is_set())
        self.assertTrue(app.closing)
        self.assertEqual(destroyed, [True])

    def test_unknown_and_unsupported_items_are_selected_but_down_and_disabled_are_not(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            store.add("https://supported.example/down", host_status="down")
            store.add("https://supported.example/unsupported", host_status="unsupported")
            store.add("https://supported.example/unknown", host_status="unknown")
            disabled_id_added = store.add("https://supported.example/disabled", host_status="up")
            self.assertTrue(disabled_id_added)
            unsupported_item, unknown_item, disabled_item = store.list_items()[1:]
            store.update(disabled_item.id, enabled=False)

            self.assertEqual(store.next_item().id, unsupported_item.id)
            store.update(unsupported_item.id, status="failed")
            self.assertEqual(store.next_item().id, unknown_item.id)
            store.update(unknown_item.id, status="failed")
            self.assertIsNone(store.next_item())

    def test_next_item_follows_persisted_visible_order_and_skips_disabled_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            store.add("https://supported.example/a.zip", host_status="supported")
            store.add("https://supported.example/b.zip", host_status="supported")
            first, second = store.list_items()

            store.set_priority_order([second.id, first.id])
            self.assertEqual(store.next_item().id, second.id)
            store.update(second.id, enabled=False)
            self.assertEqual(store.next_item().id, first.id)

    def test_force_redownload_resets_progress_and_generated_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            store.add("https://supported.example/file.zip", host_status="up")
            item = store.next_item()
            assert item is not None
            store.update(
                item.id,
                status="completed",
                downloaded=1024,
                total=1024,
                deepbrid_link="https://premium.example/token",
            )

            store.set_force_redownload(item.id)
            redownload = store.list_items()[0]

            self.assertEqual(redownload.status, "queued")
            self.assertEqual(redownload.downloaded, 0)
            self.assertIsNone(redownload.total)
            self.assertIsNone(redownload.deepbrid_link)
            self.assertTrue(redownload.force)

    def test_retry_item_resets_failed_or_blocked_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            store.add("https://supported.example/file.zip", host_status="supported")
            item = store.list_items()[0]
            store.update(item.id, status="blocked", error="HTTP 403")

            store.retry_item(item.id)
            retried = store.list_items()[0]

            self.assertEqual(retried.status, "queued")
            self.assertIsNone(retried.error)
            self.assertEqual(store.next_item().id, item.id)

    def test_remove_item_deletes_idle_rows_but_preserves_active_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = QueueStore(Path(temporary_directory) / "queue.sqlite3")
            store.add("https://supported.example/idle", host_status="supported")
            store.add("https://supported.example/active", host_status="supported")
            idle_item, active_item = store.list_items()
            store.update(active_item.id, status="downloading")

            self.assertTrue(store.remove_item(idle_item.id))
            self.assertFalse(store.remove_item(active_item.id))
            remaining = store.list_items()
            self.assertEqual([item.id for item in remaining], [active_item.id])


class ValidationAndPresentationTests(unittest.TestCase):
    def test_linux_startup_configuration_creates_and_removes_autostart_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "user home"
            project_root = Path(temporary_directory) / "project folder"
            project_root.mkdir()
            manager = StartupManager(
                platform_name="linux",
                home=home,
                executable=Path(temporary_directory) / "python with spaces",
                project_root=project_root,
                frozen=False,
            )

            enabled_message = manager.configure()

            entry = manager.startup_file.read_text(encoding="utf-8")
            self.assertIn(
                "Exec="
                + " ".join(
                    manager._desktop_exec_argument(argument)
                    for argument in (
                        str((Path(temporary_directory) / "python with spaces").resolve()),
                        "-m",
                        "src.app",
                    )
                ),
                entry,
            )
            self.assertIn(f"Path={project_root}", entry)
            self.assertTrue(manager.is_enabled())
            self.assertIn("will start after", enabled_message)

            disabled_message = manager.configure()

            self.assertFalse(manager.is_enabled())
            self.assertIn("disabled", disabled_message)

    def test_linux_startup_entry_uses_packaged_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = StartupManager(
                platform_name="linux",
                home=Path(temporary_directory),
                executable=Path(temporary_directory) / "Deepbrid Downloader",
                frozen=True,
            )

            manager.configure()

            entry = manager.startup_file.read_text(encoding="utf-8")
            self.assertIn(
                "Exec="
                + manager._desktop_exec_argument(
                    str((Path(temporary_directory) / "Deepbrid Downloader").resolve())
                ),
                entry,
            )
            self.assertNotIn("-m", entry)

    def test_windows_startup_command_uses_launcher_without_console_when_available(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            executable = Path(temporary_directory) / "python.exe"
            pythonw = executable.with_name("pythonw.exe")
            pythonw.touch()
            project_root = Path(temporary_directory) / "project"
            manager = StartupManager(
                platform_name="win32",
                home=Path(temporary_directory),
                executable=executable,
                project_root=project_root,
                frozen=False,
            )

            command = manager._windows_command()

            self.assertIn(str(pythonw), command)
            self.assertIn(str(project_root / "launcher.py"), command)

    def test_windows_startup_configuration_adds_and_removes_run_value(self) -> None:
        manager = StartupManager(platform_name="win32")
        registry_key = MagicMock()
        winreg = SimpleNamespace(
            HKEY_CURRENT_USER=object(),
            REG_SZ=1,
            CreateKey=Mock(return_value=registry_key),
            SetValueEx=Mock(),
            DeleteValue=Mock(),
        )
        with (
            patch.dict("sys.modules", {"winreg": winreg}),
            patch.object(manager, "is_enabled", return_value=False),
            patch.object(manager, "_windows_command", return_value='"app.exe"'),
        ):
            enabled_message = manager.configure()
        winreg.SetValueEx.assert_called_once_with(
            registry_key.__enter__.return_value,
            "DeepbridDownloader",
            0,
            winreg.REG_SZ,
            '"app.exe"',
        )
        self.assertIn("will start after", enabled_message)

        with (
            patch.dict("sys.modules", {"winreg": winreg}),
            patch.object(manager, "is_enabled", return_value=True),
        ):
            disabled_message = manager.configure()
        winreg.DeleteValue.assert_called_once_with(
            registry_key.__enter__.return_value,
            "DeepbridDownloader",
        )
        self.assertIn("disabled", disabled_message)

    def test_macos_startup_action_opens_login_items_settings(self) -> None:
        manager = StartupManager(platform_name="darwin")
        with patch("src.startup_manager.subprocess.Popen") as popen:
            message = manager.configure()

        popen.assert_called_once_with(["open", MACOS_LOGIN_ITEMS_URL])
        self.assertIn("Login Items", message)

    def test_unknown_platform_startup_action_reports_unsupported(self) -> None:
        manager = StartupManager(platform_name="freebsd")

        with self.assertRaisesRegex(StartupConfigurationError, "not supported"):
            manager.configure()

    def test_usenet_finder_cache_duration_setting_is_persisted(self) -> None:
        saved_settings: dict[str, str] = {}
        app = object.__new__(DownloaderApp)
        app.secure_store = SimpleNamespace(
            set_setting=lambda name, value: saved_settings.__setitem__(name, value)
        )

        app._set_usenet_finder_cache_duration(36)

        self.assertEqual(app.usenet_finder_cache_duration_hours, 36)
        self.assertEqual(saved_settings[CACHE_DURATION_SETTING], "36")

    def test_single_instance_lock_rejects_second_owner_and_releases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            application_id = f"DeepbridDownloaderTest{uuid.uuid4().hex}"
            first_lock = acquire_single_instance(Path(temporary_directory), application_id)
            self.assertIsNotNone(first_lock)
            assert first_lock is not None
            try:
                self.assertIsNone(acquire_single_instance(Path(temporary_directory), application_id))
            finally:
                first_lock.close()

            second_lock = acquire_single_instance(Path(temporary_directory), application_id)
            self.assertIsNotNone(second_lock)
            assert second_lock is not None
            second_lock.close()

    def test_startup_update_preference_persists_both_checkbox_states(self) -> None:
        saved_settings: dict[str, str] = {}
        app = object.__new__(DownloaderApp)
        app.secure_store = SimpleNamespace(
            set_setting=lambda name, value: saved_settings.__setitem__(name, value)
        )
        app.auto_check_updates = SimpleNamespace(get=lambda: True)

        app._persist_auto_check_updates()
        self.assertEqual(saved_settings["auto_check_updates_on_startup"], "true")

        app.auto_check_updates = SimpleNamespace(get=lambda: False)
        app._persist_auto_check_updates()
        self.assertEqual(saved_settings["auto_check_updates_on_startup"], "false")

    def test_startup_download_preference_persists_both_checkbox_states(self) -> None:
        saved_settings: dict[str, str] = {}
        app = object.__new__(DownloaderApp)
        app.secure_store = SimpleNamespace(
            set_setting=lambda name, value: saved_settings.__setitem__(name, value)
        )
        app.auto_start_downloads = SimpleNamespace(get=lambda: True)

        app._persist_auto_start_downloads()
        self.assertEqual(saved_settings["auto_start_downloads_on_startup"], "true")

        app.auto_start_downloads = SimpleNamespace(get=lambda: False)
        app._persist_auto_start_downloads()
        self.assertEqual(saved_settings["auto_start_downloads_on_startup"], "false")

    def test_startup_download_preference_defaults_to_enabled(self) -> None:
        self.assertTrue(_setting_is_enabled(None, default=True))
        self.assertTrue(_setting_is_enabled("true", default=True))
        self.assertFalse(_setting_is_enabled("false", default=True))

    def test_missing_and_invalid_startup_api_keys_report_a_problem(self) -> None:
        events: list[tuple] = []
        app = object.__new__(DownloaderApp)
        app.api_key = SimpleNamespace(get=lambda: "")
        app.events = SimpleNamespace(put=events.append)

        app._check_api_key_on_startup()

        self.assertEqual(
            events,
            [StartupApiKeyProblemEvent("No API key is configured.")],
        )

        events.clear()
        with patch(
            "src.app.DeepbridClient",
            **{"return_value.validate_api_key.side_effect": DeepbridError("rejected", status_code=401)},
        ):
            app._validate_api_key_on_startup("bad-key")

        self.assertEqual(
            events,
            [
                StartupApiKeyProblemEvent(
                    "The configured API key is invalid (HTTP 401)."
                )
            ],
        )

    def test_settings_api_key_check_reports_valid_and_invalid_results(self) -> None:
        events: list[tuple] = []
        app = object.__new__(DownloaderApp)
        app.events = SimpleNamespace(put=events.append)
        with patch("src.app.DeepbridClient") as client:
            app._validate_api_key_in_background("valid-key", None, None)
        client.return_value.validate_api_key.assert_called_once_with()
        self.assertEqual(
            events.pop(),
            ApiKeyCheckEvent(None, None, "API key is valid."),
        )

        with patch(
            "src.app.DeepbridClient",
            **{"return_value.validate_api_key.side_effect": DeepbridError("rejected", status_code=401)},
        ):
            app._validate_api_key_in_background("bad-key", None, None)
        self.assertEqual(
            events,
            [
                ApiKeyCheckEvent(
                    None,
                    None,
                    "API key is invalid. Check it or get a new key.",
                )
            ],
        )

    def test_api_key_problem_opens_settings_only_once(self) -> None:
        messages: list[str] = []
        app = object.__new__(DownloaderApp)
        app.api_key_problem_shown = False
        app.status_text = SimpleNamespace(set=messages.append)
        app._show_settings = lambda initial_filter, **kwargs: messages.append(
            (initial_filter, kwargs)
        )

        app._open_api_key_settings_for_problem("API key rejected.")
        app._open_api_key_settings_for_problem("Another API key error.")

        self.assertTrue(app.api_key_problem_shown)
        self.assertEqual(
            messages,
            [
                "API key rejected.",
                ("API key", {"api_key_notice": "API key rejected."}),
            ],
        )

    def test_startup_downloads_start_when_enabled_and_queue_ready(self) -> None:
        scheduled: list[tuple[int, object]] = []
        app = object.__new__(DownloaderApp)
        app.startup_download_check_pending = True
        app.api_key_problem_shown = False
        app.store = SimpleNamespace(recovered_work=False, next_item=lambda: object())
        app.auto_start_downloads = SimpleNamespace(get=lambda: True)
        app.api_key = SimpleNamespace(get=lambda: "valid-key")
        app.root = SimpleNamespace(after=lambda delay, callback: scheduled.append((delay, callback)))
        started: list[bool] = []
        app._start = lambda: started.append(True)

        app._maybe_start_downloads_on_startup()

        self.assertFalse(app.startup_download_check_pending)
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0][0], 300)
        scheduled[0][1]()
        self.assertEqual(started, [True])

    def test_startup_downloads_stay_idle_when_preference_is_off(self) -> None:
        scheduled: list[tuple[int, object]] = []
        app = object.__new__(DownloaderApp)
        app.startup_download_check_pending = True
        app.api_key_problem_shown = False
        app.store = SimpleNamespace(recovered_work=False, next_item=lambda: object())
        app.auto_start_downloads = SimpleNamespace(get=lambda: False)
        app.api_key = SimpleNamespace(get=lambda: "valid-key")
        app.root = SimpleNamespace(after=lambda delay, callback: scheduled.append((delay, callback)))

        app._maybe_start_downloads_on_startup()

        self.assertFalse(app.startup_download_check_pending)
        self.assertEqual(scheduled, [])

    def test_startup_update_check_respects_checkbox_state(self) -> None:
        app = object.__new__(DownloaderApp)
        app.auto_check_updates = SimpleNamespace(get=lambda: False)
        with patch("src.app.threading.Thread") as thread:
            app._check_for_updates_on_startup()
        thread.assert_not_called()

        app.auto_check_updates = SimpleNamespace(get=lambda: True)
        with patch("src.app.threading.Thread") as thread:
            app._check_for_updates_on_startup()
        thread.assert_called_once_with(target=app._load_startup_update_status, daemon=True)
        thread.return_value.start.assert_called_once_with()

    def test_startup_update_check_is_silent_without_a_newer_release(self) -> None:
        events: list[tuple] = []
        app = object.__new__(DownloaderApp)
        app.events = SimpleNamespace(put=events.append)
        with patch("src.app.fetch_latest_release_details", side_effect=UpdateCheckError("offline")):
            app._load_startup_update_status()
        self.assertEqual(events, [])

        release = LatestRelease(
            "v0.2.11",
            "https://github.com/developper001/Deepbrid-Downloader/releases/tag/v0.2.11",
            (),
        )
        with (
            patch("src.app.fetch_latest_release_details", return_value=release),
            patch("src.app.is_newer_version", return_value=False),
        ):
            app._load_startup_update_status()
        self.assertEqual(events, [])

    def test_startup_update_check_queues_prompt_for_newer_release(self) -> None:
        release = LatestRelease(
            "v0.2.12",
            "https://github.com/developper001/Deepbrid-Downloader/releases/tag/v0.2.12",
            (),
        )
        events: list[tuple] = []
        app = object.__new__(DownloaderApp)
        app.events = SimpleNamespace(put=events.append)
        with (
            patch("src.app.fetch_latest_release_details", return_value=release),
            patch("src.app.is_newer_version", return_value=True),
            patch("src.app.platform_release_asset", return_value=None),
        ):
            app._load_startup_update_status()

        self.assertEqual(events, [StartupUpdateEvent(release, None)])

    def test_release_version_comparison(self) -> None:
        self.assertTrue(is_newer_version("v0.2.6", "0.2.5"))
        self.assertFalse(is_newer_version("v0.2.5", "0.2.5"))
        self.assertFalse(is_newer_version("v0.2.4", "0.2.5"))
        self.assertTrue(is_newer_version("0.2.9", "v0.2.8"))
        self.assertFalse(is_newer_version("latest", "0.2.5"))

    def test_fetch_latest_release_returns_tag_and_link(self) -> None:
        response = FakeResponse(
            200,
            {},
            b'{"tag_name":"v0.2.6",'
            b'"html_url":"https://github.com/developper001/Deepbrid-Downloader/releases/tag/v0.2.6"}',
        )
        with patch("src.app_info.urllib.request.urlopen", return_value=response) as open_url:
            release = fetch_latest_release()

        self.assertEqual(
            release,
            (
                "v0.2.6",
                "https://github.com/developper001/Deepbrid-Downloader/releases/tag/v0.2.6",
            ),
        )
        self.assertEqual(open_url.call_args.args[0].full_url, GITHUB_LATEST_RELEASE_URL)

    def test_fetch_latest_release_details_parses_verified_platform_assets(self) -> None:
        checksum = "a" * 64
        response = FakeResponse(
            200,
            {},
            json.dumps(
                {
                    "tag_name": "v0.2.9",
                    "html_url": "https://github.com/developper001/Deepbrid-Downloader/releases/tag/v0.2.9",
                    "assets": [
                        {
                            "name": "DeepbridDownloader-Windows-0.2.9.exe",
                            "browser_download_url": "https://github.com/developper001/Deepbrid-Downloader/releases/download/v0.2.9/DeepbridDownloader-Windows-0.2.9.exe",
                            "size": 42,
                            "digest": f"sha256:{checksum}",
                        },
                        {
                            "name": "DeepbridDownloader-Linux-0.2.9",
                            "browser_download_url": "http://example.test/app",
                            "size": 42,
                            "digest": f"sha256:{checksum}",
                        },
                    ],
                }
            ).encode(),
        )
        with patch("src.app_info.urllib.request.urlopen", return_value=response):
            release = fetch_latest_release_details()

        asset = platform_release_asset(release, "Windows")
        self.assertIsNotNone(asset)
        assert asset is not None
        self.assertEqual(asset.sha256, checksum)
        self.assertIsNone(platform_release_asset(release, "Linux"))

    def test_unreleased_local_version_does_not_offer_an_older_release(self) -> None:
        release = LatestRelease(
            "v0.2.8",
            "https://github.com/developper001/Deepbrid-Downloader/releases/tag/v0.2.8",
            (),
        )
        asset = ReleaseAsset(
            "DeepbridDownloader-Windows-0.2.8.exe",
            "https://github.com/developper001/Deepbrid-Downloader/releases/download/v0.2.8/DeepbridDownloader-Windows-0.2.8.exe",
            1,
            "a" * 64,
        )
        app = object.__new__(DownloaderApp)
        events: list[tuple] = []
        app.events = SimpleNamespace(put=events.append)
        with (
            patch("src.app.fetch_latest_release_details", return_value=release),
            patch("src.app.platform_release_asset", return_value=asset),
        ):
            app._load_update_status(None, None, None, None, None)

        self.assertIn("newer than the latest published release", events[0].message)
        self.assertIsNone(events[0].release)
        self.assertIsNone(events[0].asset)

    def test_release_details_select_the_current_platform_asset(self) -> None:
        windows = ReleaseAsset(
            "DeepbridDownloader-Windows-0.2.9.exe",
            "https://github.com/developper001/Deepbrid-Downloader/releases/download/v0.2.9/DeepbridDownloader-Windows-0.2.9.exe",
            1,
            "a" * 64,
        )
        linux = ReleaseAsset(
            "DeepbridDownloader-Linux-0.2.9",
            "https://github.com/developper001/Deepbrid-Downloader/releases/download/v0.2.9/DeepbridDownloader-Linux-0.2.9",
            1,
            "b" * 64,
        )
        release = LatestRelease("v0.2.9", "https://github.com/developper001/Deepbrid-Downloader/releases/tag/v0.2.9", (windows, linux))

        self.assertEqual(platform_release_asset(release, "Windows"), windows)
        self.assertEqual(platform_release_asset(release, "Linux"), linux)
        self.assertIsNone(platform_release_asset(release, "FreeBSD"))

    def test_platform_asset_prefers_stable_name_and_falls_back_to_legacy_name(self) -> None:
        stable_asset = ReleaseAsset(
            "DeepbridDownloader-Windows.exe",
            "https://github.com/developper001/Deepbrid-Downloader/releases/download/v0.2.10/DeepbridDownloader-Windows.exe",
            1,
            "a" * 64,
        )
        legacy_asset = ReleaseAsset(
            "DeepbridDownloader-Windows-0.2.10.exe",
            "https://github.com/developper001/Deepbrid-Downloader/releases/download/v0.2.10/DeepbridDownloader-Windows-0.2.10.exe",
            1,
            "b" * 64,
        )
        release_with_both = LatestRelease("v0.2.10", "https://github.com/developper001/Deepbrid-Downloader/releases/tag/v0.2.10", (legacy_asset, stable_asset))
        release_with_legacy_only = LatestRelease("v0.2.10", release_with_both.release_url, (legacy_asset,))

        self.assertEqual(platform_release_asset(release_with_both, "Windows"), stable_asset)
        self.assertEqual(platform_release_asset(release_with_legacy_only, "Windows"), legacy_asset)

    def test_update_download_reports_progress_and_checks_sha256(self) -> None:
        payload = b"verified update binary"
        asset = ReleaseAsset(
            "DeepbridDownloader-Linux-0.2.9",
            "https://github.com/developper001/Deepbrid-Downloader/releases/download/v0.2.9/DeepbridDownloader-Linux-0.2.9",
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )
        progress: list[tuple[int, int]] = []
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch(
                "src.update_manager.urllib.request.urlopen",
                return_value=FakeResponse(200, {}, payload),
            ):
                staged_path = download_update_asset(
                    asset,
                    Path(temporary_directory),
                    lambda received, total: progress.append((received, total)),
                )

            self.assertEqual(staged_path.read_bytes(), payload)
            self.assertEqual(progress[0], (0, len(payload)))
            self.assertEqual(progress[-1], (len(payload), len(payload)))
            staged_path.unlink()

            bad_asset = ReleaseAsset(asset.name, asset.download_url, len(payload), "0" * 64)
            with patch(
                "src.update_manager.urllib.request.urlopen",
                return_value=FakeResponse(200, {}, payload),
            ):
                with self.assertRaisesRegex(Exception, "SHA-256"):
                    download_update_asset(bad_asset, Path(temporary_directory), lambda *_: None)
            self.assertEqual(list(Path(temporary_directory).glob("*.download")), [])

    def test_host_refresh_error_preserves_http_401_for_popup(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        events = []
        app.events = SimpleNamespace(put=events.append)

        with patch(
            "src.app.DeepbridClient.fetch_hosts",
            side_effect=DeepbridError("API key rejected", status_code=401),
        ):
            app._load_hosts("invalid-test-key")

        self.assertEqual(events,         [HostsErrorEvent("API key rejected", 401)])

    def test_host_limits_error_preserves_http_401_for_popup(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        events = []
        app.events = SimpleNamespace(put=events.append)

        with (
            patch("src.app.DeepbridClient.fetch_hosts", return_value={"example.com": "up"}),
            patch(
                "src.app.DeepbridClient.fetch_host_limits",
                side_effect=DeepbridError("API key rejected", status_code=401),
            ),
        ):
            app._load_hosts("invalid-test-key")

        self.assertEqual(
            events,
            [HostsLoadedEvent({"example.com": "up"}, {}, "API key rejected", 401)],
        )

    def test_refresh_hosts_opens_cached_popup_before_scheduling_fetch(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app.hosts = {"cached.example": "up"}
        app.host_limits = {"cached.example": "5 GB remaining"}
        app.api_key = SimpleNamespace(get=lambda: "test-key")
        app.status_text = SimpleNamespace(set=lambda _value: None)
        app.refresh_hosts_button = SimpleNamespace(
            instate=lambda _states: False,
            configure=lambda **_kwargs: None,
        )
        sequence: list[object] = []
        pending_callbacks = []
        app._show_hosts_popup = lambda hosts, limits, error: sequence.append(
            ("popup", hosts, limits, error)
        )
        app.root = SimpleNamespace(
            after_idle=lambda callback: (sequence.append("scheduled"), pending_callbacks.append(callback))
        )

        with patch("src.app.threading.Thread") as thread_class:
            app._refresh_hosts(show_popup=True)

            self.assertEqual(sequence[0], ("popup", app.hosts, app.host_limits, None))
            self.assertEqual(sequence[1], "scheduled")
            thread_class.assert_not_called()

            pending_callbacks[0]()

        thread_class.assert_called_once_with(target=app._load_hosts, args=("test-key",), daemon=True)
        thread_class.return_value.start.assert_called_once_with()

    def test_host_rows_filter_and_sort_by_each_column(self) -> None:
        rows = [
            ("zeta.example", "Unavailable", "9 GB remaining"),
            ("alpha.example", "Available", "20 GB remaining"),
            ("beta.example", "Available", "5 GB remaining"),
        ]

        self.assertEqual(
            _filter_and_sort_host_rows(rows, "TA", "host", False),
            [rows[2], rows[0]],
        )
        self.assertEqual(
            _filter_and_sort_host_rows(rows, "", "availability", False),
            [rows[1], rows[2], rows[0]],
        )
        self.assertEqual(
            _filter_and_sort_host_rows(rows, "", "limit", True),
            [rows[0], rows[2], rows[1]],
        )

    def test_plain_click_replaces_existing_selection(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app.table = FakeSelectionTable(["1", "2", "3"])
        app._selection_anchor = None

        app._select_table_row(SimpleNamespace(x=0, y=0, state=0))
        app._select_table_row(SimpleNamespace(x=0, y=2, state=0))

        self.assertEqual(app.table.selection(), ("3",))
        self.assertTrue(app.table.has_widget_focus)

    def test_control_a_selects_all_queue_rows(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app.table = FakeSelectionTable(["1", "2", "3"])
        app.table.selection_set("2")

        result = app._select_all_table_rows()

        self.assertEqual(result, "break")
        self.assertEqual(app.table.selection(), ("1", "2", "3"))

    def test_control_click_adds_and_toggles_rows(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app.table = FakeSelectionTable(["1", "2", "3"])
        app._selection_anchor = None

        app._select_table_row(SimpleNamespace(x=0, y=0, state=0))
        app._select_table_row(SimpleNamespace(x=0, y=2, state=0x0004))
        self.assertEqual(set(app.table.selection()), {"1", "3"})

        app._select_table_row(SimpleNamespace(x=0, y=0, state=0x0004))
        self.assertEqual(app.table.selection(), ("3",))

    def test_shift_click_adds_inclusive_range_from_anchor(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app.table = FakeSelectionTable(["1", "2", "3", "4"])
        app._selection_anchor = None

        app._select_table_row(SimpleNamespace(x=0, y=0, state=0))
        app._select_table_row(SimpleNamespace(x=0, y=3, state=0x0004))
        app._select_table_row(SimpleNamespace(x=0, y=2, state=0x0001))

        self.assertEqual(set(app.table.selection()), {"3", "4"})

    def test_clicking_blank_table_space_clears_selection(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app.table = FakeSelectionTable(["1", "2"])
        app.table.selected = ["1", "2"]
        app._selection_anchor = "1"

        app._select_table_row(SimpleNamespace(x=0, y=4, state=0))

        self.assertEqual(app.table.selection(), ())
        self.assertIsNone(app._selection_anchor)

    def test_bulk_actions_use_display_order(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app.table = FakeSelectionTable(["1", "2", "3"])
        app.table.selected = ["3", "1"]
        items = [
            SimpleNamespace(id=3, status="failed"),
            SimpleNamespace(id=2, status="queued"),
            SimpleNamespace(id=1, status="blocked"),
        ]
        app.store = SimpleNamespace(list_items=lambda: items, retry_item=lambda item_id: retried.append(item_id))
        app.item_speeds = {}
        app._log = lambda _message: None
        app._refresh_rows = lambda: None
        retried: list[int] = []

        app._retry_item()

        self.assertEqual(retried, [1, 3])

    def test_api_key_dashboard_link_opens_browser(self) -> None:
        self.assertEqual(API_KEY_DASHBOARD_URL, "https://www.deepbrid.com/devices")
        with patch("src.app.webbrowser.open") as open_browser:
            DownloaderApp._open_api_key_dashboard()
        open_browser.assert_called_once_with(API_KEY_DASHBOARD_URL)

    def test_console_stream_forwards_complete_and_partial_lines(self) -> None:
        messages = []
        stream = _ConsoleStream(messages.append)
        stream.write("first\nsecond")
        stream.flush()
        self.assertEqual(messages, ["first", "second"])

    def test_output_log_appends_lines_and_redacts_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "logs" / "application.log"
            append_output_log_line(
                path,
                "12:34:56",
                "Request failed for https://example.test/path?token=secret",
            )
            append_output_log_line(path, "12:34:57", "Another diagnostic")

            logged = path.read_text(encoding="utf-8")
            self.assertIn("[12:34:56] Request failed for <URL REDACTED>", logged)
            self.assertIn("[12:34:57] Another diagnostic", logged)
            self.assertNotIn("token=secret", logged)

    def test_output_log_setting_creates_file_and_persists_enabled_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "settings.sqlite3")
            app = object.__new__(DownloaderApp)
            app.secure_store = store
            app.append_output_log_path = SimpleNamespace(set=Mock())
            app._log = Mock()
            app._output_log_error_reported = False
            target = Path(temporary_directory) / "logs" / "app.log"

            app._set_append_output_log(True, str(target))

            self.assertTrue(target.is_file())
            self.assertEqual(store.get_setting("append_output_log_enabled"), "true")
            self.assertEqual(store.get_setting("append_output_log_path"), str(target))
            self.assertTrue(app.append_output_log_enabled)
            app.append_output_log_path.set.assert_called_once_with(str(target))

    def test_main_add_links_warns_when_duplicates_are_skipped(self) -> None:
        app = object.__new__(DownloaderApp)
        app.hosts = {"example.com": "up"}
        app.link_placeholder_active = False
        app.links_input = Mock()
        app.links_input.get.return_value = "https://example.com/file"
        app.store = Mock()
        app.store.add.return_value = False
        app.root = object()
        app.status_text = SimpleNamespace(set=Mock())
        app._log = Mock()
        app._refresh_rows = Mock()
        app._save_visible_queue_order = Mock()

        with patch("src.app.messagebox.showwarning") as showwarning:
            app._add_link()

        showwarning.assert_called_once()
        self.assertIn("1 duplicate link", showwarning.call_args.args[1])

    def test_default_download_directory_prefers_existing_user_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            downloads = Path(temporary_directory) / "Downloads"
            downloads.mkdir()
            with patch("src.app.user_downloads_dir", return_value=str(downloads)):
                self.assertEqual(default_download_directory(), downloads)

            missing = downloads / "missing"
            fallback = Path(temporary_directory) / "AppData" / "download"
            with (
                patch("src.app.user_downloads_dir", return_value=str(missing)),
                patch("src.app.OUTPUT_DIR", fallback),
            ):
                self.assertEqual(default_download_directory(), fallback)
                self.assertTrue(fallback.is_dir())

    def test_readme_content_is_available_and_mentions_license(self) -> None:
        readme_text = DownloaderApp.readme_text()
        self.assertIn("## Features", readme_text)
        self.assertIn("MIT License", readme_text)
        self.assertIn("src/img/DeepbridDownloader.png", DownloaderApp.readme_image_paths())

    def test_about_parser_recognizes_linked_readme_badges(self) -> None:
        badge_line = (
            "[![CI](https://img.shields.io/example.svg)]"
            "(https://github.com/example/repo/actions)"
        )
        self.assertEqual(
            _parse_linked_image_badge(badge_line),
            ("CI", "https://github.com/example/repo/actions"),
        )
        self.assertIsNone(_parse_linked_image_badge("ordinary README text"))

    def test_runtime_database_and_download_folders_use_app_data(self) -> None:
        self.assertEqual(DATABASE_PATH.parent, APP_DATA_DIR)
        self.assertEqual(OUTPUT_DIR.parent, APP_DATA_DIR)
        self.assertNotEqual(DATABASE_PATH, LEGACY_DATABASE_PATH)
        self.assertNotEqual(DATABASE_PATH, LEGACY_QUEUE_DATABASE_PATH)

    def test_dark_theme_state_is_persisted(self) -> None:
        app = object.__new__(DownloaderApp)
        app.secure_store = SecureStore(Path(tempfile.mkdtemp()) / "theme.sqlite3")
        app.dark_theme = True
        app._persist_theme()
        self.assertEqual(app.secure_store.get_setting("dark_theme"), "true")

        app.dark_theme = False
        config = AppConfiguration.load(
            app.secure_store,
            COLUMN_ORDER,
            DEFAULT_COLUMNS,
            lambda: Path(tempfile.gettempdir()),
            lambda: None,
            lambda: None,
            lambda: Path(tempfile.gettempdir()) / "deepbrid-output.log",
        )
        self.assertTrue(config.dark_theme)

    def test_missing_api_key_and_invalid_download_folders_have_explicit_messages(self) -> None:
        self.assertIn("API key", validate_api_key("  "))
        self.assertIn("Choose a download folder", validate_output_folder(""))
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            self.assertIsNone(validate_output_folder(root))
            self.assertIn("does not exist", validate_output_folder(root / "missing"))
            file_path = root / "not-a-folder"
            file_path.write_text("x", encoding="utf-8")
            self.assertIn("not a folder", validate_output_folder(file_path))

    def test_byte_duration_and_sort_values_are_consistent(self) -> None:
        self.assertNotIn("remaining", DEFAULT_COLUMNS)
        self.assertNotIn("status", DEFAULT_COLUMNS)
        self.assertIn("progress", DEFAULT_COLUMNS)
        self.assertIn("progress_percentage", COLUMN_ORDER)
        self.assertNotIn("progress_percentage", DEFAULT_COLUMNS)
        self.assertIn("verification", DEFAULT_COLUMNS)
        self.assertEqual(size_verification_label("completed", True), "Verified")
        self.assertEqual(size_verification_label("completed", False), "Not verified")
        self.assertEqual(size_verification_label("queued", False), "—")
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(None), "Unknown")
        self.assertEqual(progress_indicator_values("downloading", 1000, 500), (0.5, "50%"))
        self.assertEqual(progress_indicator_values("queued", 1000, 0), (0.0, "0%"))
        self.assertEqual(progress_indicator_values("completed", None, 1000), (1.0, "100%"))
        self.assertEqual(progress_indicator_values("downloading", 1000, 1500), (1.0, "100%"))
        self.assertEqual(progress_indicator_values("downloading", None, 500), (None, "—"))
        self.assertEqual(progress_indicator_values("queued", None, 0), (None, "—"))
        self.assertEqual(format_duration(3661), "1h 1m")
        self.assertEqual(format_item_eta("downloading", 1000, 400, 100), "6s")
        self.assertEqual(format_item_eta("downloading", 1000, 400, 0), "Calculating")
        self.assertEqual(format_item_eta("queued", None, 0, 100), "—")
        self.assertEqual(format_item_eta("completed", 1000, 1000, 100), "0s")
        app = object.__new__(DownloaderApp)
        app.item_speeds = {1: 100}
        app.item_status_messages = {1: "Link retry 2/5 in 3s"}
        item = SimpleNamespace(
            id=1,
            filename="Bravo.zip",
            url="https://example.test/alpha.zip",
            host_message="SUPPORTED",
            status="queued",
            total=500,
            downloaded=100,
        )
        self.assertEqual(app._sort_value(item, "filename"), "bravo.zip")
        self.assertEqual(app._sort_value(item, "remaining"), 400)
        self.assertEqual(app._sort_value(item, "eta"), "link retry 2/5 in 3s")

    def test_progress_indicators_wait_for_theme_initialization(self) -> None:
        app = object.__new__(DownloaderApp)
        app._progress_indicator_layout_pending = False

        app._position_progress_indicators()

        self.assertFalse(app._progress_indicator_layout_pending)

    def test_total_eta_shows_known_work_when_queued_sizes_are_unknown(self) -> None:
        app = object.__new__(DownloaderApp)
        eta_text: list[str] = []
        app.total_eta_text = SimpleNamespace(set=eta_text.append)
        items = [
            SimpleNamespace(enabled=True, status="completed", total=1000, downloaded=1000),
            SimpleNamespace(enabled=True, status="downloading", total=1000, downloaded=500),
            SimpleNamespace(enabled=True, status="queued", total=None, downloaded=0),
        ]

        app._update_total_eta(items, 100)

        self.assertEqual(eta_text, ["Total remaining: ~15s (1 unknown @ avg 1000 B)"])

    def test_queue_progress_counts_enabled_files_and_estimates_unknown_active_size(self) -> None:
        app = object.__new__(DownloaderApp)
        progress: list[float] = []
        queue_text: list[str] = []
        app.progress_value = SimpleNamespace(set=progress.append)
        app.queue_progress_text = SimpleNamespace(set=queue_text.append)
        items = [
            SimpleNamespace(enabled=True, status="completed", total=1000, downloaded=1000),
            SimpleNamespace(enabled=True, status="downloading", total=None, downloaded=500),
            SimpleNamespace(enabled=True, status="queued", total=None, downloaded=0),
            SimpleNamespace(enabled=False, status="queued", total=None, downloaded=0),
        ]

        app._update_queue_progress(items)

        self.assertEqual(progress, [50.0])
        self.assertEqual(queue_text, ["Queue: 1/3 files (50%)"])

    def test_total_eta_keeps_unknown_when_no_completed_size_sample_exists(self) -> None:
        app = object.__new__(DownloaderApp)
        eta_text: list[str] = []
        app.total_eta_text = SimpleNamespace(set=eta_text.append)
        items = [SimpleNamespace(enabled=True, status="queued", total=None, downloaded=0)]

        app._update_total_eta(items, 100)

        self.assertEqual(eta_text, ["Total remaining: calculating (1 size(s) unknown)"])

    def test_total_speed_uses_a_slow_exponential_average(self) -> None:
        average = smooth_rate(100.0, 1000.0)
        self.assertEqual(average, 190.0)
        self.assertAlmostEqual(smooth_rate(average, 100.0), 181.0)
        self.assertEqual(smooth_rate(average, 0.0), average)


class LogRedactionTests(unittest.TestCase):
    def test_redacts_plain_and_form_encoded_urls_but_keeps_diagnostics(self) -> None:
        contents = (
            "HTTP 403 for https://host.example/file.zip\n"
            "curl --data link=https%3A%2F%2Fhost.example%2Ffile.zip\n"
        )

        redacted = redact_log_urls(contents)

        self.assertNotIn("https://", redacted)
        self.assertNotIn("https%3A", redacted)
        self.assertIn("HTTP 403", redacted)
        self.assertEqual(redacted.count("<URL REDACTED>"), 2)

    def test_legacy_database_is_backed_up_before_old_folder_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            old_directory = root / "download"
            old_directory.mkdir()
            old_database = old_directory / "queue.sqlite3"
            new_database = root / "src" / "deepbrid_downloader.sqlite3"
            with closing(sqlite3.connect(old_database)) as connection:
                connection.execute("CREATE TABLE data (value TEXT NOT NULL)")
                connection.execute("INSERT INTO data VALUES ('preserved')")
                connection.commit()

            migrate_legacy_database(old_database, new_database)
            with closing(sqlite3.connect(new_database)) as connection:
                copied_value = connection.execute("SELECT value FROM data").fetchone()[0]

            self.assertEqual(copied_value, "preserved")
            self.assertTrue(remove_legacy_storage(old_directory, root / "Downloads"))
            self.assertFalse(old_directory.exists())

    def test_legacy_source_database_is_migrated_before_legacy_queue_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_database = root / "src" / "legacy.sqlite3"
            queue_database = root / "download" / "queue.sqlite3"
            destination = root / "application-data" / "queue.sqlite3"
            for database, value in ((source_database, "current"), (queue_database, "older")):
                database.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(database)) as connection:
                    connection.execute("CREATE TABLE data (value TEXT NOT NULL)")
                    connection.execute("INSERT INTO data VALUES (?)", (value,))
                    connection.commit()

            migrate_legacy_databases(destination, (source_database, queue_database))

            with closing(sqlite3.connect(destination)) as connection:
                copied_value = connection.execute("SELECT value FROM data").fetchone()[0]
            self.assertEqual(copied_value, "current")

    def test_legacy_folder_with_unexpected_files_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            old_directory = Path(temporary_directory) / "download"
            old_directory.mkdir()
            (old_directory / "user-file.bin").write_bytes(b"preserve")

            self.assertFalse(remove_legacy_storage(old_directory, Path(temporary_directory) / "Downloads"))
            self.assertTrue((old_directory / "user-file.bin").exists())


class LinkExtractionTests(unittest.TestCase):
    def test_extracts_only_supported_urls_from_text_and_html(self) -> None:
        hosts = {"supported.example": "up", "down.example": "down (today)"}
        source = (
            '<a href="https://www.supported.example/file?id=1">download</a>'
            '<a href="https://unknown.example/file">unsupported</a>'
            ' Also https://down.example/other.zip, and https://supported.example/two.zip.'
        )

        links = extract_supported_links(source, hosts)

        self.assertEqual(
            links,
            [
                ("https://www.supported.example/file?id=1", "up", "UP: supported.example"),
                ("https://down.example/other.zip", "down", "DOWN: down.example"),
                ("https://supported.example/two.zip", "up", "UP: supported.example"),
            ],
        )
        self.assertEqual(
            supported_link_status("https://sub.supported.example/a", hosts),
            ("up", "UP: supported.example"),
        )
        self.assertEqual(
            supported_link_status("https://ddownload.com/file", {"ddownload": "supported"}),
            ("supported", "SUPPORTED: ddownload"),
        )

    def test_extracts_alias_domains_when_host_is_unavailable(self) -> None:
        hosts = {"ddl.to,ddownload.com": "down (today)"}

        links = extract_supported_links(
            "https://ddl.to/file.zip https://ddownload.com/part01.rar",
            hosts,
        )

        self.assertEqual(
            links,
            [
                ("https://ddl.to/file.zip", "down", "DOWN: ddl.to"),
                ("https://ddownload.com/part01.rar", "down", "DOWN: ddownload.com"),
            ],
        )


class SecureStoreTests(unittest.TestCase):
    def test_api_key_is_encrypted_in_sqlite_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "queue.sqlite3"
            store = SecureStore(database_path)
            store.save_api_key("sensitive-test-api-key")
            self.assertEqual(store.get_api_key(), "sensitive-test-api-key")
            store.set_setting("output_directory", "C:/Downloads")
            self.assertEqual(store.get_setting("output_directory"), "C:/Downloads")

            with closing(sqlite3.connect(database_path)) as connection:
                settings = dict(connection.execute("SELECT name, value FROM app_settings").fetchall())
            saved_value = bytes(settings["api_key"])
            self.assertNotIn(b"sensitive-test-api-key", bytes(saved_value))
            self.assertEqual(len(bytes(settings["encryption_key"])), 32)

            copied_database = Path(temporary_directory) / "portable-copy.sqlite3"
            shutil.copy2(database_path, copied_database)
            copied_store = SecureStore(copied_database)
            self.assertEqual(copied_store.get_api_key(), "sensitive-test-api-key")

    def test_api_key_cannot_be_saved_as_plain_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "queue.sqlite3")
            with self.assertRaises(ValueError):
                store.set_setting("api_key", "sensitive-test-api-key")

    def test_encrypted_settings_round_trip_without_storing_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "queue.sqlite3"
            store = SecureStore(database_path)
            store.set_encrypted_setting("finder_cache", "https://private.example/file", b"test")

            self.assertEqual(
                store.get_encrypted_setting("finder_cache", b"test"),
                "https://private.example/file",
            )
            with closing(sqlite3.connect(database_path)) as connection:
                value = connection.execute(
                    "SELECT value FROM app_settings WHERE name = 'finder_cache'"
                ).fetchone()[0]
            self.assertNotIn(b"private.example", bytes(value))


class UsenetFinderStateTests(unittest.TestCase):
    def test_search_and_resolved_package_persist_across_state_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "queue.sqlite3")
            package = FinderPackage(
                name="Example package",
                files=(
                    FinderFile(
                        name="example.mkv",
                        link="https://private.example/file",
                        size="1 GB",
                        is_video=True,
                        inaccessible=None,
                        metadata={"link": "https://private.example/file"},
                    ),
                ),
                metadata={"pkg": "Example package"},
            )
            first_state = UsenetFinderState(store, clock=lambda: 1000)
            first_state.save_search("example release", "tv-hd")
            first_state.save_resolved("opaque-token", package)

            reopened_state = UsenetFinderState(store, clock=lambda: 1001)
            self.assertEqual(reopened_state.load_search(), ("example release", "tv-hd"))
            self.assertEqual(reopened_state.get_resolved("opaque-token"), package)
            self.assertEqual(
                reopened_state.get_resolved_with_expiry("opaque-token"),
                (package, 1000 + CACHE_TTL_SECONDS),
            )
            self.assertEqual(
                reopened_state.load_search_history(),
                [("example release", "tv-hd")],
            )

    def test_search_history_keeps_recent_unique_query_and_category_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "queue.sqlite3")
            state = UsenetFinderState(store, clock=lambda: 1000)
            state.save_search("first", "TV")
            state.save_search("second", "Movies")
            state.save_search("first", "TV")

            self.assertEqual(
                state.load_search_history()[:2],
                [("first", "TV"), ("second", "Movies")],
            )

    def test_search_history_can_be_cleared_without_clearing_the_last_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "queue.sqlite3")
            state = UsenetFinderState(store, clock=lambda: 1000)
            state.save_search("example", "TV")

            state.clear_search_history()

            self.assertEqual(state.load_search_history(), [])
            self.assertEqual(state.load_search(), ("example", "TV"))

    def test_last_selected_result_persists_and_resolved_links_use_configured_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "queue.sqlite3")
            package = FinderPackage("Example", (), {})
            state = UsenetFinderState(
                store,
                clock=lambda: 1000,
                cache_duration_hours=2,
            )
            state.save_last_selected_result("example", "TV", "opaque-token", 15)
            state.save_resolved("opaque-token", package)

            reopened_state = UsenetFinderState(
                store,
                clock=lambda: 1001,
                cache_duration_hours=2,
            )
            self.assertEqual(
                reopened_state.load_last_selected_result(),
                ("example", "TV", "opaque-token", 15),
            )
            self.assertEqual(
                reopened_state.get_resolved_with_expiry("opaque-token"),
                (package, 1000 + 2 * 60 * 60),
            )
            self.assertIsNone(
                UsenetFinderState(
                    store,
                    clock=lambda: 1000 + 2 * 60 * 60,
                    cache_duration_hours=2,
                ).get_resolved("opaque-token")
            )

    def test_search_results_persist_and_are_reused_across_state_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "queue.sqlite3")
            page = FinderSearchPage(
                results=(
                    FinderResult(
                        title="Example release",
                        token="opaque-token",
                        category="TV",
                        size="1 GB",
                        size_bytes=1_000_000_000,
                        date="2026-10-01",
                        metadata={"sources": 2},
                    ),
                ),
                has_more=True,
            )
            first_state = UsenetFinderState(store, clock=lambda: 1000)
            first_state.save_search_page("example", "TV", 0, 15, page)

            reopened_state = UsenetFinderState(store, clock=lambda: 1001)
            self.assertEqual(reopened_state.get_search_page("example", "TV", 0, 15), page)
            self.assertEqual(
                reopened_state.get_search_page_with_expiry("example", "TV", 0, 15),
                (page, 1000 + CACHE_TTL_SECONDS),
            )
            self.assertIsNone(reopened_state.get_search_page("example", "", 0, 15))

    def test_search_results_expire_using_configured_cache_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "queue.sqlite3")
            page = FinderSearchPage((), False)
            state = UsenetFinderState(
                store,
                clock=lambda: 1000,
                cache_duration_hours=2,
            )
            state.save_search_page("example", "", 0, 15, page)

            reopened_state = UsenetFinderState(
                store,
                clock=lambda: 1000 + 2 * 60 * 60,
                cache_duration_hours=2,
            )
            self.assertIsNone(reopened_state.get_search_page("example", "", 0, 15))

    def test_search_cache_uses_the_configured_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "queue.sqlite3")
            state = UsenetFinderState(
                store,
                clock=lambda: 1000,
                cache_duration_hours=2,
            )
            self.assertEqual(state.cache_duration_hours_value(), 2)
            self.assertEqual(state.cache_ttl_seconds(), 2 * 60 * 60)

    def test_resolved_package_cache_expires_after_one_day(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "queue.sqlite3")
            package = FinderPackage("Example", (), {})
            state = UsenetFinderState(store, clock=lambda: 1000)
            state.save_resolved("opaque-token", package)

            reopened_state = UsenetFinderState(
                store,
                clock=lambda: 1000 + CACHE_TTL_SECONDS,
            )
            self.assertIsNone(reopened_state.get_resolved("opaque-token"))

    def test_cache_duration_accepts_only_whole_hours_in_supported_range(self) -> None:
        self.assertEqual(valid_cache_duration_hours(1), 1)
        self.assertEqual(valid_cache_duration_hours("720"), 720)
        for invalid in ("0", "721", "1.5", "abc"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                valid_cache_duration_hours(invalid)

    def test_clear_cache_removes_search_and_resolved_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "queue.sqlite3")
            state = UsenetFinderState(store, clock=lambda: 1000)
            state.save_search_page("example", "", 0, 15, FinderSearchPage((), False))
            state.save_resolved("opaque-token", FinderPackage("Example", (), {}))
            state.save_last_selected_result("example", "", "opaque-token", 0)

            state.clear_cache()

            self.assertIsNone(state.get_search_page("example", "", 0, 15))
            self.assertIsNone(state.get_resolved("opaque-token"))
            self.assertIsNone(state.load_last_selected_result())
            self.assertIsNone(store.get_encrypted_setting("usenet_finder_search_cache", b"usenet-finder-search-cache-v1"))
            self.assertIsNone(store.get_encrypted_setting("usenet_finder_resolved_cache", b"usenet-finder-resolved-cache-v1"))


class ResumeTests(unittest.TestCase):
    def test_successful_download_reports_whether_size_was_verified(self) -> None:
        for headers, expected_verification in (
            ({"Content-Length": "3"}, True),
            ({}, False),
        ):
            with self.subTest(expected_verification=expected_verification):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    response = FakeResponse(200, headers, b"abc")
                    verification: list[bool] = []

                    with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
                        completed = DeepbridClient("unused").download(
                            "https://example.com/generated",
                            "file.bin",
                            Path(temporary_directory),
                            lambda: False,
                            lambda *_: None,
                            on_size_verified=verification.append,
                        )

                    self.assertTrue(completed)
                    self.assertEqual(verification, [expected_verification])

    def test_short_download_is_rejected_and_kept_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            response = FakeResponse(200, {"Content-Length": "6"}, b"abc")

            with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
                with self.assertRaisesRegex(DeepbridError, "received 3 of 6 bytes"):
                    DeepbridClient("unused").download(
                        "https://example.com/generated",
                        "file.bin",
                        output_dir,
                        lambda: False,
                        lambda *_: None,
                    )

            self.assertFalse((output_dir / "file.bin").exists())
            self.assertEqual((output_dir / ".file.bin.part").read_bytes(), b"abc")

    def test_appends_when_server_honors_range_request(self) -> None:
        self._assert_resume_result(
            status=206,
            headers={"Content-Range": "bytes 3-5/6", "Content-Length": "3"},
            body=b"def",
            expected=b"abcdef",
            expected_range="bytes=3-",
        )

    def test_restarts_file_when_server_ignores_range_request(self) -> None:
        self._assert_resume_result(
            status=200,
            headers={"Content-Length": "3"},
            body=b"new",
            expected=b"new",
            expected_range="bytes=3-",
        )

    def test_existing_destination_is_not_requested_or_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            destination = output_dir / "file.bin"
            destination.write_bytes(b"keep this file")
            progress: list[tuple[int, int | None]] = []

            with patch("src.deepbrid_client.urllib.request.urlopen") as open_url:
                completed = DeepbridClient("unused").download(
                    "https://example.com/generated",
                    "file.bin",
                    output_dir,
                    lambda: False,
                    lambda downloaded, total: progress.append((downloaded, total)),
                )

            self.assertTrue(completed)
            self.assertFalse(open_url.called)
            self.assertEqual(destination.read_bytes(), b"keep this file")
            self.assertEqual(progress, [(len(b"keep this file"), len(b"keep this file"))])

    def test_forced_download_atomically_replaces_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            destination = output_dir / "file.bin"
            destination.write_bytes(b"old file")
            response = FakeResponse(200, {"Content-Length": "8"}, b"new file")

            with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response) as open_url:
                completed = DeepbridClient("unused").download(
                    "https://example.com/generated",
                    "file.bin",
                    output_dir,
                    lambda: False,
                    lambda *_: None,
                    overwrite_existing=True,
                )

            self.assertTrue(completed)
            self.assertTrue(open_url.called)
            self.assertEqual(destination.read_bytes(), b"new file")

    def _assert_resume_result(
        self,
        status: int,
        headers: dict[str, str],
        body: bytes,
        expected: bytes,
        expected_range: str,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            (output_dir / ".file.bin.part").write_bytes(b"abc")
            response = FakeResponse(status, headers, body)

            with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response) as open_url:
                completed = DeepbridClient("unused").download(
                    "https://example.com/generated", "file.bin", output_dir, lambda: False, lambda *_: None
                )

            self.assertTrue(completed)
            self.assertEqual((output_dir / "file.bin").read_bytes(), expected)
            request = open_url.call_args.args[0]
            self.assertEqual(request.get_header("Range"), expected_range)


class UsenetFinderPrototypeTests(unittest.TestCase):
    def test_search_parses_results_and_uses_the_undocumented_query_contract(self) -> None:
        result_payload = {
            "title": "Example release",
            "token": "opaque-token",
            "cat": "TV",
            "size": "1.2 GB",
            "sizeBytes": 1_200_000_000,
            "date": "2026-10-01",
            "sources": 2,
            "dupes": [],
        }
        browser = Mock()
        browser.request_json.return_value = {"results": [result_payload], "hasMore": True}
        page = UsenetFinderClient(browser).search(
            "example release",
            category="tv-hd",
            offset=15,
            limit=15,
        )

        self.assertTrue(page.has_more)
        self.assertEqual(len(page.results), 1)
        result = page.results[0]
        self.assertEqual(result.title, "Example release")
        self.assertEqual(result.token, "opaque-token")
        self.assertEqual(result.size_bytes, 1_200_000_000)
        self.assertIn("sources", result.metadata)
        browser.request_json.assert_called_once_with(
            {
                "do": "search",
                "q": "example release",
                "cat": "tv-hd",
                "offset": "15",
                "limit": "15",
            }
        )

    def test_resolve_marks_unavailable_files_and_preserves_metadata(self) -> None:
        browser = Mock()
        browser.request_json.return_value = {
            "pkg": "Example release",
            "files": [
                {
                    "name": "episode.mkv",
                    "link": "https://download.example/file",
                    "size": "1 GB",
                    "isVideo": True,
                },
                {
                    "name": "archive.rar",
                    "link": "",
                    "size": "2 GB",
                    "inaccessible": "missing_volumes",
                },
                {
                    "name": "relative-link.bin",
                    "link": "/private/download",
                },
                {
                    "name": "finder-url-field.mkv",
                    "url": "https://usenet-2.myfast.link:8183/get/private-id/6/finder-url-field.mkv",
                    "size": "492.41 MB",
                    "isVideo": True,
                },
            ],
        }
        package = UsenetFinderClient(browser).resolve("opaque-token")

        self.assertEqual(package.name, "Example release")
        self.assertEqual(len(package.files), 4)
        self.assertTrue(package.files[0].is_accessible)
        self.assertTrue(package.files[0].is_video)
        self.assertFalse(package.files[1].is_accessible)
        self.assertEqual(package.files[1].inaccessible, "missing_volumes")
        self.assertFalse(package.files[2].is_accessible)
        self.assertTrue(package.files[3].is_accessible)
        self.assertEqual(package.files[3].link, package.files[3].metadata["url"])
        browser.request_json.assert_called_once_with(
            {"do": "process", "token": "opaque-token"}
        )

    def test_invalid_search_inputs_and_response_shapes_are_explicit(self) -> None:
        browser = Mock()
        client = UsenetFinderClient(browser)
        with self.assertRaises(ValueError):
            client.search(" ")
        with self.assertRaises(ValueError):
            client.search("query", offset=-1)
        with self.assertRaises(ValueError):
            client.search("query", limit=101)

        browser.request_json.return_value = {"results": [], "hasMore": "yes"}
        with self.assertRaisesRegex(UsenetFinderError, "hasMore"):
            client.search("query")

    def test_browser_challenge_and_sign_in_html_produce_actionable_error(self) -> None:
        from src.usenet_finder import parse_browser_response

        with self.assertRaisesRegex(UsenetFinderError, "Complete the Cloudflare check"):
            parse_browser_response(403, "text/html", "<html>challenge</html>")
        with self.assertRaisesRegex(UsenetFinderError, "instead of JSON"):
            parse_browser_response(200, "text/html", "<html>login</html>")


class UsenetFinderDialogTests(unittest.TestCase):
    def test_antialiased_globe_assets_are_square_pngs_for_both_themes(self) -> None:
        asset_directory = Path(__file__).resolve().parent.parent / "src"
        for filename in ("usenet-globe-light.png", "usenet-globe-dark.png"):
            with self.subTest(filename=filename):
                asset = (asset_directory / filename).read_bytes()
                self.assertEqual(asset[:8], b"\x89PNG\r\n\x1a\n")
                self.assertEqual(struct.unpack(">II", asset[16:24]), (46, 46))

    def test_saved_search_results_are_restored_without_a_network_request(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        page = FinderSearchPage((), False)
        dialog.state = Mock()
        dialog.state.get_search_page_with_expiry.return_value = (page, 2000)
        dialog.app = SimpleNamespace(_log=Mock())
        dialog._results = {}
        dialog._result_records = []
        dialog.status = Mock()
        with patch.object(dialog, "_show_search_page") as show_search_page:
            dialog._restore_cached_results("example", "TV")

        dialog.state.get_search_page_with_expiry.assert_called_once_with(
            "example", "TV", 0, 15
        )
        show_search_page.assert_called_once_with(page, append=False, cached_expiry=2000)
        dialog.status.set.assert_called_once_with(
            "Restored 0 cached result(s); cache expires "
            f"{datetime.fromtimestamp(2000).strftime('%Y-%m-%d %H:%M')}."
        )

    def test_search_page_cache_hit_skips_the_network_request(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        page = FinderSearchPage((), False)
        dialog.query = SimpleNamespace(get=lambda: "example")
        dialog.category = SimpleNamespace(get=lambda: "TV")
        dialog._offset = 0
        dialog.PAGE_SIZE = 15
        dialog._results = {}
        dialog._result_records = []
        dialog.state = Mock()
        dialog.state.get_search_page_with_expiry.return_value = (page, 2000)
        dialog.client = Mock()
        dialog.status = Mock()
        with (
            patch.object(dialog, "_show_search_page") as show_search_page,
            patch.object(dialog, "_begin_request") as begin_request,
        ):
            dialog._search_page(append=False)

        dialog.state.get_search_page_with_expiry.assert_called_once_with(
            "example", "TV", 0, 15
        )
        show_search_page.assert_called_once_with(page, False, cached_expiry=2000)
        begin_request.assert_not_called()
        dialog.client.search.assert_not_called()
        self.assertIn("Loaded 0 cached result(s); cache expires ", dialog.status.set.call_args.args[0])

    def test_search_opens_the_browser_before_starting_the_request(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        dialog._request_running = False
        dialog._offset = 12
        dialog._results = {"old": object()}
        dialog._result_page_offsets = {}
        dialog._files = {"old": object()}
        dialog._has_more = True
        dialog.app = SimpleNamespace(usenet_browser=Mock())
        dialog.state = Mock()
        dialog.status = Mock()
        dialog.query = SimpleNamespace(get=Mock(return_value="example"))
        dialog.category = SimpleNamespace(get=Mock(return_value=""))
        dialog.results = Mock()
        dialog.results.get_children.return_value = ()
        dialog.files = Mock()
        dialog.files.get_children.return_value = ()
        dialog.more_button = Mock()
        with (
            patch.object(dialog, "_update_file_actions") as update_file_actions,
            patch.object(dialog, "_search_page") as search_page,
        ):
            dialog.search()

        dialog.app.usenet_browser.open.assert_called_once_with()
        dialog.state.save_search.assert_called_once_with("example", "")
        search_page.assert_called_once_with(append=False)
        update_file_actions.assert_called_once_with()
        self.assertEqual(dialog._offset, 0)
        self.assertFalse(dialog._has_more)
        self.assertEqual(dialog._results, {})
        self.assertEqual(dialog._files, {})

    def test_results_can_be_sorted_and_filtered_locally(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        first = FinderResult("Alpha", "a", "TV", "2 GB", 2_000, "2026-01-01", {})
        second = FinderResult("Beta release", "b", "Movies", "1 GB", 1_000, "2026-02-01", {})
        dialog._result_records = [first, second]
        dialog._results = {}
        dialog._sort_column = "title"
        dialog._sort_reverse = False
        dialog.result_filter = SimpleNamespace(get=lambda: "beta")
        dialog.results = Mock()
        dialog.results.get_children.return_value = ()
        dialog.results.insert.side_effect = lambda _parent, _index, **_kwargs: "beta-row"

        dialog._refresh_result_rows()

        self.assertEqual(
            dialog.results.insert.call_args.kwargs["values"],
            ("Beta release", "Movies", "1 GB", "2026-02-01"),
        )
        self.assertEqual(dialog._results, {"beta-row": second})

        dialog.result_filter = SimpleNamespace(get=lambda: "")
        dialog._sort_results("size")
        self.assertEqual(
            [call.kwargs["values"][0] for call in dialog.results.insert.call_args_list[-2:]],
            ["Beta release", "Alpha"],
        )

    def test_selecting_a_result_starts_resolution_automatically(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        dialog._request_running = False
        dialog.results = Mock()
        dialog.results.selection.return_value = ("selected-result",)
        dialog._results = {}
        dialog.resolve_button = Mock()
        with patch.object(dialog, "resolve_selected") as resolve_selected:
            dialog._selection_changed(Mock())

        dialog.resolve_button.configure.assert_called_once_with(state="normal")
        resolve_selected.assert_called_once_with()


    def test_resolving_a_cached_result_skips_the_finder_request(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        dialog._request_running = False
        dialog.results = Mock()
        dialog.results.selection.return_value = ("selected-result",)
        result = SimpleNamespace(token="opaque-token")
        dialog._results = {"selected-result": result}
        package = FinderPackage("Example", (), {})
        dialog.query = SimpleNamespace(get=Mock(return_value="example"))
        dialog.category = SimpleNamespace(get=Mock(return_value="TV"))
        dialog.state = Mock()
        dialog.state.get_resolved_with_expiry.return_value = (package, 2000)
        dialog._result_page_offsets = {}
        dialog.files = Mock()
        dialog.status = Mock()
        dialog.client = Mock()
        with patch.object(dialog, "_show_package") as show_package:
            dialog.resolve_selected()

        dialog.state.get_resolved_with_expiry.assert_called_once_with("opaque-token")
        dialog.state.save_last_selected_result.assert_called_once_with(
            "example",
            "TV",
            "opaque-token",
            0,
        )
        show_package.assert_called_once_with(package)
        dialog.client.resolve.assert_not_called()
        dialog.status.set.assert_called_once_with(
            "Loaded saved resolution (0 file(s)); cache expires "
            f"{datetime.fromtimestamp(2000).strftime('%Y-%m-%d %H:%M')}."
        )

    def test_reopening_finder_restores_the_selected_result_and_cached_file_list(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        result = FinderResult("Example release", "opaque-token", "TV", "1 GB", 1, "date", {})
        package = FinderPackage("Example", (), {})
        dialog.state = Mock()
        dialog.state.load_last_selected_result.return_value = (
            "example",
            "TV",
            "opaque-token",
            0,
        )
        dialog.state.get_resolved_with_expiry.return_value = (package, 2000)
        dialog._results = {"result-row": result}
        dialog._offset = 15
        dialog.PAGE_SIZE = 15
        dialog.query = SimpleNamespace(get=lambda: "example")
        dialog.category = SimpleNamespace(get=lambda: "TV")
        dialog.results = Mock()
        dialog.status = Mock()
        dialog.app = SimpleNamespace(_log=Mock())
        dialog._restoring_result_token = None

        with patch.object(dialog, "_show_package") as show_package:
            dialog._restore_last_resolved("example", "TV")

        dialog.results.selection_set.assert_called_once_with("result-row")
        dialog.results.focus.assert_called_once_with("result-row")
        show_package.assert_called_once_with(package)
        dialog.status.set.assert_called_once_with(
            "Restored last resolved links (0 file(s)); "
            f"cache expires {datetime.fromtimestamp(2000).strftime('%Y-%m-%d %H:%M')}."
        )


class UsenetBrowserSessionTests(unittest.TestCase):
    def test_browser_connection_check_requires_a_deepbrid_tab(self) -> None:
        session = UsenetBrowserSession(Path("unused"))
        with (
            patch.object(session, "_debug_port", return_value=9222),
            patch.object(
                session,
                "_find_finder_target",
                return_value={"url": "https://www.deepbrid.com/login"},
            ),
        ):
            self.assertIn("connection is working", session.test_connection())

        with (
            patch.object(session, "_debug_port", return_value=None),
            patch.object(session, "_find_browser", return_value=None),
            self.assertRaisesRegex(UsenetFinderError, "Chrome or Microsoft Edge was not found"),
        ):
            session.test_connection()
        with (
            patch.object(session, "_debug_port", return_value=None),
            patch.object(session, "_find_browser", return_value="chrome"),
            self.assertRaisesRegex(UsenetFinderError, "not connected"),
        ):
            session.test_connection()

    def test_missing_browser_error_explains_requirement_and_install_options(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            session = UsenetBrowserSession(Path(temporary_directory))
            with (
                patch.object(session, "_debug_port", return_value=None),
                patch.object(session, "_close_legacy_automated_browser", return_value=False),
                patch.object(session, "_find_browser", return_value=None),
                patch("src.usenet_browser.sys.platform", "linux"),
                self.assertRaisesRegex(UsenetFinderError, "requires Google Chrome or Microsoft Edge") as error,
            ):
                session.open()
        self.assertIn("https://www.google.com/chrome/", str(error.exception))
        self.assertIn("https://www.microsoft.com/edge/download", str(error.exception))
        self.assertIn("Ubuntu or Debian", str(error.exception))

    def test_launch_uses_a_separate_profile_and_loopback_debugger_on_fixed_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory) / "browser-profile"
            session = UsenetBrowserSession(profile, browser_path="C:/Chrome/chrome.exe")
            with (
                patch.object(session, "_debug_port", return_value=None),
                patch.object(session, "_find_free_port", return_value=9225),
                patch("src.usenet_browser.subprocess.Popen") as popen,
            ):
                session.open()
            args = popen.call_args.args[0]
            self.assertIn(f"--user-data-dir={profile}", args)
            self.assertIn("--remote-debugging-port=9225", args)
            self.assertIn("--remote-debugging-address=127.0.0.1", args)
            self.assertEqual(args[-1], "https://www.deepbrid.com/login")
            self.assertEqual((profile / "DeepbridDevToolsPort").read_text(), "9225")

    def test_debug_port_comes_from_the_application_port_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            profile = Path(temporary_directory)
            (profile / "DeepbridDevToolsPort").write_text("9225", encoding="ascii")
            session = UsenetBrowserSession(profile)
            with patch(
                "src.usenet_browser.urllib.request.urlopen",
                return_value=FakeResponse(
                    200,
                    {},
                    b'{"Browser":"Chrome/154.0.0.0"}',
                ),
            ) as open_url:
                self.assertEqual(session._debug_port(), 9225)
            self.assertEqual(
                open_url.call_args.args[0],
                "http://127.0.0.1:9225/json/version",
            )

    def test_fetch_runs_inside_browser_and_never_exports_cookie_header(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            messages: list[str] = []
            session = UsenetBrowserSession(Path(temporary_directory), log=messages.append)
            target = {
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/one"
            }
            response = {
                "status": 200,
                "contentType": "application/json",
                "body": '{"results":[],"hasMore":false}',
            }
            with (
                patch.object(session, "_wait_for_debug_port", return_value=9222),
                patch.object(session, "_find_finder_target", return_value=target),
                patch.object(UsenetBrowserSession, "_evaluate", return_value=response) as evaluate,
            ):
                payload = session.request_json({"do": "search", "q": "test", "limit": "15"})

            self.assertEqual(payload, {"results": [], "hasMore": False})
            expression = evaluate.call_args.args[1]
            self.assertIn("credentials:'same-origin'", expression)
            self.assertNotIn("Cookie", expression)
            self.assertNotIn("token", "\n".join(messages))

    def test_cdp_evaluation_uses_page_runtime_and_closes_local_websocket(self) -> None:
        class FakeWebSocket:
            def __init__(self):
                self.sent: list[str] = []
                self.closed = False

            def send(self, payload: str) -> None:
                self.sent.append(payload)

            def recv(self) -> str:
                return json.dumps(
                    {
                        "id": 1,
                        "result": {
                            "result": {
                                "value": {
                                    "status": 200,
                                    "contentType": "application/json",
                                    "body": '{"results":[],"hasMore":false}',
                                }
                            }
                        },
                    }
                )

            def close(self) -> None:
                self.closed = True

        connection = FakeWebSocket()
        with patch(
            "src.usenet_browser.websocket.create_connection",
            return_value=connection,
        ) as connect:
            response = UsenetBrowserSession._evaluate(
                "ws://127.0.0.1:9222/devtools/page/test",
                "fetch('/usenet-finder')",
            )

        self.assertEqual(response["status"], 200)
        self.assertTrue(connection.closed)
        self.assertEqual(connect.call_args.kwargs["suppress_origin"], True)
        command = json.loads(connection.sent[0])
        self.assertEqual(command["method"], "Runtime.evaluate")
        self.assertTrue(command["params"]["awaitPromise"])

    def test_cdp_refuses_remote_debugging_hosts(self) -> None:
        with self.assertRaisesRegex(UsenetFinderError, "non-local"):
            UsenetBrowserSession._evaluate(
                "ws://attacker.example/devtools/page/test",
                "document.cookie",
            )


class DeepbridDiagnosticsTests(unittest.TestCase):
    def test_unsupported_filehost_response_is_not_retryable(self) -> None:
        response = FakeResponse(
            200,
            {},
            b'{"error":10,"message":"Filehoster not supported"}',
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(DeepbridError, "Filehoster not supported") as raised:
                DeepbridClient("test-key").generate_link("https://unsupported.example/file")

        self.assertFalse(raised.exception.retryable)

    def test_valid_api_key_account_response_is_accepted(self) -> None:
        response = FakeResponse(200, {}, b'{"type":"premium","error":0}')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response) as open_url:
            DeepbridClient("valid-test-key").validate_api_key()
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.deepbrid.com/api/v1/user")
        self.assertEqual(request.get_header("Authorization"), "Bearer valid-test-key")

    def test_api_key_validation_logs_redacted_request_and_result(self) -> None:
        secret = "sensitive-test-api-key"
        messages: list[str] = []
        response = FakeResponse(200, {}, b'{"type":"premium","error":0}')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            DeepbridClient(secret, log=messages.append).validate_api_key()

        self.assertTrue(any("curl -X GET" in message for message in messages))
        self.assertTrue(any("Authorization: Bearer <redacted>" in message for message in messages))
        self.assertIn("API-key validation response: HTTP 200.", messages)
        self.assertIn("API-key validation succeeded.", messages)
        self.assertNotIn(secret, "\n".join(messages))

    def test_invalid_api_key_is_reported_as_non_retryable_401(self) -> None:
        error = urllib.error.HTTPError(
            "https://www.deepbrid.com/api/v1/user",
            401,
            "Unauthorized",
            {},
            BytesIO(b'{"error":401,"message":"Authentication required."}'),
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(DeepbridError) as raised:
                DeepbridClient("bad-key").validate_api_key()
        self.assertEqual(raised.exception.status_code, 401)
        self.assertFalse(raised.exception.retryable)

    def test_fetch_hosts_accepts_current_string_array_and_legacy_status_map(self) -> None:
        current = FakeResponse(200, {}, b'["ddownload","mega"]')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=current):
            current_hosts = DeepbridClient.fetch_hosts()
        self.assertEqual(current_hosts, {"ddownload": "supported", "mega": "supported"})

        legacy = FakeResponse(200, {}, b'[{"mega.nz":"up"},{"ddownload.com":"down (today)"}]')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=legacy):
            legacy_hosts = DeepbridClient.fetch_hosts()
        self.assertEqual(legacy_hosts["mega.nz"], "up")
        self.assertEqual(legacy_hosts["ddownload.com"], "down (today)")

    def test_fetch_hosts_uses_api_key_for_live_status(self) -> None:
        response = FakeResponse(200, {}, b'[{"ddownload.com":"up"},{"1fichier.com":"down (today)"}]')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response) as open_url:
            hosts = DeepbridClient.fetch_hosts("test-api-key")

        self.assertEqual(hosts, {"ddownload.com": "up", "1fichier.com": "down (today)"})
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.deepbrid.com/api/v1/hosts")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-api-key")

    def test_fetch_hosts_preserves_http_401_status(self) -> None:
        error = urllib.error.HTTPError(
            "https://www.deepbrid.com/api/v1/hosts",
            401,
            "Unauthorized",
            {},
            BytesIO(b"API key rejected"),
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(DeepbridError) as raised:
                DeepbridClient.fetch_hosts("invalid-test-key")
        self.assertEqual(raised.exception.status_code, 401)

    def test_fetch_host_limits_formats_daily_links_and_bandwidth(self) -> None:
        response = FakeResponse(
            200,
            {},
            b'{"error":0,"reset":"daily","hosters":['
            b'{"domain":"ddownload.com","type":"links","limit":5,"used":3,"remaining":2},'
            b'{"domain":"mega.nz","type":"bandwidth","remaining":5368709120,"remaining_str":"5.00 GB"}]}',
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response) as open_url:
            limits = DeepbridClient.fetch_host_limits("test-key")
        self.assertEqual(limits["ddownload.com"], "2 / 5 links")
        self.assertEqual(limits["mega.nz"], "5.00 GB remaining")
        self.assertEqual(open_url.call_args.args[0].full_url, "https://www.deepbrid.com/api/v1/user/limits")

    def test_fetch_host_limits_preserves_json_401_status(self) -> None:
        response = FakeResponse(200, {}, b'{"error":401,"message":"Authentication required."}')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(DeepbridError, "Authentication required") as raised:
                DeepbridClient.fetch_host_limits("invalid-test-key")
        self.assertEqual(raised.exception.status_code, 401)

    def test_fetch_host_limits_accepts_live_hoster_field(self) -> None:
        response = FakeResponse(
            200,
            {},
            b'{"error":0,"hosters":[{"hoster":"ddownload.com","type":"bandwidth",'
            b'"remaining_str":"4.00 GB"}]}',
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            limits = DeepbridClient.fetch_host_limits("test-key")
        self.assertEqual(limits, {"ddownload.com": "4.00 GB remaining"})

    def test_fetch_host_limits_splits_comma_separated_aliases(self) -> None:
        response = FakeResponse(
            200,
            {},
            b'{"error":0,"hosters":[{"hoster":"ddl.to,ddownload.com","type":"bandwidth",'
            b'"remaining_str":"2.78 GB"}]}',
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            limits = DeepbridClient.fetch_host_limits("test-key")
        self.assertEqual(limits["ddownload.com"], "2.78 GB remaining")
        self.assertEqual(limits["ddl.to"], "2.78 GB remaining")

    def test_empty_host_response_is_an_explicit_error(self) -> None:
        empty = FakeResponse(200, {}, b"[]")
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=empty):
            with self.assertRaisesRegex(DeepbridError, "no hosts.*HTTP 200"):
                DeepbridClient.fetch_hosts()

    def test_http_error_logs_status_body_and_redacted_curl(self) -> None:
        api_key = "secret-test-key"
        body = b'{"error":401,"message":"Authentication required."}'
        error = urllib.error.HTTPError(
            "https://www.deepbrid.com/api/v1/generate/link",
            401,
            "Unauthorized",
            {},
            BytesIO(body),
        )
        messages: list[str] = []
        client = DeepbridClient(api_key, log=messages.append)

        with patch("src.deepbrid_client.urllib.request.urlopen", side_effect=error) as open_url:
            with self.assertRaisesRegex(DeepbridError, "HTTP 401") as raised:
                client.generate_link("https://example.com/file.zip")

        combined = "\n".join(messages) + "\n" + str(raised.exception)
        self.assertIn("HTTP 401", combined)
        self.assertIn("Authentication required", combined)
        self.assertIn("curl -X POST", combined)
        self.assertIn("Bearer <REDACTED>", combined)
        self.assertNotIn(api_key, combined)
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("User-agent"), APP_USER_AGENT)

    def test_successful_response_does_not_log_temporary_download_url(self) -> None:
        download_url = "https://premium-dl.deepbrid.com/d/private-token"
        body = (
            '{"error":0,"link":"'
            + download_url
            + '","filename":"file.zip"}'
        ).encode()
        response = FakeResponse(200, {}, body)
        messages: list[str] = []
        client = DeepbridClient("secret-test-key", log=messages.append)

        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            client.generate_link("https://example.com/file.zip")

        self.assertNotIn(download_url, "\n".join(messages))
        self.assertIn("<DOWNLOAD_LINK_REDACTED>", "\n".join(messages))

    def test_premium_link_generation_does_not_log_source_url(self) -> None:
        source_url = "https://usenet.example/download/123?ticket=private-value"
        error = urllib.error.HTTPError(
            "https://www.deepbrid.com/api/v1/generate/link",
            403,
            "Forbidden",
            {},
            BytesIO(
                json.dumps(
                    {
                        "message": f"Cannot process {source_url}",
                        "source": urllib.parse.quote_plus(source_url),
                    }
                ).encode()
            ),
        )
        messages: list[str] = []
        with patch("src.deepbrid_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(DeepbridError) as raised:
                DeepbridClient("secret-test-key", log=messages.append).generate_link(source_url)

        combined = "\n".join(messages) + str(raised.exception)
        self.assertNotIn(source_url, combined)
        self.assertNotIn(urllib.parse.quote_plus(source_url), combined)
        self.assertIn("<SOURCE_URL_REDACTED>", combined)

    def test_invalid_json_logs_http_status_and_body(self) -> None:
        response = FakeResponse(200, {}, b"upstream temporarily broken")
        messages: list[str] = []
        client = DeepbridClient("secret-test-key", log=messages.append)

        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(DeepbridError, "HTTP 200") as raised:
                client.generate_link("https://example.com/file.zip")

        combined = "\n".join(messages) + "\n" + str(raised.exception)
        self.assertIn("upstream temporarily broken", combined)
        self.assertIn("curl -X POST", combined)
        self.assertNotIn("secret-test-key", combined)

    def test_download_http_error_includes_redacted_curl_template(self) -> None:
        download_url = "https://premium-dl.deepbrid.com/d/private-token"
        error = urllib.error.HTTPError(
            download_url,
            503,
            "Unavailable",
            {},
            BytesIO(b"upstream offline"),
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            with patch("src.deepbrid_client.urllib.request.urlopen", side_effect=error):
                with self.assertRaisesRegex(DeepbridError, "HTTP 503") as raised:
                    DeepbridClient("secret-test-key").download(
                        download_url,
                        "file.bin",
                        Path(temporary_directory),
                        lambda: False,
                        lambda *_: None,
                    )

        message = str(raised.exception)
        self.assertIn("upstream offline", message)
        self.assertIn("curl -L", message)
        self.assertIn("<TEMPORARY_DOWNLOAD_URL_REDACTED>", message)
        self.assertNotIn("private-token", message)

    def test_cloudflare_owner_block_is_not_retryable(self) -> None:
        body = b'{"error_code":1010,"error_name":"browser_signature_banned","retryable":false,"owner_action_required":true}'
        error = urllib.error.HTTPError(
            "https://www.deepbrid.com/api/v1/generate/link",
            403,
            "Forbidden",
            {},
            BytesIO(body),
        )
        with patch("src.deepbrid_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(DeepbridError) as raised:
                DeepbridClient("test-key").generate_link("https://supported.example/file")

        self.assertFalse(raised.exception.retryable)
        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()