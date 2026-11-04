from tests._test_support import (
    DeepbridClient,
    DeepbridError,
    DownloaderApp,
    FILE_HOST_LINKS_DRAFT_PURPOSE,
    FILE_HOST_LINKS_DRAFT_SETTING,
    FakeResponse,
    Path,
    SecureStore,
    SimpleNamespace,
    closing,
    extract_supported_links,
    migrate_legacy_database,
    migrate_legacy_databases,
    patch,
    redact_log_urls,
    remove_legacy_storage,
    shutil,
    sqlite3,
    supported_link_status,
    tempfile,
    unittest,
)

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
            new_database = root / "src" / "database" / "deepbrid_downloader.sqlite3"
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

    def test_file_host_link_draft_is_persisted_encrypted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "queue.sqlite3"
            store = SecureStore(database_path)
            draft = "https://1fichier.com/?private-link"

            class FakeText:
                def get(self, *_args: str) -> str:
                    return draft

            app = object.__new__(DownloaderApp)
            app.root = SimpleNamespace()
            app.file_host_links_save_after = None
            app.file_host_links_input = FakeText()
            app.secure_store = store

            self.assertTrue(app._persist_file_host_links_draft())
            self.assertEqual(
                store.get_encrypted_setting(
                    FILE_HOST_LINKS_DRAFT_SETTING,
                    FILE_HOST_LINKS_DRAFT_PURPOSE,
                ),
                draft,
            )
            with closing(sqlite3.connect(database_path)) as connection:
                value = connection.execute(
                    "SELECT value FROM app_settings WHERE name = ?",
                    (FILE_HOST_LINKS_DRAFT_SETTING,),
                ).fetchone()[0]
            self.assertNotIn(b"private-link", bytes(value))

class ResumeTests(unittest.TestCase):
    def test_download_uses_server_filename_from_content_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            response = FakeResponse(
                200,
                {"Content-Length": "3", "Content-Disposition": 'attachment; filename="actual.bin"'},
                b"abc",
            )
            filenames: list[str] = []

            with patch(
                "src.deepbrid_client.urllib.request.urlopen",
                return_value=response,
            ):
                completed = DeepbridClient("unused").download(
                    "https://www.deepbrid.com/mytorrents?file=opaque",
                    "bundle-file-1",
                    output_dir,
                    lambda: False,
                    lambda *_: None,
                    on_filename=filenames.append,
                )

            self.assertTrue(completed)
            self.assertEqual(filenames, ["actual.bin"])
            self.assertEqual((output_dir / "actual.bin").read_bytes(), b"abc")

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
