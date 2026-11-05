from tests._test_support import (
    ApiKeyCheckEvent,
    AppConfiguration,
    CACHE_DURATION_SETTING,
    COLUMN_ORDER,
    DEFAULT_CACHE_DURATION_HOURS,
    DEFAULT_COLUMNS,
    DeepbridError,
    DownloaderApp,
    FILE_HOST_LOGO_DARK_PATH,
    FILE_HOST_LOGO_LIGHT_PATH,
    FakeResponse,
    GITHUB_LATEST_RELEASE_URL,
    HIDE_AUXILIARY_FILES_SETTING,
    LatestRelease,
    MACOS_LOGIN_ITEMS_URL,
    MagicMock,
    Mock,
    Path,
    QueueStore,
    RESETTABLE_SETTINGS,
    ReleaseAsset,
    SecureStore,
    SimpleNamespace,
    StartupApiKeyProblemEvent,
    StartupConfigurationError,
    StartupManager,
    StartupUpdateEvent,
    TORRENT_LOGO_DARK_PATH,
    TORRENT_LOGO_LIGHT_PATH,
    USENET_LOGO_DARK_PATH,
    USENET_LOGO_LIGHT_PATH,
    USENET_AUTO_OPEN_BROWSER_SETTING,
    UpdateCheckError,
    UpdateCheckEvent,
    UpdateInstallError,
    _setting_is_enabled,
    acquire_single_instance,
    apply_update_from_args,
    closing,
    download_update_asset,
    fetch_latest_release,
    fetch_latest_release_details,
    hashlib,
    is_newer_version,
    json,
    os,
    patch,
    platform_release_asset,
    reset_application_settings,
    sqlite3,
    struct,
    tempfile,
    threading,
    tk,
    unittest,
    uuid,
)

