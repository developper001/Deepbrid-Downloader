from __future__ import annotations

import json
import queue
import sqlite3
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Protocol

from .usenet_finder import (
    FinderFile,
    FinderPackage,
    FinderResult,
    FinderSearchPage,
    UsenetFinderClient,
    UsenetFinderError,
    file_size_bytes,
)
from .usenet_browser import UsenetBrowserSession
from .secure_store import SecureStorageError, SecureStore
from .usenet_finder_state import UsenetFinderState, UsenetFinderStateError


SEARCH_COLUMNS = ("title", "category", "size", "date")
DEFAULT_SEARCH_COLUMNS = SEARCH_COLUMNS
FILE_COLUMNS = ("name", "extension", "size", "availability")
DEFAULT_FILE_COLUMNS = FILE_COLUMNS
SEARCH_COLUMN_LABELS = {
    "title": "Title",
    "category": "Category",
    "size": "Size",
    "date": "Date",
}
FILE_COLUMN_LABELS = {
    "name": "File name",
    "extension": "Extension",
    "size": "Size",
    "availability": "Availability",
}


class FinderController(Protocol):
    root: tk.Tk
    theme_colors: dict[str, str]
    usenet_browser: UsenetBrowserSession
    secure_store: SecureStore
    def _log(self, message: str) -> None: ...
    def add_usenet_links(self, links: list[tuple[str, str]]) -> tuple[int, int, int]: ...
    def _clear_usenet_finder_search_history(self) -> None: ...
    def _show_settings(self, initial_filter: str = "") -> None: ...


