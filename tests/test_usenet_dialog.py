from tests._test_support import (
    FinderFile,
    FinderPackage,
    FinderResult,
    FinderSearchPage,
    Mock,
    Path,
    SimpleNamespace,
    UsenetFinderClient,
    UsenetFinderDialog,
    UsenetFinderError,
    _configure_horizontal_scrollbar,
    _read_column_widths,
    call,
    datetime,
    file_size_bytes,
    json,
    patch,
    struct,
    unittest,
)

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

    def test_premium_required_finder_response_prompts_user_to_sign_in(self) -> None:
        from src.usenet_finder import parse_browser_response

        with self.assertRaisesRegex(
            UsenetFinderError,
            "Sign in to your Deepbrid account in the Chrome window",
        ):
            parse_browser_response(
                200,
                "application/json",
                json.dumps({"error": "Premium required"}),
            )

        with self.assertRaisesRegex(UsenetFinderError, "^Unsupported operation$"):
            parse_browser_response(
                200,
                "application/json",
                json.dumps({"error": "Unsupported operation"}),
            )

class UsenetFinderDialogTests(unittest.TestCase):
    def test_row_context_handlers_allow_heading_menu_to_run(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        dialog.results = Mock()
        dialog.results.identify_region.return_value = "heading"
        dialog.files = Mock()
        dialog.files.identify_region.return_value = "heading"

        self.assertIsNone(dialog._show_result_context_menu(SimpleNamespace(x=1, y=1)))
        self.assertIsNone(dialog._show_file_context_menu(SimpleNamespace(x=1, y=1)))

    def test_column_widths_restore_saved_values_and_default_invalid_values(self) -> None:
        store = Mock()
        store.get_setting.return_value = json.dumps(
            {"title": 720, "category": 0, "size": "wide", "unused": 300}
        )

        widths = _read_column_widths(
            store,
            "usenet_search_column_widths",
            ("title", "category", "size", "date"),
            {"title": 470, "category": 100, "size": 100, "date": 150},
        )

        self.assertEqual(
            widths,
            {"title": 720, "category": 100, "size": 100, "date": 150},
        )

    def test_column_widths_are_saved_for_both_finder_tables(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        dialog.results = Mock()
        dialog.files = Mock()
        dialog.app = SimpleNamespace(secure_store=Mock(), _log=Mock())
        dialog.results.column.side_effect = lambda column, _option: {
            "title": "700",
            "category": "130",
            "size": "110",
            "date": "160",
        }[column]
        dialog.files.column.side_effect = lambda column, _option: {
            "name": "640",
            "extension": "110",
            "size": "120",
            "availability": "170",
        }[column]

        dialog._save_column_widths("search")
        dialog._save_column_widths("files")

        store = dialog.app.secure_store
        store.set_setting.assert_any_call(
            "usenet_search_column_widths",
            json.dumps({"title": 700, "category": 130, "size": 110, "date": 160}),
        )
        store.set_setting.assert_any_call(
            "usenet_file_column_widths",
            json.dumps(
                {"name": 640, "extension": 110, "size": 120, "availability": 170}
            ),
        )

    def test_horizontal_scrollbar_tracks_column_overflow(self) -> None:
        table = Mock()
        scrollbar = Mock()

        _configure_horizontal_scrollbar(table, scrollbar)
        update_scrollbar = table.configure.call_args.kwargs["xscrollcommand"]

        update_scrollbar("0.1", "0.9")
        scrollbar.set.assert_called_once_with("0.1", "0.9")
        scrollbar.grid.assert_called_once_with()
        scrollbar.grid_remove.assert_not_called()

        scrollbar.reset_mock()
        update_scrollbar("0", "1")
        scrollbar.set.assert_called_once_with("0", "1")
        scrollbar.grid_remove.assert_called_once_with()
        scrollbar.grid.assert_not_called()

    def test_resolved_file_size_parser_supports_decimal_and_binary_units(self) -> None:
        self.assertEqual(file_size_bytes("1.1 GB"), 1_100_000_000)
        self.assertEqual(file_size_bytes("1.1 GiB"), round(1.1 * 1024**3))
        self.assertEqual(file_size_bytes("17.88 KB"), 17_880)
        self.assertIsNone(file_size_bytes("Unknown"))

    def test_ctrl_a_selects_all_resolved_files(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        dialog.files = Mock()
        dialog.files.get_children.return_value = ("first", "second")

        self.assertEqual(dialog._select_all_files(), "break")

        dialog.files.selection_set.assert_called_once_with("first", "second")
        dialog.files.focus.assert_called_once_with("first")

    def test_clearing_search_query_clears_category(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        dialog.query = SimpleNamespace(get=lambda: "   ")
        dialog.category = Mock()

        dialog._search_query_changed()

        dialog.category.set.assert_called_once_with("")

    def test_delete_key_removes_selected_search_history_entry(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        dialog.query_entry = Mock()
        dialog.query_entry.current.return_value = 1
        dialog.query_entry.tk = Mock()
        dialog.query_history = [("example", "TV"), ("example", "Movies")]
        dialog.state = Mock()
        dialog.state.delete_search_history_entry.return_value = True
        dialog.app = SimpleNamespace(_log=Mock())
        dialog.status = Mock()
        dialog.query = Mock()
        dialog.category = Mock()

        with patch.object(dialog, "_update_search_history_options") as update_options:
            self.assertEqual(dialog._delete_selected_search_history_entry(), "break")

        dialog.state.delete_search_history_entry.assert_called_once_with("example", "Movies")
        update_options.assert_called_once_with()
        dialog.query.set.assert_called_once_with("")
        dialog.category.set.assert_called_once_with("")
        dialog.query_entry.tk.call.assert_called_once_with(
            "ttk::combobox::Unpost",
            dialog.query_entry,
        )
        dialog.status.set.assert_called_once_with(
            "Usenet Finder search history entry deleted."
        )

    def test_delete_key_uses_highlighted_history_row_in_open_dropdown(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        dialog.query_entry = Mock()
        dialog.query_entry.__str__ = Mock(return_value="combo")
        dialog.query_entry.current.return_value = -1
        dialog.query_entry.tk = Mock()
        dialog.query_history = [
            ("example", "TV"),
            ("example", "Movies"),
            ("example", "Music"),
        ]
        dialog.dialog = Mock()
        dialog.dialog.tk.call.return_value = "2"
        dialog.dialog.tk.splitlist.return_value = ("2",)
        dialog.state = Mock()
        dialog.state.delete_search_history_entry.return_value = True
        dialog.app = SimpleNamespace(_log=Mock())
        dialog.status = Mock()
        dialog.query = Mock()
        dialog.category = Mock()
        event = SimpleNamespace(widget="combo.popdown.f.l")

        with patch.object(dialog, "_update_search_history_options"):
            self.assertEqual(
                dialog._delete_selected_search_history_entry(event),
                "break",
            )

        dialog.dialog.tk.call.assert_called_once_with(
            "combo.popdown.f.l",
            "curselection",
        )
        dialog.state.delete_search_history_entry.assert_called_once_with(
            "example",
            "Music",
        )
        dialog.query_entry.tk.call.assert_called_once_with(
            "ttk::combobox::Unpost",
            dialog.query_entry,
        )

    def test_usenet_column_layout_defaults_and_saved_layouts(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        dialog.app = SimpleNamespace(secure_store=Mock())
        dialog.app.secure_store.get_setting.return_value = None

        search_order, search_visible = dialog._load_column_layout(
            "search-order",
            "search-visible",
            ("title", "category", "size", "date"),
            ("title", "category", "size", "date"),
        )
        file_order, file_visible = dialog._load_column_layout(
            "file-order",
            "file-visible",
            ("name", "extension", "size", "availability"),
            ("name", "extension", "size", "availability"),
        )
        self.assertEqual(search_order, ["title", "category", "size", "date"])
        self.assertEqual(search_visible, search_order)
        self.assertEqual(file_order, ["name", "extension", "size", "availability"])
        self.assertEqual(file_visible, file_order)

        dialog.app.secure_store.get_setting.side_effect = lambda name: {
            "saved-order": '["date", "title"]',
            "saved-visible": '["date", "size"]',
        }.get(name)
        order, visible = dialog._load_column_layout(
            "saved-order",
            "saved-visible",
            ("title", "category", "size", "date"),
            ("title", "category", "size", "date"),
        )
        self.assertEqual(order, ["date", "title", "category", "size"])
        self.assertEqual(visible, ["date", "size"])

    def test_resolved_files_show_extension_column_value(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        file = FinderFile("episode.MKV", "https://example.test/file", "1 GB", True, None, {})
        dialog.files = Mock()
        dialog.files.get_children.return_value = ()
        dialog.files.insert.return_value = "episode"
        dialog._files = {}
        dialog._finder_file_indices = {}
        dialog._file_order = []
        dialog._file_sort_column = "name"
        dialog._file_sort_reverse = False
        dialog._hide_auxiliary_files = False
        dialog.status = Mock()
        dialog._update_file_actions = Mock()

        dialog._show_package(FinderPackage("Example", (file,), {}))

        self.assertEqual(
            dialog.files.insert.call_args.kwargs["values"],
            ("episode.MKV", ".mkv", "1 GB", "Accessible"),
        )
        self.assertEqual(dialog._files, {"episode": file})

    def test_resolved_files_can_be_sorted_by_name_and_size(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        first = FinderFile("Beta.mkv", "https://example.test/b", "2 GB", True, None, {})
        second = FinderFile("Alpha.mkv", "https://example.test/a", "500 MB", True, None, {})
        third = FinderFile("Unknown.mkv", "https://example.test/u", "Unknown", True, None, {})
        dialog.files = Mock()
        dialog.files.get_children.return_value = ("first", "second", "third")
        dialog._files = {"first": first, "second": second, "third": third}
        dialog._file_order = ["first", "second", "third"]
        dialog._file_sort_column = "name"
        dialog._file_sort_reverse = False
        dialog._hide_auxiliary_files = False

        dialog._sort_files("name")
        dialog.files.set_children.assert_called_with("", "third", "first", "second")

        dialog._sort_files("size")
        dialog.files.set_children.assert_called_with("", "second", "first", "third")

        dialog._sort_files("size")
        dialog.files.set_children.assert_called_with("", "first", "second", "third")

        inaccessible = FinderFile(
            "Locked.mkv",
            "",
            "1 GB",
            True,
            "missing_volumes",
            {},
        )
        dialog._files["locked"] = inaccessible
        dialog._file_order = ["locked", "first"]
        dialog._sort_files("availability")
        dialog.files.set_children.assert_called_with("", "first", "locked")

    def test_add_all_accessible_skips_hidden_auxiliary_files(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        video = FinderFile("episode.mkv", "https://example.test/video", "1 GB", True, None, {})
        parity = FinderFile("episode.PAR2", "https://example.test/par2", "2 MB", False, None, {})
        info = FinderFile("episode.NFO", "https://example.test/nfo", "2 KB", False, None, {})
        inaccessible = FinderFile(
            "missing.mkv",
            "",
            "Unknown",
            True,
            "No link available",
            {},
        )
        dialog._files = {
            "video": video,
            "parity": parity,
            "info": info,
            "inaccessible": inaccessible,
        }
        dialog._hide_auxiliary_files = True
        dialog._resolved_result_token = None
        dialog.app = SimpleNamespace(
            add_usenet_links=Mock(return_value=(1, 0, 0)),
        )
        dialog.status = Mock()

        dialog.add_all_accessible_files()

        dialog.app.add_usenet_links.assert_called_once_with(
            [("https://example.test/video", "episode.mkv", 1_000_000_000)]
        )

    def test_add_all_accessible_includes_auxiliary_files_when_visible(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        video = FinderFile("episode.mkv", "https://example.test/video", "1 GB", True, None, {})
        info = FinderFile("episode.NFO", "https://example.test/nfo", "2 KB", False, None, {})
        dialog._files = {"video": video, "info": info}
        dialog._hide_auxiliary_files = False
        dialog._resolved_result_token = None
        dialog.app = SimpleNamespace(
            add_usenet_links=Mock(return_value=(2, 0, 0)),
        )
        dialog.status = Mock()

        dialog.add_all_accessible_files()

        dialog.app.add_usenet_links.assert_called_once_with(
            [
                ("https://example.test/video", "episode.mkv", 1_000_000_000),
                ("https://example.test/nfo", "episode.NFO", 2_000),
            ]
        )

    def test_auxiliary_file_filter_hides_par2_and_nfo_and_can_be_reversed(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        regular = FinderFile("episode.mkv", "https://example.test/video", "1 GB", True, None, {})
        parity = FinderFile(
            "episode.vol00+01.PAR2",
            "https://example.test/parity",
            "20 MB",
            False,
            None,
            {},
        )
        info = FinderFile("episode.NFO", "https://example.test/info", "2 KB", True, None, {})
        dialog.files = Mock()
        dialog.files.get_children.return_value = ()
        dialog.files.insert.side_effect = ("video-row", "par2-row", "nfo-row")
        dialog.files.selection.return_value = ("par2-row",)
        dialog._files = {}
        dialog._finder_file_indices = {}
        dialog._file_order = []
        dialog._file_sort_column = "name"
        dialog._file_sort_reverse = False
        dialog._hide_auxiliary_files = False
        dialog.auxiliary_files_filter_button = Mock()
        dialog._update_file_actions = Mock()
        dialog.status = Mock()
        dialog.app = SimpleNamespace(
            _set_usenet_finder_hide_auxiliary_files=dialog._set_auxiliary_files_hidden,
            _log=Mock(),
        )

        dialog._show_package(FinderPackage("Example", (regular, parity, info), {}))
        dialog._toggle_auxiliary_files_filter()

        dialog.files.set_children.assert_called_with("", "video-row")
        self.assertEqual(set(dialog._files), {"video-row", "par2-row", "nfo-row"})
        dialog.files.selection_remove.assert_called_once_with("par2-row")
        dialog.auxiliary_files_filter_button.configure.assert_called_with(
            text="Show PAR2/NFO"
        )
        self.assertEqual(dialog._update_file_actions.call_count, 2)

        dialog._toggle_auxiliary_files_filter()
        dialog.files.set_children.assert_called_with(
            "",
            "video-row",
            "nfo-row",
            "par2-row",
        )
        dialog.auxiliary_files_filter_button.configure.assert_called_with(
            text="Hide PAR2/NFO"
        )

    def test_antialiased_globe_assets_are_square_pngs_for_both_themes(self) -> None:
        asset_directory = Path(__file__).resolve().parent.parent / "src" / "img"
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
        dialog._results = {
            "selected-result": SimpleNamespace(token="selected-token")
        }
        dialog._restoring_result_token = None
        with patch.object(dialog, "resolve_selected") as resolve_selected:
            dialog._selection_changed(Mock())

        resolve_selected.assert_called_once_with()

    def test_right_click_result_offers_refresh_selected(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        dialog.dialog = Mock()
        dialog._request_running = False
        result = SimpleNamespace(token="selected-token")
        dialog._results = {"result-row": result}
        dialog.results = Mock()
        dialog.results.identify_row.return_value = "result-row"
        dialog.results.selection.return_value = ()
        event = SimpleNamespace(x=100, y=15, x_root=100, y_root=200)

        with (
            patch("src.usenet_finder_dialog.tk.Menu") as menu_class,
            patch.object(dialog, "resolve_selected") as refresh,
        ):
            self.assertEqual(dialog._show_result_context_menu(event), "break")

        dialog.results.selection_set.assert_called_once_with("result-row")
        menu = menu_class.return_value
        self.assertEqual(menu.add_command.call_args.kwargs["label"], "Refresh selected")
        self.assertEqual(menu.add_command.call_args.kwargs["state"], "normal")
        menu.tk_popup.assert_called_once_with(100, 200)
        menu.grab_release.assert_called_once_with()
        menu.add_command.call_args.kwargs["command"]()
        refresh.assert_called_once_with()

    def test_right_click_resolved_file_offers_add_selected(self) -> None:
        dialog = UsenetFinderDialog.__new__(UsenetFinderDialog)
        dialog.dialog = Mock()
        dialog._request_running = False
        file = FinderFile("episode.mkv", "https://example.test/file", "1 GB", True, None, {})
        dialog._files = {"file-row": file}
        dialog.files = Mock()
        selected: list[str] = []
        dialog.files.identify_row.return_value = "file-row"
        dialog.files.selection.side_effect = lambda: tuple(selected)
        dialog.files.selection_set.side_effect = lambda item_id: selected.append(item_id)
        dialog._update_file_actions = Mock()
        event = SimpleNamespace(x=100, y=15, x_root=100, y_root=200)

        with (
            patch("src.usenet_finder_dialog.tk.Menu") as menu_class,
            patch.object(dialog, "add_selected_files") as add_selected,
        ):
            self.assertEqual(dialog._show_file_context_menu(event), "break")

        self.assertEqual(selected, ["file-row"])
        dialog._update_file_actions.assert_called_once_with()
        menu = menu_class.return_value
        self.assertEqual(menu.add_command.call_args.kwargs["label"], "Add selected to queue")
        self.assertEqual(menu.add_command.call_args.kwargs["state"], "normal")
        menu.tk_popup.assert_called_once_with(100, 200)
        menu.grab_release.assert_called_once_with()
        menu.add_command.call_args.kwargs["command"]()
        add_selected.assert_called_once_with()


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