class AppStartupUpdatesTests(unittest.TestCase):
    def test_copy_filename_copies_selected_names_in_display_order(self) -> None:
        app = object.__new__(DownloaderApp)
        app._selected_items = Mock(
            return_value=[
                SimpleNamespace(filename="first file.txt", url="https://example.test/first"),
                SimpleNamespace(filename=None, url="https://example.test/second%20file.bin?token=x"),
            ]
        )
        app.root = Mock()
        app.status_text = Mock()
        app._log = Mock()

        app._copy_filename()

        app.root.clipboard_clear.assert_called_once_with()
        app.root.clipboard_append.assert_called_once_with("first file.txt\nsecond%20file.bin")
        app.status_text.set.assert_called_once_with("Filenames copied")
        app._log.assert_called_once_with("Filename(s) copied to clipboard.")

    def test_action_dialog_logos_have_light_and_dark_high_resolution_assets(self) -> None:
        for logo_path in (
            FILE_HOST_LOGO_LIGHT_PATH,
            FILE_HOST_LOGO_DARK_PATH,
            TORRENT_LOGO_LIGHT_PATH,
            TORRENT_LOGO_DARK_PATH,
        ):
            contents = logo_path.read_bytes()

            self.assertEqual(contents[:8], b"\x89PNG\r\n\x1a\n")
            self.assertGreaterEqual(struct.unpack(">I", contents[16:20])[0], 200)
            self.assertGreaterEqual(struct.unpack(">I", contents[20:24])[0], 40)

    def test_usenet_modal_logo_has_light_and_dark_assets(self) -> None:
        for logo_path in (USENET_LOGO_LIGHT_PATH, USENET_LOGO_DARK_PATH):
            contents = logo_path.read_bytes()

            self.assertEqual(contents[:8], b"\x89PNG\r\n\x1a\n")
            self.assertGreaterEqual(struct.unpack(">I", contents[16:20])[0], 40)
            self.assertGreaterEqual(struct.unpack(">I", contents[20:24])[0], 40)

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
            self.assertIn(f"Path={manager.project_root}", entry)
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

            self.assertIn(str(pythonw.resolve()), command)
            self.assertIn(str(manager.project_root / "launcher.py"), command)

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

    def test_usenet_auxiliary_file_filter_setting_is_persisted(self) -> None:
        saved_settings: dict[str, str] = {}
        app = object.__new__(DownloaderApp)
        app.secure_store = SimpleNamespace(
            set_setting=lambda name, value: saved_settings.__setitem__(name, value)
        )
        finder_dialog = SimpleNamespace(
            dialog=SimpleNamespace(winfo_exists=lambda: True),
            _set_auxiliary_files_hidden=Mock(),
        )
        app.usenet_finder_dialog = finder_dialog

        app._set_usenet_finder_hide_auxiliary_files(True)

        self.assertTrue(app.usenet_finder_hide_auxiliary_files)
        self.assertEqual(saved_settings[HIDE_AUXILIARY_FILES_SETTING], "true")
        finder_dialog._set_auxiliary_files_hidden.assert_called_once_with(True)

    def test_auto_open_usenet_browser_setting_is_persisted(self) -> None:
        saved_settings: dict[str, str] = {}
        app = object.__new__(DownloaderApp)
        app.secure_store = SimpleNamespace(
            set_setting=lambda name, value: saved_settings.__setitem__(name, value)
        )

        app._set_usenet_auto_open_browser_for_link_renewal(True)

        self.assertTrue(app.usenet_auto_open_browser_for_link_renewal)
        self.assertEqual(saved_settings[USENET_AUTO_OPEN_BROWSER_SETTING], "true")

        app._set_usenet_auto_open_browser_for_link_renewal(False)

        self.assertFalse(app.usenet_auto_open_browser_for_link_renewal)
        self.assertEqual(saved_settings[USENET_AUTO_OPEN_BROWSER_SETTING], "false")

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
            app._load_update_status(None, None, None, None)

        self.assertIn("newer than the latest published release", events[0].message)
        self.assertIsNone(events[0].release)
        self.assertIsNone(events[0].asset)

    def test_manual_update_check_uses_startup_update_prompt(self) -> None:
        release = LatestRelease(
            "v0.5.2",
            "https://github.com/developper001/Deepbrid-Downloader/releases/tag/v0.5.2",
            (),
        )
        asset = ReleaseAsset(
            "DeepbridDownloader-Windows.exe",
            "https://github.com/developper001/Deepbrid-Downloader/releases/download/v0.5.2/DeepbridDownloader-Windows.exe",
            1,
            "a" * 64,
        )
        popup = Mock()
        popup.winfo_exists.return_value = True
        update_status = Mock()
        check_button = Mock()
        update_progress = Mock()
        app = object.__new__(DownloaderApp)
        app._prompt_startup_update = Mock()
        event = UpdateCheckEvent(
            popup,
            update_status,
            check_button,
            update_progress,
            "Version v0.5.2 is available.",
            release,
            asset,
        )

        app._handle_update_check(event)

        check_button.configure.assert_called_once_with(state="normal")
        update_status.set.assert_called_once_with(event.message)
        update_progress.pack_forget.assert_called_once_with()
        app._prompt_startup_update.assert_called_once_with(release, asset)

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

    def test_update_install_restores_original_when_replacement_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target_path = directory / "DeepbridDownloader.exe"
            staged_path = directory / ".deepbrid-update-test.download"
            helper_path = directory / ".deepbrid-update-helper-test.exe"
            target_path.write_bytes(b"old application")
            staged_path.write_bytes(b"new application")
            arguments = [
                "--apply-update",
                str(staged_path),
                str(target_path),
                "123",
                str(helper_path),
            ]
            real_replace = os.replace
            replace_calls = 0

            def fail_first_replace(source: str | os.PathLike, destination: str | os.PathLike) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    raise PermissionError("access denied")
                real_replace(source, destination)

            with (
                patch("src.update_manager.sys.executable", str(helper_path)),
                patch("src.update_manager._wait_for_process"),
                patch("src.update_manager.os.replace", side_effect=fail_first_replace) as replace,
                patch("src.update_manager.subprocess.Popen"),
                self.assertRaisesRegex(
                    UpdateInstallError,
                    "Could not install the update: access denied",
                ),
            ):
                apply_update_from_args(arguments)

            self.assertEqual(replace.call_count, 2)
            self.assertEqual(target_path.read_bytes(), b"old application")
            self.assertEqual(staged_path.read_bytes(), b"new application")
            self.assertEqual(list(directory.glob(".deepbrid-old-*")), [])

    def test_update_install_restores_original_if_restart_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            target_path = directory / "DeepbridDownloader.exe"
            staged_path = directory / ".deepbrid-update-test.download"
            helper_path = directory / ".deepbrid-update-helper-test.exe"
            target_path.write_bytes(b"old application")
            staged_path.write_bytes(b"new application")
            arguments = [
                "--apply-update",
                str(staged_path),
                str(target_path),
                "123",
                str(helper_path),
            ]

            with (
                patch("src.update_manager.sys.executable", str(helper_path)),
                patch("src.update_manager._wait_for_process"),
                patch("src.update_manager.subprocess.Popen", side_effect=OSError("restart failed")),
                self.assertRaisesRegex(UpdateInstallError, "restart failed"),
            ):
                apply_update_from_args(arguments)

            self.assertEqual(target_path.read_bytes(), b"old application")
            self.assertFalse(staged_path.exists())
            self.assertEqual(list(directory.glob(".deepbrid-old-*")), [])

