from __future__ import annotations

import queue
import sqlite3
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from typing import Protocol

from .usenet_finder import (
    FinderFile,
    FinderPackage,
    FinderResult,
    FinderSearchPage,
    UsenetFinderClient,
    UsenetFinderError,
)
from .usenet_browser import UsenetBrowserSession
from .secure_store import SecureStorageError, SecureStore
from .usenet_finder_state import UsenetFinderState, UsenetFinderStateError


class FinderController(Protocol):
    root: tk.Tk
    theme_colors: dict[str, str]
    usenet_browser: UsenetBrowserSession
    secure_store: SecureStore
    def _log(self, message: str) -> None: ...
    def add_usenet_links(self, links: list[tuple[str, str]]) -> tuple[int, int, int]: ...
    def _show_settings(self, initial_filter: str = "") -> None: ...


class UsenetFinderDialog:
    PAGE_SIZE = 15

    def __init__(self, app: FinderController):
        self.app = app
        self.dialog = tk.Toplevel(app.root)
        self.dialog.title("Usenet Finder (experimental)")
        self.dialog.geometry("900x650")
        self.dialog.minsize(680, 480)
        self.dialog.resizable(True, True)
        self.dialog.configure(background=app.theme_colors["background"])
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._request_running = False
        self._has_more = False
        self._offset = 0
        self._results: dict[str, FinderResult] = {}
        self._files: dict[str, FinderFile] = {}
        self.client = UsenetFinderClient(app.usenet_browser)
        self.state = UsenetFinderState(
            app.secure_store,
            cache_duration_hours=lambda: app.usenet_finder_cache_duration_hours,
        )
        self._poll_id: str | None = None
        self._build_ui()
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

        search_row = ttk.Frame(frame)
        search_row.grid(row=1, column=0, sticky="ew")
        search_row.columnconfigure(1, weight=1)
        ttk.Label(search_row, text="Search").grid(row=0, column=0, padx=(0, 8))
        self.query = tk.StringVar(master=self.dialog)
        self.query_entry = ttk.Entry(search_row, textvariable=self.query)
        self.query_entry.grid(row=0, column=1, sticky="ew")
        self.query_entry.bind("<Return>", lambda _event: self.search())
        ttk.Label(search_row, text="Category").grid(row=0, column=2, padx=(12, 8))
        self.category = tk.StringVar(master=self.dialog)
        ttk.Entry(search_row, textvariable=self.category, width=14).grid(
            row=0,
            column=3,
        )
        self.search_button = ttk.Button(search_row, text="Search", command=self.search)
        self.search_button.grid(row=0, column=4, padx=(8, 0))

        ttk.Label(
            frame,
            text=(
                "Complete any Cloudflare check and sign in in the dedicated Chrome profile, "
                "then return here to search. Keep this Chrome window open while using Finder; "
                "it provides the signed-in session. Resolved links can be added to the queue."
            ),
            wraplength=850,
        ).grid(row=2, column=0, sticky="w", pady=(6, 10))

        self.results = ttk.Treeview(
            frame,
            columns=("title", "category", "size", "date"),
            show="headings",
            selectmode="browse",
        )
        for column, label, width in (
            ("title", "Title", 470),
            ("category", "Category", 100),
            ("size", "Size", 100),
            ("date", "Date", 150),
        ):
            self.results.heading(column, text=label)
            self.results.column(column, width=width, anchor="w")
        self.results.grid(row=4, column=0, sticky="nsew")
        results_scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.results.yview)
        results_scrollbar.grid(row=4, column=1, sticky="ns")
        self.results.configure(yscrollcommand=results_scrollbar.set)
        self.results.bind("<<TreeviewSelect>>", self._selection_changed)
        self.results.bind("<Double-1>", lambda _event: self.resolve_selected())

        result_actions = ttk.Frame(frame)
        result_actions.grid(row=5, column=0, sticky="ew", pady=(8, 8))
        self.resolve_button = ttk.Button(
            result_actions,
            text="Refresh selected",
            command=self.resolve_selected,
            state="disabled",
        )
        self.resolve_button.pack(side="left")
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

        ttk.Label(frame, text="Resolved files").grid(
            row=6,
            column=0,
            sticky="sw",
            pady=(0, 4),
        )
        self.files = ttk.Treeview(
            frame,
            columns=("name", "size", "availability"),
            show="headings",
            selectmode="extended",
            height=6,
        )
        for column, label, width in (
            ("name", "File name", 570),
            ("size", "Size", 120),
            ("availability", "Availability", 150),
        ):
            self.files.heading(column, text=label)
            self.files.column(column, width=width, anchor="w")
        self.files.grid(row=7, column=0, sticky="nsew")
        files_scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.files.yview)
        files_scrollbar.grid(row=7, column=1, sticky="ns")
        self.files.configure(yscrollcommand=files_scrollbar.set)
        self.files.bind("<<TreeviewSelect>>", self._file_selection_changed)
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

    def open_browser(self) -> bool:
        try:
            self.app.usenet_browser.open()
        except UsenetFinderError as error:
            self.status.set(str(error))
            return False
        self.status.set(
            "In the dedicated Chrome window, complete any Cloudflare check and sign in; "
            "then return and search."
        )
        return True

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
            except (SecureStorageError, sqlite3.Error) as error:
                self.app._log(f"Could not save Usenet Finder search settings: {error}")
        self._offset = 0
        self._results.clear()
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
            page = self.state.get_search_page(query, category, 0, self.PAGE_SIZE)
        except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
            self.app._log(f"Could not restore cached Usenet Finder search results: {error}")
            return
        if page is None:
            return
        self._show_search_page(page, append=False)
        self.status.set(
            f"Restored {len(self._results)} cached result(s); "
            f"cached for {self.state.cache_duration_hours_value()} hour(s)."
        )

    def _search_page(self, *, append: bool) -> None:
        query = self.query.get().strip()
        category = self.category.get().strip()
        if not query:
            self.status.set("Enter a search query.")
            return
        offset = self._offset
        try:
            page = self.state.get_search_page(query, category, offset, self.PAGE_SIZE)
        except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
            self.app._log(f"Could not read Usenet Finder search cache: {error}")
            page = None
        if page is not None:
            self._show_search_page(page, append)
            self.status.set(
                f"Loaded {len(self._results)} cached result(s); "
                f"cached for {self.state.cache_duration_hours_value()} hour(s)."
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
            package = self.state.get_resolved(result.token)
        except (UsenetFinderStateError, SecureStorageError, sqlite3.Error) as error:
            self.app._log(f"Could not read Usenet Finder resolved-link cache: {error}")
            package = None
        if package is not None:
            self._show_package(package)
            self.status.set(
                f"Loaded saved resolution ({len(package.files)} file(s)); "
                f"cached for {self.state.cache_duration_hours_value()} hour(s)."
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
        self.resolve_button.configure(state="disabled")
        self.more_button.configure(state="disabled")
        self.add_selected_button.configure(state="disabled")
        self.add_accessible_button.configure(state="disabled")
        self.status.set(message)

    def _selection_changed(self, _event: tk.Event) -> None:
        if self._request_running:
            return
        selected = bool(self.results.selection())
        self.resolve_button.configure(state="normal" if selected else "disabled")
        if selected:
            self.resolve_selected()

    def _process_events(self) -> None:
        if not self.dialog.winfo_exists():
            return
        while True:
            try:
                event, payload = self._events.get_nowait()
            except queue.Empty:
                break
            self._request_running = False
            self.search_button.configure(state="normal")
            self.resolve_button.configure(
                state="normal" if self.results.selection() else "disabled"
            )
            self.more_button.configure(
                state="normal" if self._has_more else "disabled"
            )
            self._update_file_actions()
            if event == "error":
                self.status.set(f"Finder request failed: {payload}")
                continue
            if event == "search":
                page, append = payload
                self._show_search_page(page, append)
            elif event == "resolve":
                self._show_package(payload)
        self._poll_id = self.dialog.after(100, self._process_events)

    def _show_search_page(self, page: FinderSearchPage, append: bool) -> None:
        if not append:
            self.results.delete(*self.results.get_children())
            self._results.clear()
            self._offset = 0
        for result in page.results:
            item_id = self.results.insert(
                "",
                "end",
                values=(result.title, result.category, result.size, result.date),
            )
            self._results[item_id] = result
        self._offset += len(page.results)
        self._has_more = page.has_more
        self.more_button.configure(
            state="normal" if page.has_more and page.results else "disabled"
        )
        self.status.set(
            f"Loaded {len(self._results)} result(s). "
            f"{'More results available.' if page.has_more else 'No more results.'}"
        )

    def _show_package(self, package: FinderPackage) -> None:
        self.files.delete(*self.files.get_children())
        self._files.clear()
        for file in package.files:
            item_id = self.files.insert(
                "",
                "end",
                values=(file.name, file.size, self._availability(file)),
            )
            self._files[item_id] = file
        package_label = f" ({package.name})" if package.name else ""
        accessible_count = sum(file.is_accessible for file in package.files)
        self.status.set(
            f"Resolved{package_label}: {len(package.files)} file(s); "
            f"{accessible_count} accessible."
        )
        self._update_file_actions()

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
            [(file.link, file.name) for file in files]
        )
        self.status.set(
            f"Added {added} file(s) to the download queue; skipped {duplicates} "
            f"duplicate(s) and {invalid} invalid link(s). Click Start to download."
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
