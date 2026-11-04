from tests._test_support import (
    COLUMN_ORDER,
    DownloaderApp,
    FakeSelectionTable,
    MagicMock,
    Mock,
    QueueTableView,
    SimpleNamespace,
    call,
    json,
    patch,
    show_column_context_menu,
    threading,
    unittest,
)

class QueueUiTests(unittest.TestCase):
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

    def test_pause_button_is_enabled_only_while_queue_can_be_paused(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app.worker = SimpleNamespace(is_alive=lambda: True)
        app.stop_event = threading.Event()
        app.stop_button = Mock()

        app._queue_active = False
        app._sync_pause_button_state()
        app.stop_button.configure.assert_called_with(state="disabled")

        app.stop_button.reset_mock()
        app._queue_active = True
        app._sync_pause_button_state()
        app.stop_button.configure.assert_called_with(state="normal")

        app.stop_button.reset_mock()
        app.stop_event.set()
        app._sync_pause_button_state()
        app.stop_button.configure.assert_called_with(state="disabled")

    def test_horizontal_table_scrollbar_tracks_column_overflow(self) -> None:
        app = DownloaderApp.__new__(DownloaderApp)
        app._schedule_progress_indicator_layout = Mock()
        scrollbar = Mock()

        app._update_table_scrollbar(
            scrollbar,
            "0.1",
            "0.9",
            show_if_needed=True,
        )
        scrollbar.set.assert_called_once_with("0.1", "0.9")
        scrollbar.grid.assert_called_once_with()
        scrollbar.grid_remove.assert_not_called()

        scrollbar.reset_mock()
        app._update_table_scrollbar(
            scrollbar,
            "0",
            "1",
            show_if_needed=True,
        )
        scrollbar.set.assert_called_once_with("0", "1")
        scrollbar.grid_remove.assert_called_once_with()
        scrollbar.grid.assert_not_called()
        self.assertEqual(app._schedule_progress_indicator_layout.call_count, 2)

    def test_queue_column_widths_are_saved_after_resizing(self) -> None:
        view = QueueTableView.__new__(QueueTableView)
        view.table = Mock()
        view.table.identify_region.return_value = "separator"
        widths = {column: 150 + index for index, column in enumerate(COLUMN_ORDER)}
        view.table.column.side_effect = lambda column, _option: str(widths[column])
        view.app = SimpleNamespace(
            secure_store=Mock(),
            column_widths={},
            _log=Mock(),
        )

        view._column_resize_finished(SimpleNamespace(x=100, y=10))

        view.app.secure_store.set_setting.assert_called_once_with(
            "column_widths",
            json.dumps(widths),
        )
        self.assertEqual(view.app.column_widths, widths)

    def test_column_context_menu_reorders_and_hides_columns(self) -> None:
        table = MagicMock()
        table.identify_region.return_value = "heading"
        table.identify_column.return_value = "#2"
        table.__getitem__.return_value = ("filename", "extension", "link")
        on_change = Mock()
        event = SimpleNamespace(x=120, x_root=420, y=12, y_root=212)

        with patch("src.table_column_menu.tk.Menu") as menu_class:
            result = show_column_context_menu(
                table,
                event,
                ["filename", "extension", "file_path", "link"],
                ["filename", "extension", "link"],
                on_change,
            )

        self.assertEqual(result, "break")
        menu = menu_class.return_value
        menu.add_command.call_args_list[0].kwargs["command"]()
        on_change.assert_called_once_with(
            ["extension", "filename", "file_path", "link"],
            ["extension", "filename", "link"],
        )
        menu.add_command.call_args_list[2].kwargs["command"]()
        on_change.assert_called_with(
            ["filename", "extension", "file_path", "link"],
            ["filename", "link"],
        )

    def test_column_context_menu_keeps_one_column_visible(self) -> None:
        table = MagicMock()
        table.identify_region.return_value = "heading"
        table.identify_column.return_value = "#1"
        table.__getitem__.return_value = ("filename",)
        on_change = Mock()
        event = SimpleNamespace(x=30, x_root=30, y=10, y_root=10)

        with patch("src.table_column_menu.tk.Menu") as menu_class:
            show_column_context_menu(table, event, ["filename"], ["filename"], on_change)

        self.assertEqual(menu_class.return_value.add_command.call_args_list[2].kwargs["state"], "disabled")
        menu_class.return_value.add_command.call_args_list[2].kwargs["command"]()
        on_change.assert_not_called()

    def test_queue_column_context_menu_is_posted_on_heading(self) -> None:
        view = QueueTableView.__new__(QueueTableView)
        view.table = MagicMock()
        view.table.identify_region.return_value = "heading"
        view.table.identify_column.return_value = "#2"
        view.table.__getitem__.return_value = ("filename", "extension", "link")
        view.app = SimpleNamespace(
            column_order=["filename", "extension", "link"],
            visible_columns=["filename", "extension", "link"],
        )
        event = SimpleNamespace(x=120, x_root=420, y=12, y_root=212)

        with patch("src.queue_table.tk.Menu") as menu_class:
            result = view._show_column_menu(event)

        self.assertEqual(result, "break")
        menu = menu_class.return_value
        menu.tk_popup.assert_called_once_with(420, 212)
        self.assertIs(view.table._column_context_menu, menu)
        menu.grab_release.assert_not_called()
        self.assertEqual(
            [call.kwargs["label"] for call in menu.add_command.call_args_list],
            ["Move left", "Move right", "Hide column"],
        )

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
