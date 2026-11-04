from tests._test_support import (
    CACHE_TTL_SECONDS,
    FinderFile,
    FinderPackage,
    FinderResult,
    FinderSearchPage,
    Path,
    SecureStore,
    UsenetFinderState,
    tempfile,
    unittest,
    valid_cache_duration_hours,
)

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

    def test_search_history_entry_can_be_deleted_without_clearing_last_search(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = SecureStore(Path(temporary_directory) / "queue.sqlite3")
            state = UsenetFinderState(store, clock=lambda: 1000)
            state.save_search("example", "TV")
            state.save_search("example", "Movies")

            self.assertTrue(state.delete_search_history_entry("example", "TV"))
            self.assertFalse(state.delete_search_history_entry("missing", ""))

            self.assertEqual(state.load_search_history(), [("example", "Movies")])
            self.assertEqual(state.load_search(), ("example", "Movies"))

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