class UsenetFinderDialog:
    PAGE_SIZE = 15
    CLEAR_HISTORY_OPTION = "[Clear search history]"

    def __init__(self, app: FinderController):
        self.app = app
        self.dialog = tk.Toplevel(app.root)
        self.dialog.title("Usenet Finder")
        self.dialog.geometry("900x650")
        self.dialog.minsize(680, 480)
        self.dialog.resizable(True, True)
        self.dialog.configure(background=app.theme_colors["background"])
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._request_running = False
        self._has_more = False
        self._offset = 0
        self._results: dict[str, FinderResult] = {}
        self._result_page_offsets: dict[str, int] = {}
        self._result_records: list[FinderResult] = []
        self._sort_column = "title"
        self._sort_reverse = False
        self._browser_check_running = False
        self._restoring_result_token: str | None = None
        self._files: dict[str, FinderFile] = {}
        self._file_sort_column = "name"
        self._file_sort_reverse = False
        self._suppress_result_selection_token: str | None = None
        self.search_column_order, self.search_visible_columns = self._load_column_layout(
            "usenet_search_column_order",
            "usenet_search_visible_columns",
            SEARCH_COLUMNS,
            DEFAULT_SEARCH_COLUMNS,
        )
        self.file_column_order, self.file_visible_columns = self._load_column_layout(
            "usenet_file_column_order",
            "usenet_file_visible_columns",
            FILE_COLUMNS,
            DEFAULT_FILE_COLUMNS,
        )
        self.client = UsenetFinderClient(app.usenet_browser)
        self.state = UsenetFinderState(
            app.secure_store,
            cache_duration_hours=lambda: app.usenet_finder_cache_duration_hours,
        )
        self._poll_id: str | None = None
        self._build_ui()
        try:
            self._update_search_history_options()
        except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
            app._log(f"Could not restore Usenet Finder search history: {error}")
            self.query_history = []
            self.query_entry.configure(values=(self.CLEAR_HISTORY_OPTION,))
        restored_search: tuple[str, str] | None = None
        try:
            restored_search = self.state.load_search()
        except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
            app._log(f"Could not restore Usenet Finder search settings: {error}")
            self.status.set("Could not restore the previous Usenet Finder search.")
        else:
            self.query.set(restored_search[0])
            self.category.set(restored_search[1])
        self.dialog.protocol("WM_DELETE_WINDOW", self.close)
        self.dialog.bind("<Escape>", lambda _event: self.close())
        self.query_entry.focus_set()
        self._poll_id = self.dialog.after(100, self._process_events)
        self.dialog.after(1000, self._check_browser_health)
        self.open_browser()
        if restored_search is not None and restored_search[0]:
            self._restore_cached_results(*restored_search)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self.dialog, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(4, weight=3)

        header = ttk.Frame(frame)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        logo_background = self.app.theme_colors["background"]
        logo_foreground = self.app.theme_colors["foreground"]
        logo_accent = self.app.theme_colors["accent"]
        self.logo = tk.Canvas(
            header,
            width=168,
            height=46,
            background=logo_background,
            highlightthickness=0,
            borderwidth=0,
        )
        self.logo.grid(row=0, column=0, sticky="w")
        resource_root = Path(
            getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)
        )
        globe_filename = "usenet-globe-dark.png" if self.app.dark_theme else "usenet-globe-light.png"
        self.globe_image = tk.PhotoImage(
            master=self.dialog,
            file=str(resource_root / "src" / globe_filename),
        )
        self.logo.create_image(23, 23, image=self.globe_image)
        self.logo.create_text(
            51,
            17,
            anchor="w",
            text="USENET",
            fill=logo_foreground,
            font=("Segoe UI", 16, "bold"),
        )
        self.logo.create_text(
            53,
            34,
            anchor="w",
            text="FINDER",
            fill=logo_accent,
            font=("Segoe UI", 8, "bold"),
        )
        header_actions = ttk.Frame(header)
        header_actions.grid(row=0, column=1, sticky="e")
        self.usenet_settings_button = ttk.Button(
            header_actions,
            text="Usenet settings",
            command=lambda: self.app._show_settings("Usenet Finder"),
        )
        self.usenet_settings_button.pack(side="right")
        self.open_browser_button = ttk.Button(
            header_actions,
            text="Open Chrome / sign in",
            command=self.open_browser,
        )
        self.open_browser_button.pack(side="right", padx=(0, 8))
        self.browser_status = tk.StringVar(master=self.dialog, value="Browser: checking")
        ttk.Label(header_actions, textvariable=self.browser_status).pack(
            side="right",
            padx=(0, 8),
        )

        search_row = ttk.Frame(frame)
        search_row.grid(row=1, column=0, sticky="ew")
        search_row.columnconfigure(1, weight=1)
        ttk.Label(search_row, text="Search").grid(row=0, column=0, padx=(0, 8))
        self.query = tk.StringVar(master=self.dialog)
        self.query_entry = ttk.Combobox(
            search_row,
            textvariable=self.query,
            values=(),
        )
        self.query_entry.grid(row=0, column=1, sticky="ew")
        self.query_entry.bind("<Return>", lambda _event: self.search())
        self.query_entry.bind("<<ComboboxSelected>>", self._search_history_selected)
        ttk.Label(search_row, text="Category").grid(row=0, column=2, padx=(12, 8))
        self.category = tk.StringVar(master=self.dialog)
        self.category_picker = ttk.Combobox(
            search_row,
            textvariable=self.category,
            values=("",),
            width=18,
        )
        self.category_picker.grid(
            row=0,
            column=3,
        )
        self.search_button = ttk.Button(search_row, text="Search", command=self.search)
        self.search_button.grid(row=0, column=4, padx=(8, 0))

        ttk.Label(
            frame,
            text=(
                "Sign in using the dedicated Chrome window, then return here to search. "
                "Keep Chrome open while using Finder."
            ),
            wraplength=850,
        ).grid(row=2, column=0, sticky="w", pady=(6, 10))

        filter_row = ttk.Frame(frame)
        filter_row.grid(row=3, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(filter_row, text="Filter results").pack(side="left", padx=(0, 8))
        self.result_filter = tk.StringVar(master=self.dialog)
        filter_entry = ttk.Entry(filter_row, textvariable=self.result_filter)
        filter_entry.pack(side="left", fill="x", expand=True)
        self.result_filter.trace_add("write", lambda *_args: self._refresh_result_rows())

        self.results = ttk.Treeview(
            frame,
            columns=SEARCH_COLUMNS,
            show="headings",
            selectmode="browse",
        )
        for column, width in (
            ("title", 470),
            ("category", 100),
            ("size", 100),
            ("date", 150),
        ):
            self.results.heading(
                column,
                text=SEARCH_COLUMN_LABELS[column],
                command=lambda selected_column=column: self._sort_results(selected_column),
            )
            self.results.column(column, width=width, anchor="w")
        self.results.configure(displaycolumns=self.search_visible_columns)
        self.results.grid(row=4, column=0, sticky="nsew")
        results_scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.results.yview)
        results_scrollbar.grid(row=4, column=1, sticky="ns")
        self.results.configure(yscrollcommand=results_scrollbar.set)
        self.results.bind("<<TreeviewSelect>>", self._selection_changed)
        self.results.bind("<Double-1>", lambda _event: self.resolve_selected())
        self.results.bind("<Button-3>", self._show_result_context_menu)
        self.results.bind("<Button-2>", self._show_result_context_menu)

        result_actions = ttk.Frame(frame)
        result_actions.grid(row=5, column=0, sticky="ew", pady=(8, 8))
        ttk.Button(
            result_actions,
            text="Search columns...",
            command=lambda: self._show_column_settings("search"),
        ).pack(side="left")
        self.more_button = ttk.Button(
            result_actions,
            text="Load more",
            command=self.load_more,
            state="disabled",
        )
        self.more_button.pack(side="left", padx=(8, 0))
        self.status = tk.StringVar(
            master=self.dialog,
            value="Open Chrome / sign in before searching.",
        )
        ttk.Label(result_actions, textvariable=self.status).pack(side="left", padx=(12, 0))

        self.files = ttk.Treeview(
            frame,
            columns=FILE_COLUMNS,
            show="headings",
            selectmode="extended",
            height=6,
        )
        for column, width in (
            ("name", 500),
            ("extension", 100),
            ("size", 120),
            ("availability", 150),
        ):
            self.files.heading(
                column,
                text=FILE_COLUMN_LABELS[column],
                command=lambda selected_column=column: self._sort_files(selected_column),
            )
            self.files.column(column, width=width, anchor="w")
        self.files.configure(displaycolumns=self.file_visible_columns)
        self.files.tag_configure(
            "inaccessible",
            foreground=self.app.theme_colors["error"],
        )
        self.files.grid(row=7, column=0, sticky="nsew")
        files_scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.files.yview)
        files_scrollbar.grid(row=7, column=1, sticky="ns")
        self.files.configure(yscrollcommand=files_scrollbar.set)
        self.files.bind("<<TreeviewSelect>>", self._file_selection_changed)
        self.files.bind("<Button-3>", self._show_file_context_menu)
        self.files.bind("<Button-2>", self._show_file_context_menu)
        resolved_header = ttk.Frame(frame)
        resolved_header.grid(row=6, column=0, sticky="ew", pady=(0, 4))
        resolved_header.columnconfigure(0, weight=1)
        ttk.Label(resolved_header, text="Resolved files").grid(
            row=0,
            column=0,
            sticky="sw",
        )
        frame.rowconfigure(7, weight=2)
        file_actions = ttk.Frame(frame)
        file_actions.grid(row=8, column=0, sticky="ew", pady=(8, 0))
        self.add_selected_button = ttk.Button(
            file_actions,
            text="Add selected to queue",
            command=self.add_selected_files,
            state="disabled",
        )
        self.add_selected_button.pack(side="left")
        self.add_accessible_button = ttk.Button(
            file_actions,
            text="Add all accessible to queue",
            command=self.add_all_accessible_files,
            state="disabled",
        )
        self.add_accessible_button.pack(side="left", padx=(8, 0))
        ttk.Button(
            file_actions,
            text="File columns...",
            command=lambda: self._show_column_settings("files"),
        ).pack(side="left", padx=(8, 0))

    def open_browser(self) -> bool:
        try:
            self.app.usenet_browser.open()
        except UsenetFinderError as error:
            self.status.set("Chrome or Edge is required to use Usenet Finder.")
            self.app._log(f"Could not open Usenet Finder browser: {error}")
            messagebox.showerror(
                "Browser required for Usenet Finder",
                str(error),
                parent=self.dialog,
            )
            return False
        self.status.set(
            "In the dedicated Chrome window, complete any Cloudflare check and sign in; "
            "then return and search."
        )
        return True

    def _search_history_selected(self, _event: tk.Event | None = None) -> None:
        selected_query = self.query.get()
        if selected_query == self.CLEAR_HISTORY_OPTION:
            if messagebox.askyesno(
                "Clear Usenet search history",
                "Clear all saved Usenet Finder searches?",
                parent=self.dialog,
            ):
                try:
                    self.app._clear_usenet_finder_search_history()
                    saved_query, saved_category = self.state.load_search()
                    self.query.set(saved_query)
                    self.category.set(saved_category)
                    self.status.set("Usenet Finder search history cleared.")
                except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
                    self.app._log(f"Could not clear Usenet Finder search history: {error}")
                    self.status.set(f"Could not clear search history: {error}")
                    self.query.set("")
            else:
                try:
                    selected_query, category = self.state.load_search()
                    self.query.set(selected_query)
                    self.category.set(category)
                except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
                    self.app._log(f"Could not restore Usenet Finder search: {error}")
                    self.query.set("")
            return
        for query, category in self.query_history:
            if query == selected_query:
                self.category.set(category)
                break

    def _update_search_history_options(self) -> None:
        self.query_history = self.state.load_search_history()
        self.query_entry.configure(
            values=(
                *(query for query, _category in self.query_history),
                self.CLEAR_HISTORY_OPTION,
            )
        )

    def _check_browser_health(self) -> None:
        if not self.dialog.winfo_exists():
            return
        if not self._browser_check_running:
            self._browser_check_running = True

            def check() -> None:
                connected = self.app.usenet_browser._debug_port() is not None
                self._events.put(("browser_health", connected))

            threading.Thread(target=check, daemon=True).start()
        self.dialog.after(30000, self._check_browser_health)

    def search(self) -> None:
        if self._request_running:
            return
        if not self.open_browser():
            return
        query = self.query.get().strip()
        category = self.category.get().strip()
        if query:
            try:
                self.state.save_search(query, category)
                if hasattr(self, "query_entry"):
                    self._update_search_history_options()
                self.state.clear_last_selected_result()
            except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
                self.app._log(f"Could not save Usenet Finder search settings: {error}")
        self._offset = 0
        self._results.clear()
        self._result_page_offsets.clear()
        self.results.delete(*self.results.get_children())
        self.files.delete(*self.files.get_children())
        self._files.clear()
        self._update_file_actions()
        self._has_more = False
        self.more_button.configure(state="disabled")
        self._search_page(append=False)

    def load_more(self) -> None:
        if not self._request_running and self._has_more:
            self._search_page(append=True)

    def _restore_cached_results(self, query: str, category: str) -> None:
        try:
            cached = self.state.get_search_page_with_expiry(
                query,
                category,
                0,
                self.PAGE_SIZE,
            )
        except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
            self.app._log(f"Could not restore cached Usenet Finder search results: {error}")
            return
        if cached is None:
            return
        page, expires_at = cached
        self._show_search_page(page, append=False, cached_expiry=expires_at)
        self.status.set(
            f"Restored {len(self._result_records)} cached result(s); "
            f"cache expires {datetime.fromtimestamp(expires_at).strftime('%Y-%m-%d %H:%M')}."
        )
        self._restore_last_resolved(query, category)

    def _restore_last_resolved(self, query: str, category: str) -> None:
        try:
            selected = self.state.load_last_selected_result()
            if not isinstance(selected, tuple) or len(selected) != 4:
                return
            if selected[:2] != (query, category):
                self.state.clear_last_selected_result()
                return
            _saved_query, _saved_category, token, selected_offset = selected
            while selected_offset >= self._offset:
                cached_page = self.state.get_search_page_with_expiry(
                    query,
                    category,
                    self._offset,
                    self.PAGE_SIZE,
                )
                if cached_page is None:
                    break
                page, page_expiry = cached_page
                if not page.results:
                    break
                self._show_search_page(
                    page,
                    append=True,
                    cached_expiry=page_expiry,
                )
            item_id = next(
                (
                    item_id
                    for item_id, result in self._results.items()
                    if result.token == token
                ),
                None,
            )
            cached = self.state.get_resolved_with_expiry(token)
            if cached is None:
                self.state.clear_last_selected_result()
                return
            package, expires_at = cached
            if item_id is not None:
                self._restoring_result_token = token
                self.results.selection_set(item_id)
                self.results.focus(item_id)
            self._show_package(package)
            self.status.set(
                f"Restored last resolved links ({len(package.files)} file(s)); "
                f"cache expires {datetime.fromtimestamp(expires_at).strftime('%Y-%m-%d %H:%M')}."
            )
        except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
            self.app._log(f"Could not restore last Usenet Finder resolution: {error}")

    def _search_page(self, *, append: bool) -> None:
        query = self.query.get().strip()
        category = self.category.get().strip()
        if not query:
            self.status.set("Enter a search query.")
            return
        offset = self._offset
        try:
            cached = self.state.get_search_page_with_expiry(
                query,
                category,
                offset,
                self.PAGE_SIZE,
            )
        except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
            self.app._log(f"Could not read Usenet Finder search cache: {error}")
            cached = None
        if cached is not None:
            page, expires_at = cached
            self._show_search_page(page, append, cached_expiry=expires_at)
            self.status.set(
                f"Loaded {len(self._result_records)} cached result(s); "
                f"cache expires {datetime.fromtimestamp(expires_at).strftime('%Y-%m-%d %H:%M')}."
            )
            return
        self._begin_request("Searching...")

        def request() -> None:
            try:
                page = self.client.search(query, category, offset, self.PAGE_SIZE)
                try:
                    self.state.save_search_page(
                        query,
                        category,
                        offset,
                        self.PAGE_SIZE,
                        page,
                    )
                except (
                    UsenetFinderStateError,
                    SecureStorageError,
                    sqlite3.Error,
                    TypeError,
                ) as error:
                    self.app._log(f"Could not cache Usenet Finder search results: {error}")
                self._events.put(("search", (page, append)))
            except (UsenetFinderError, ValueError) as error:
                self.app._log(f"Finder search failed: {error}")
                self._events.put(("error", str(error)))

        threading.Thread(target=request, daemon=True).start()

    def resolve_selected(self) -> None:
        selection = self.results.selection()
        if not selection or self._request_running:
            return
        result = self._results.get(selection[0])
        if result is None:
            return
        try:
            result_offset = self._result_page_offsets.get(result.token, 0)
            self.state.save_last_selected_result(
                self.query.get(),
                self.category.get(),
                result.token,
                result_offset,
            )
        except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
            self.app._log(f"Could not save selected Usenet Finder result: {error}")
        try:
            cached_resolution = self.state.get_resolved_with_expiry(result.token)
        except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
            self.app._log(f"Could not read Usenet Finder resolved-link cache: {error}")
            cached_resolution = None
        if cached_resolution is not None:
            package, expires_at = cached_resolution
            self._show_package(package)
            self.status.set(
                f"Loaded saved resolution ({len(package.files)} file(s)); "
                f"cache expires {datetime.fromtimestamp(expires_at).strftime('%Y-%m-%d %H:%M')}."
            )
            return
        self.files.delete(*self.files.get_children())
        self._files.clear()
        self._begin_request("Resolving selected result...")

        def request() -> None:
            try:
                package = self.client.resolve(result.token)
                try:
                    self.state.save_resolved(result.token, package)
                except (
                    UsenetFinderStateError,
                    SecureStorageError,
                    sqlite3.Error,
                    TypeError,
                ) as error:
                    self.app._log(f"Could not cache Usenet Finder resolved links: {error}")
                self._events.put(("resolve", package))
            except (UsenetFinderError, ValueError) as error:
                self.app._log(f"Finder result resolution failed: {error}")
                self._events.put(("error", str(error)))

        threading.Thread(target=request, daemon=True).start()

    def _begin_request(self, message: str) -> None:
        self._request_running = True
        self.search_button.configure(state="disabled")
        self.more_button.configure(state="disabled")
        self.add_selected_button.configure(state="disabled")
        self.add_accessible_button.configure(state="disabled")
        self.status.set(message)

    def _selection_changed(self, _event: tk.Event) -> None:
        if self._request_running:
            return
        selection = self.results.selection()
        if selection:
            selected = self._results.get(selection[0])
            if (
                selected is not None
                and selected.token == getattr(
                    self,
                    "_suppress_result_selection_token",
                    None,
                )
            ):
                self._suppress_result_selection_token = None
                return
        if not selection:
            return
        selected_result = self._results.get(selection[0])
        if (
            selected_result is not None
            and selected_result.token == self._restoring_result_token
        ):
            self._restoring_result_token = None
            return
        self._restoring_result_token = None
        self.resolve_selected()

    def _show_result_context_menu(self, event: tk.Event) -> str:
        item_id = self.results.identify_row(event.y)
        if not item_id:
            return "break"
        result = self._results.get(item_id)
        if result is None:
            return "break"
        if item_id not in self.results.selection():
            if not self._request_running:
                self._suppress_result_selection_token = result.token
            self.results.selection_set(item_id)
        menu = tk.Menu(self.dialog, tearoff=0)
        menu.add_command(
            label="Refresh selected",
            command=self.resolve_selected,
            state="disabled" if self._request_running else "normal",
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _show_file_context_menu(self, event: tk.Event) -> str:
        item_id = self.files.identify_row(event.y)
        if not item_id:
            return "break"
        if item_id not in self.files.selection():
            self.files.selection_set(item_id)
            self._update_file_actions()
        can_add = any(
            self._files.get(selected_id) is not None
            and self._files[selected_id].is_accessible
            for selected_id in self.files.selection()
        )
        menu = tk.Menu(self.dialog, tearoff=0)
        menu.add_command(
            label="Add selected to queue",
            command=self.add_selected_files,
            state="normal" if can_add and not self._request_running else "disabled",
        )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _process_events(self) -> None:
        if not self.dialog.winfo_exists():
            return
        while True:
            try:
                event, payload = self._events.get_nowait()
            except queue.Empty:
                break
            if event == "browser_health":
                self._browser_check_running = False
                self.browser_status.set(
                    "Browser: connected" if payload else "Browser: not connected"
                )
                continue
            self._request_running = False
            self.search_button.configure(state="normal")
            self.more_button.configure(
                state="normal" if self._has_more else "disabled"
            )
            self._update_file_actions()
            if event == "error":
                self.status.set(f"Finder request failed: {payload}")
                continue
            if event == "search":
                page, append = payload
                expires_at = time.time() + self.state.cache_ttl_seconds()
                self._show_search_page(page, append, cached_expiry=expires_at)
            elif event == "resolve":
                self._show_package(payload)
        self._poll_id = self.dialog.after(100, self._process_events)

    def _show_search_page(
        self,
        page: FinderSearchPage,
        append: bool,
        *,
        cached_expiry: float | None = None,
    ) -> None:
        if not append:
            self._result_records.clear()
            self._result_page_offsets.clear()
            self._offset = 0
        page_offset = self._offset
        for result in page.results:
            self._result_page_offsets[result.token] = page_offset
        self._result_records.extend(page.results)
        categories = sorted(
            {result.category for result in self._result_records if result.category}
        )
        self.category_picker.configure(values=("", *categories))
        self._refresh_result_rows()
        self._offset += len(page.results)
        self._has_more = page.has_more
        self.more_button.configure(
            state="normal" if page.has_more and page.results else "disabled"
        )
        message = (
            f"Loaded {len(self._result_records)} result(s). "
            f"{'More results available.' if page.has_more else 'No more results.'}"
        )
        if cached_expiry is not None:
            message += (
                f" Cached results expire "
                f"{datetime.fromtimestamp(cached_expiry).strftime('%Y-%m-%d %H:%M')}."
            )
        self.status.set(message)

    def _refresh_result_rows(self) -> None:
        self.results.delete(*self.results.get_children())
        self._results.clear()
        filter_text = self.result_filter.get().strip().casefold()
        records = self._result_records
        if self._sort_column == "size":
            records = sorted(
                records,
                key=lambda result: (
                    result.size_bytes is None,
                    result.size_bytes if result.size_bytes is not None else 0,
                ),
                reverse=self._sort_reverse,
            )
        else:
            records = sorted(
                records,
                key=lambda result: str(getattr(result, self._sort_column)).casefold(),
                reverse=self._sort_reverse,
            )
        for result in records:
            searchable = " ".join(
                (result.title, result.category, result.size, result.date)
            ).casefold()
            if filter_text and filter_text not in searchable:
                continue
            item_id = self.results.insert(
                "",
                "end",
                values=(result.title, result.category, result.size, result.date),
            )
            self._results[item_id] = result

    def _sort_results(self, column: str) -> None:
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False
        self._refresh_result_rows()

    def _load_column_layout(
        self,
        order_setting: str,
        visible_setting: str,
        columns: tuple[str, ...],
        default_visible: tuple[str, ...],
    ) -> tuple[list[str], list[str]]:
        try:
            saved_order = json.loads(self.app.secure_store.get_setting(order_setting) or "[]")
            saved_visible = json.loads(
                self.app.secure_store.get_setting(visible_setting) or "[]"
            )
        except (json.JSONDecodeError, sqlite3.Error):
            return list(columns), list(default_visible)
        order = (
            [column for column in saved_order if column in columns]
            if isinstance(saved_order, list)
            else []
        )
        order = list(dict.fromkeys((*order, *columns)))
        visible = (
            {column for column in saved_visible if column in columns}
            if isinstance(saved_visible, list)
            else set()
        )
        if not visible:
            visible = set(default_visible)
        return order, [column for column in order if column in visible]

    def _show_column_settings(self, table_name: str) -> None:
        if table_name == "search":
            table = self.results
            columns = SEARCH_COLUMNS
            labels = SEARCH_COLUMN_LABELS
            order_setting = "usenet_search_column_order"
            visible_setting = "usenet_search_visible_columns"
            working_order = list(self.search_column_order)
            working_visible = set(self.search_visible_columns)
        else:
            table = self.files
            columns = FILE_COLUMNS
            labels = FILE_COLUMN_LABELS
            order_setting = "usenet_file_column_order"
            visible_setting = "usenet_file_visible_columns"
            working_order = list(self.file_column_order)
            working_visible = set(self.file_visible_columns)

        dialog = tk.Toplevel(self.dialog)
        dialog.title("Usenet columns")
        dialog.transient(self.dialog)
        dialog.resizable(False, False)
        dialog.geometry("390x300")
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        column_tree = ttk.Treeview(
            frame,
            columns=("name", "visibility"),
            show="headings",
            selectmode="browse",
            height=len(columns),
        )
        column_tree.heading("name", text="Column")
        column_tree.heading("visibility", text="Visibility")
        column_tree.column("name", width=220, stretch=True)
        column_tree.column("visibility", width=85, stretch=False, anchor="center")
        column_tree.grid(row=0, column=0, sticky="nsew")

        visible_variable = tk.BooleanVar(master=dialog)

        def selected_column() -> str | None:
            selection = column_tree.selection()
            return selection[0] if selection else None

        def update_controls() -> None:
            column = selected_column()
            if column is None:
                visible_variable.set(False)
                visibility_check.configure(state="disabled")
                move_up_button.configure(state="disabled")
                move_down_button.configure(state="disabled")
                return
            visible_variable.set(column in working_visible)
            visibility_check.configure(state="normal")
            position = working_order.index(column)
            move_up_button.configure(state="normal" if position else "disabled")
            move_down_button.configure(
                state="normal" if position < len(working_order) - 1 else "disabled"
            )

        def render_order(selected: str | None = None) -> None:
            column_tree.delete(*column_tree.get_children())
            for column in working_order:
                column_tree.insert(
                    "",
                    "end",
                    iid=column,
                    values=(
                        labels[column],
                        "Visible" if column in working_visible else "Hidden",
                    ),
                )
            if selected in working_order:
                column_tree.selection_set(selected)
                column_tree.focus(selected)
            update_controls()

        def toggle_visibility() -> None:
            column = selected_column()
            if column is None:
                return
            if visible_variable.get():
                working_visible.add(column)
            else:
                working_visible.discard(column)
            render_order(column)

        def move_column(direction: int) -> None:
            column = selected_column()
            if column is None:
                return
            position = working_order.index(column)
            destination = position + direction
            if not 0 <= destination < len(working_order):
                return
            working_order[position], working_order[destination] = (
                working_order[destination],
                working_order[position],
            )
            render_order(column)

        def reset_defaults() -> None:
            working_order[:] = columns
            working_visible.clear()
            working_visible.update(
                DEFAULT_SEARCH_COLUMNS if table_name == "search" else DEFAULT_FILE_COLUMNS
            )
            render_order(columns[0])

        def apply_changes() -> None:
            if not working_visible:
                messagebox.showwarning(
                    "Columns required",
                    "At least one column must remain visible.",
                    parent=dialog,
                )
                return
            ordered_visible = [
                column for column in working_order if column in working_visible
            ]
            try:
                self.app.secure_store.set_setting(order_setting, json.dumps(working_order))
                self.app.secure_store.set_setting(visible_setting, json.dumps(ordered_visible))
            except (OSError, sqlite3.Error) as error:
                self.app._log(f"Could not save Usenet column settings: {error}")
                messagebox.showerror(
                    "Could not save columns",
                    str(error),
                    parent=dialog,
                )
                return
            table.configure(displaycolumns=ordered_visible)
            if table_name == "search":
                self.search_column_order = list(working_order)
                self.search_visible_columns = ordered_visible
            else:
                self.file_column_order = list(working_order)
                self.file_visible_columns = ordered_visible
            dialog.destroy()

        move_buttons = ttk.Frame(frame)
        move_buttons.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        move_up_button = ttk.Button(
            move_buttons,
            text="Move up",
            command=lambda: move_column(-1),
        )
        move_up_button.pack(fill="x")
        move_down_button = ttk.Button(
            move_buttons,
            text="Move down",
            command=lambda: move_column(1),
        )
        move_down_button.pack(fill="x", pady=(6, 0))
        visibility_check = ttk.Checkbutton(
            frame,
            text="Visible",
            variable=visible_variable,
            command=toggle_visibility,
            state="disabled",
        )
        visibility_check.grid(row=1, column=0, sticky="w", pady=(8, 0))
        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="Reset to defaults", command=reset_defaults).pack(side="left")
        ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(actions, text="Apply", command=apply_changes).pack(
            side="right",
            padx=(0, 8),
        )
        column_tree.bind("<<TreeviewSelect>>", lambda _event: update_controls())
        render_order(working_order[0] if working_order else None)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()

    def _show_package(self, package: FinderPackage) -> None:
        self.files.delete(*self.files.get_children())
        self._files.clear()
        for file in package.files:
            item_id = self.files.insert(
                "",
                "end",
                values=(
                    file.name,
                    Path(file.name).suffix.casefold(),
                    file.size,
                    self._availability(file),
                ),
                tags=("inaccessible",) if not file.is_accessible else (),
            )
            self._files[item_id] = file
        self._sort_files(self._file_sort_column, toggle=False)
        package_label = f" ({package.name})" if package.name else ""
        accessible_count = sum(file.is_accessible for file in package.files)
        self.status.set(
            f"Resolved{package_label}: {len(package.files)} file(s); "
            f"{accessible_count} accessible."
        )
        self._update_file_actions()

    def _sort_files(self, column: str, *, toggle: bool = True) -> None:
        if toggle:
            if self._file_sort_column == column:
                self._file_sort_reverse = not self._file_sort_reverse
            else:
                self._file_sort_column = column
                self._file_sort_reverse = False

        item_ids = list(self.files.get_children())
        files = [(item_id, self._files[item_id]) for item_id in item_ids if item_id in self._files]
        if column == "size":
            sized_files = [
                (item_id, file, self._file_size_bytes(file.size))
                for item_id, file in files
            ]
            known_sizes = [entry for entry in sized_files if entry[2] is not None]
            unknown_sizes = [entry for entry in sized_files if entry[2] is None]
            known_sizes.sort(
                key=lambda entry: entry[2],
                reverse=self._file_sort_reverse,
            )
            item_ids = [entry[0] for entry in (*known_sizes, *unknown_sizes)]
        else:
            if column == "availability":
                key = lambda file: self._availability(file).casefold()
            elif column == "extension":
                key = lambda file: Path(file.name).suffix.casefold()
            else:
                key = lambda file: file.name.casefold()
            files.sort(key=lambda entry: key(entry[1]), reverse=self._file_sort_reverse)
            item_ids = [item_id for item_id, _file in files]
        self.files.set_children("", *item_ids)

    @staticmethod
    def _file_size_bytes(size: str) -> int | None:
        return file_size_bytes(size)

    def _file_selection_changed(self, _event: tk.Event | None = None) -> None:
        self._update_file_actions()

    def _update_file_actions(self) -> None:
        selected = self.files.selection()
        can_add = any(
            self._files.get(item_id) is not None and self._files[item_id].is_accessible
            for item_id in selected
        )
        self.add_selected_button.configure(
            state="normal" if can_add and not self._request_running else "disabled"
        )
        can_add_all = any(file.is_accessible for file in self._files.values())
        self.add_accessible_button.configure(
            state="normal" if can_add_all and not self._request_running else "disabled"
        )

    def add_selected_files(self) -> None:
        files = [
            self._files[item_id]
            for item_id in self.files.selection()
            if item_id in self._files and self._files[item_id].is_accessible
        ]
        self._add_files_to_queue(files)

    def add_all_accessible_files(self) -> None:
        self._add_files_to_queue(
            [file for file in self._files.values() if file.is_accessible]
        )

    def _add_files_to_queue(self, files: list[FinderFile]) -> None:
        if not files:
            self.status.set("No accessible files selected to add.")
            return
        added, duplicates, invalid = self.app.add_usenet_links(
            [(file.link, file.name, file_size_bytes(file.size)) for file in files]
        )
        self.status.set(
            f"Added {added} file(s) to the download queue; skipped {duplicates} "
            f"duplicate(s) and {invalid} invalid link(s). Click Start to download."
        )
        if duplicates:
            messagebox.showwarning(
                "Duplicate Usenet files skipped",
                f"{duplicates} link(s) were already in the download queue and were skipped.",
                parent=self.dialog,
            )

    @staticmethod
    def _availability(file: FinderFile) -> str:
        if file.is_accessible:
            return "Accessible"
        return f"Inaccessible: {file.inaccessible}" if file.inaccessible else "No link available"

    def close(self) -> None:
        if self._poll_id is not None:
            try:
                self.dialog.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None
        self.dialog.destroy()
