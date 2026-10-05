from __future__ import annotations

import hashlib
import json
import tempfile
import sqlite3
import shutil
import unittest
import uuid
import urllib.error
from contextlib import closing
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.deepbrid_client import APP_USER_AGENT, DeepbridClient, DeepbridError
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
    DownloaderApp,
    LEGACY_DATABASE_PATH,
    LEGACY_QUEUE_DATABASE_PATH,
    OUTPUT_DIR,
    _ConsoleStream,
    default_download_directory,
    _filter_and_sort_host_rows,
    _parse_linked_image_badge,
    format_bytes,
    format_duration,
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
from src.secure_store import SecureStore
from src.single_instance import acquire_single_instance
from src.update_manager import download_update_asset


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
            store.update(item.id, deepbrid_link=generated_url)

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

        self.assertEqual(events, [("startup_update", release, None)])

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

        self.assertIn("newer than the latest published release", events[0][6])
        self.assertIsNone(events[0][7])
        self.assertIsNone(events[0][8])

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

        self.assertEqual(events, [("hosts_error", "API key rejected", 401)])

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
            [("hosts", {"example.com": "up"}, {}, "API key rejected", 401)],
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
        app.dark_theme = app._load_theme_preference()
        self.assertTrue(app.dark_theme)

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
        self.assertEqual(format_bytes(1024), "1.0 KB")
        self.assertEqual(format_bytes(None), "Unknown")
        self.assertEqual(format_duration(3661), "1h 1m")
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


class ResumeTests(unittest.TestCase):
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


class DeepbridDiagnosticsTests(unittest.TestCase):
    def test_valid_api_key_account_response_is_accepted(self) -> None:
        response = FakeResponse(200, {}, b'{"type":"premium","error":0}')
        with patch("src.deepbrid_client.urllib.request.urlopen", return_value=response) as open_url:
            DeepbridClient("valid-test-key").validate_api_key()
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://www.deepbrid.com/api/v1/user")
        self.assertEqual(request.get_header("Authorization"), "Bearer valid-test-key")

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