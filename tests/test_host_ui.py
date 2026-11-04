from tests._test_support import (
    DeepbridError,
    DownloaderApp,
    HostsErrorEvent,
    HostsLoadedEvent,
    Mock,
    SettingsDialog,
    SimpleNamespace,
    _filter_and_sort_host_rows,
    call,
    json,
    load_table_column_layout,
    load_table_column_widths,
    patch,
    save_table_column_layout,
    unittest,
)

class HostUiTests(unittest.TestCase):
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
        app._hosts_refreshing = False
        app.api_key = SimpleNamespace(get=lambda: "test-key")
        app.status_text = SimpleNamespace(set=lambda _value: None)
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

    def test_settings_host_popup_does_not_block_settings_focus(self) -> None:
        settings = SettingsDialog.__new__(SettingsDialog)
        settings.dialog = Mock()
        popup = Mock()
        settings.app = SimpleNamespace(
            hosts_popup=popup,
            _refresh_hosts=Mock(),
        )

        settings._show_host_status()

        settings.app._refresh_hosts.assert_called_once_with(show_popup=True)
        popup.transient.assert_called_once_with("")
        popup.grab_set.assert_not_called()
        popup.lift.assert_called_once_with()
        popup.bind.assert_not_called()
        settings.dialog.grab_set.assert_not_called()
        settings.dialog.grab_release.assert_not_called()

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

    def test_usenet_column_settings_can_open_directly_from_settings(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app.usenet_finder_dialog = None
        parent = Mock()

        with patch("src.app.UsenetFinderDialog.show_column_settings") as show_settings:
            app._show_usenet_column_settings("search", parent)

        show_settings.assert_called_once_with(
            app,
            parent,
            "search",
            table=None,
            on_apply=None,
        )

    def test_host_column_layout_defaults_and_saved_layouts(self) -> None:
        store = Mock()
        store.get_setting.return_value = None

        order, visible = load_table_column_layout(store, "hosts")

        self.assertEqual(order, ["host", "availability", "limit"])
        self.assertEqual(visible, order)
        store.get_setting.assert_any_call("host_status_column_order")
        store.get_setting.assert_any_call("host_status_visible_columns")

    def test_column_layout_saves_for_host_and_finder_tables(self) -> None:
        store = Mock()

        save_table_column_layout(store, "hosts", ["availability", "host"], ["host"])
        self.assertEqual(
            store.set_setting.call_args_list,
            [
                call("host_status_column_order", '["availability", "host"]'),
                call("host_status_visible_columns", '["host"]'),
            ],
        )

        with self.assertRaises(ValueError):
            save_table_column_layout(store, "unknown", [], [])

    def test_host_column_widths_restore_saved_values(self) -> None:
        store = Mock()
        store.get_setting.side_effect = lambda name: (
            json.dumps({"host": 360, "availability": 250, "limit": 140})
            if name == "host_status_column_widths"
            else None
        )

        widths = load_table_column_widths(store, "hosts")

        self.assertEqual(widths, {"host": 360, "availability": 250, "limit": 140})

    def test_host_column_widths_are_saved_after_resizing(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app.hosts_popup_table = Mock()
        widths = {"host": 360, "availability": 280, "limit": 160}
        app.hosts_popup_table.column.side_effect = lambda column, _option: str(
            widths[column]
        )
        app.secure_store = Mock()
        app._log = Mock()

        app._save_host_column_widths()

        app.secure_store.set_setting.assert_called_once_with(
            "host_status_column_widths",
            json.dumps(widths),
        )

    def test_host_column_settings_can_open_directly_from_settings(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app.hosts_popup_table = None
        parent = Mock()

        with patch("src.app.UsenetFinderDialog.show_column_settings") as show_settings:
            app._show_host_column_settings(parent)

        show_settings.assert_called_once_with(
            app,
            parent,
            "hosts",
            table=None,
        )