class AppConfigurationTests(unittest.TestCase):
    def test_reset_application_settings_removes_preferences_but_keeps_user_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "settings.sqlite3")
            for name in RESETTABLE_SETTINGS:
                store.set_setting(name, "custom value")
            store.set_setting("usenet_finder_search_history", "saved history")
            store.set_setting("file_host_links_draft", "saved draft")

            reset_application_settings(store)

            self.assertTrue(
                all(store.get_setting(name) is None for name in RESETTABLE_SETTINGS)
            )
            self.assertEqual(
                store.get_setting("usenet_finder_search_history"),
                "saved history",
            )
            self.assertEqual(store.get_setting("file_host_links_draft"), "saved draft")

    def test_reset_settings_updates_live_application_preferences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "settings.sqlite3")
            for name in RESETTABLE_SETTINGS:
                store.set_setting(name, "custom value")
            app = object.__new__(DownloaderApp)
            app.worker = None
            app.startup_manager = SimpleNamespace(supports_toggle=False)
            app.usenet_finder_dialog = None
            app.hosts_popup = None
            app.secure_store = store
            app.auto_check_updates = Mock()
            app.auto_start_downloads = Mock()
            app.output_dir_text = Mock()
            app.append_output_log_path = Mock()
            app.table = Mock()
            app.theme_button = Mock()
            app._apply_theme = Mock()
            app._refresh_rows = Mock()
            app._output_log_error_reported = True

            with patch(
                "src.app.default_download_directory",
                return_value=Path(temporary_directory) / "downloads",
            ):
                result = app._reset_settings_to_defaults()

            app.auto_check_updates.set.assert_called_once_with(True)
            app.auto_start_downloads.set.assert_called_once_with(True)
            self.assertEqual(
                app.output_dir,
                Path(temporary_directory) / "downloads",
            )
            self.assertEqual(app.usenet_finder_cache_duration_hours, DEFAULT_CACHE_DURATION_HOURS)
            self.assertTrue(app.usenet_finder_hide_auxiliary_files)
            self.assertTrue(app.usenet_auto_open_browser_for_link_renewal)
            self.assertFalse(app.append_output_log_enabled)
            self.assertFalse(app.dark_theme)
            app._apply_theme.assert_called_once_with()
            app._refresh_rows.assert_called_once_with()
            self.assertIn("API key", result)

    def test_reset_settings_is_blocked_while_download_queue_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "settings.sqlite3")
            store.set_setting("dark_theme", "true")
            app = object.__new__(DownloaderApp)
            app.worker = SimpleNamespace(is_alive=lambda: True)

            with self.assertRaisesRegex(RuntimeError, "Stop the download queue"):
                app._reset_settings_to_defaults()

            self.assertEqual(store.get_setting("dark_theme"), "true")

    def test_legacy_default_columns_are_migrated_to_show_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "settings.sqlite3")
            store.set_setting(
                "visible_columns",
                json.dumps(
                    [
                        "filename",
                        "host",
                        "size",
                        "progress",
                        "time_remaining",
                        "eta",
                        "verification",
                    ]
                ),
            )
            store.set_setting("progress_column_initialized", "true")
            store.set_setting("progress_percentage_column_initialized", "true")

            config = AppConfiguration.load(
                store,
                COLUMN_ORDER,
                DEFAULT_COLUMNS,
                lambda: Path(temporary_directory),
                lambda: None,
                lambda: None,
                lambda: Path(temporary_directory) / "deepbrid-output.log",
            )

            self.assertEqual(
                config.visible_columns[:3],
                ["filename", "extension", "host"],
            )
            self.assertEqual(
                json.loads(store.get_setting("visible_columns") or "[]"),
                config.visible_columns,
            )

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
            self.assertTrue(config.usenet_finder_hide_auxiliary_files)
            self.assertTrue(config.usenet_auto_open_browser_for_link_renewal)
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
            self.assertEqual(config.column_widths["filename"], 250)

            store.set_setting("auto_start_downloads_on_startup", "false")
            store.set_setting("dark_theme", "true")
            store.set_setting("hosts", json.dumps({"example.com": "up"}))
            store.set_setting("output_directory", str(downloads / "custom"))
            store.set_setting("visible_columns", json.dumps(["filename", "progress"]))
            store.set_setting(
                "column_widths",
                json.dumps({"filename": 500, "link": 800}),
            )
            store.set_setting(CACHE_DURATION_SETTING, "48")
            store.set_setting(HIDE_AUXILIARY_FILES_SETTING, "false")
            store.set_setting(USENET_AUTO_OPEN_BROWSER_SETTING, "true")
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
            self.assertEqual(config.column_widths["filename"], 500)
            self.assertEqual(config.column_widths["link"], 800)
            self.assertEqual(config.usenet_finder_cache_duration_hours, 48)
            self.assertFalse(config.usenet_finder_hide_auxiliary_files)
            self.assertTrue(config.usenet_auto_open_browser_for_link_renewal)
            self.assertTrue(config.append_output_log_enabled)
            self.assertEqual(config.append_output_log_path, log_path)

class ApplicationShutdownTests(unittest.TestCase):
    def test_shutdown_prunes_tcl_commands_already_removed_by_interpreter(self) -> None:
        interpreter = tk.Tcl()
        live_command = interpreter.register(lambda: None)
        stale_command = interpreter.register(lambda: None)
        interpreter.tk.deletecommand(stale_command)
        app = object.__new__(DownloaderApp)
        app.root = SimpleNamespace(
            _tclCommands=[live_command, stale_command],
            tk=interpreter.tk,
        )
        app._log = Mock()

        app._prune_deleted_tcl_commands()

        self.assertEqual(app.root._tclCommands, [live_command])
        app._log.assert_called_once_with(
            "Skipped cleanup of 1 Tcl callback(s) already removed during shutdown."
        )
        interpreter.tk.deletecommand(live_command)

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
