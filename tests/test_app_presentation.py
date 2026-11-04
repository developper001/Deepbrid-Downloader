from tests._test_support import (
    API_KEY_DASHBOARD_URL,
    APP_DATA_DIR,
    APP_VERSION,
    AppConfiguration,
    COLUMN_ORDER,
    DATABASE_PATH,
    DEFAULT_COLUMNS,
    DownloaderApp,
    IMAGE_ROOT,
    LEGACY_DATABASE_PATH,
    LEGACY_DATABASE_PATHS,
    LEGACY_QUEUE_DATABASE_PATH,
    Mock,
    OUTPUT_DIR,
    Path,
    SecureStore,
    SimpleNamespace,
    _ConsoleStream,
    _parse_linked_image_badge,
    append_output_log_line,
    default_download_directory,
    format_bytes,
    format_duration,
    format_item_eta,
    full_file_path,
    patch,
    progress_indicator_values,
    size_verification_label,
    smooth_rate,
    tempfile,
    unittest,
    validate_api_key,
    validate_output_folder,
)

class AppPresentationTests(unittest.TestCase):
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
        app.store = Mock()
        app.store.add.return_value = False
        app.root = object()
        app.status_text = SimpleNamespace(set=Mock())
        app._log = Mock()
        app._refresh_rows = Mock()
        app._save_visible_queue_order = Mock()

        with patch("src.app.messagebox.showwarning") as showwarning:
            app._add_link("https://example.com/file")

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
        image_paths = DownloaderApp.readme_image_paths()
        self.assertIn("src/screenshots/dashboard-main-window.png", image_paths)
        repository_root = Path(__file__).resolve().parent.parent
        for image_path in image_paths:
            if image_path.startswith("src/"):
                with self.subTest(image_path=image_path):
                    self.assertTrue((repository_root / image_path).is_file())

    def test_release_notes_content_is_available(self) -> None:
        release_notes = DownloaderApp.release_notes_text()
        self.assertIn("# Changelog", release_notes)
        self.assertIn("## [Unreleased]", release_notes)
        self.assertIn(f"## [v{APP_VERSION}]", release_notes)

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
        self.assertEqual(LEGACY_DATABASE_PATH.parent.name, "database")
        self.assertEqual(
            LEGACY_DATABASE_PATHS[1],
            LEGACY_DATABASE_PATH.parent.parent / "deepbrid_downloader.sqlite3",
        )
        self.assertEqual(IMAGE_ROOT.name, "img")
        self.assertTrue(IMAGE_ROOT.is_dir())

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
        self.assertIn("extension", COLUMN_ORDER)
        self.assertIn("file_path", COLUMN_ORDER)
        self.assertNotIn("file_path", DEFAULT_COLUMNS)
        self.assertEqual(DEFAULT_COLUMNS[:3], ("filename", "extension", "host"))
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
        self.assertEqual(
            full_file_path(Path.cwd(), "Bravo.zip"),
            str((Path.cwd() / "Bravo.zip").resolve()),
        )
        app = object.__new__(DownloaderApp)
        app.item_speeds = {1: 100}
        app.item_status_messages = {1: "Link retry 2/5 in 3s"}
        app.output_dir = Path.cwd()
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
        self.assertEqual(
            app._sort_value(item, "file_path"),
            full_file_path(Path.cwd(), "Bravo.zip").casefold(),
        )
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
