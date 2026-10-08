from __future__ import annotations

import sqlite3
import tkinter as tk
from tkinter import ttk
from typing import Protocol

from .usenet_finder_state import (
    DEFAULT_CACHE_DURATION_HOURS,
    MAX_CACHE_DURATION_HOURS,
    MIN_CACHE_DURATION_HOURS,
    valid_cache_duration_hours,
)


class SettingsController(Protocol):
    root: tk.Tk
    theme_colors: dict[str, str]
    api_key: tk.StringVar
    output_dir_text: tk.StringVar
    auto_start_downloads: tk.BooleanVar
    auto_check_updates: tk.BooleanVar
    dark_theme: bool
    usenet_finder_cache_duration_hours: int

    def _log(self, message: str) -> None: ...
    def _open_api_key_dashboard(self) -> None: ...
    def _check_api_key(self, button: ttk.Button, status: tk.StringVar) -> None: ...
    def _choose_output_folder(self) -> None: ...
    def _persist_theme(self) -> None: ...
    def _apply_theme(self) -> None: ...
    def _persist_auto_start_downloads(self) -> None: ...
    def _persist_auto_check_updates(self) -> None: ...
    def _set_usenet_finder_cache_duration(self, hours: int) -> None: ...
    def _clear_usenet_finder_cache(self) -> None: ...
    def _show_columns_dialog(self) -> None: ...


class SettingsDialog:
    def __init__(
        self,
        app: SettingsController,
        initial_filter: str = "",
        api_key_notice: str | None = None,
    ):
        self.app = app
        self.dialog = tk.Toplevel(app.root)
        self.dialog.title("Settings")
        self.dialog.geometry("720x560")
        self.dialog.minsize(580, 420)
        self.dialog.resizable(True, True)
        self.dialog.configure(background=app.theme_colors["background"])

        frame = ttk.Frame(self.dialog, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        filter_var = tk.StringVar(master=self.dialog)
        filter_row = ttk.Frame(frame)
        filter_row.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        filter_row.columnconfigure(1, weight=1)
        ttk.Label(filter_row, text="Filter settings").grid(row=0, column=0, padx=(0, 8))
        filter_entry = ttk.Entry(filter_row, textvariable=filter_var)
        filter_entry.grid(row=0, column=1, sticky="ew")

        ttk.Separator(frame, orient="horizontal").grid(
            row=1,
            column=0,
            sticky="ew",
            pady=(0, 10),
        )
        body = ttk.Frame(frame)
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            body,
            background=app.theme_colors["background"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        settings_list = ttk.Frame(self.canvas)
        settings_window = self.canvas.create_window((0, 0), window=settings_list, anchor="nw")
        settings_list.columnconfigure(0, weight=1)
        settings_list.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda event: self.canvas.itemconfigure(settings_window, width=event.width),
        )
        settings_rows: list[tuple[ttk.Frame, str]] = []

        def add_setting(title: str, keywords: str, build_controls) -> None:
            row = ttk.Frame(settings_list, padding=(0, 6))
            row.columnconfigure(1, weight=1)
            row.grid(row=len(settings_rows), column=0, sticky="ew")
            ttk.Label(row, text=title).grid(row=0, column=0, sticky="nw", padx=(0, 12))
            controls = ttk.Frame(row)
            controls.grid(row=0, column=1, sticky="ew")
            controls.columnconfigure(0, weight=1)
            build_controls(controls)
            ttk.Separator(row, orient="horizontal").grid(
                row=1,
                column=0,
                columnspan=2,
                sticky="ew",
                pady=(8, 0),
            )
            settings_rows.append((row, f"{title} {keywords}".casefold()))

        no_matches = ttk.Label(settings_list, text="No matching settings")

        def build_api_key(controls: ttk.Frame) -> None:
            controls.columnconfigure(0, weight=1)
            api_key_entry = ttk.Entry(controls, textvariable=app.api_key, show="*")
            api_key_entry.grid(row=0, column=0, sticky="ew")

            def toggle_visibility() -> None:
                visible = api_key_entry.cget("show") == "*"
                api_key_entry.configure(show="" if visible else "*")
                visibility_button.configure(text="Hide" if visible else "Show")

            visibility_button = ttk.Button(
                controls,
                text="Show",
                command=toggle_visibility,
            )
            visibility_button.grid(row=0, column=1, padx=(8, 0))
            ttk.Button(
                controls,
                text="Get API key",
                command=app._open_api_key_dashboard,
            ).grid(row=0, column=2, padx=(8, 0))
            key_actions = ttk.Frame(controls)
            key_actions.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 0))
            check_status = tk.StringVar(master=self.dialog, value=api_key_notice or "")
            check_button = ttk.Button(
                key_actions,
                text="Check API key",
                command=lambda: app._check_api_key(check_button, check_status),
            )
            check_button.pack(side="left")
            check_status_label = ttk.Label(key_actions, textvariable=check_status)
            check_status_label.pack(side="left", padx=(8, 0))

            def update_check_status_style(*_args: str) -> None:
                message = check_status.get()
                is_warning = bool(message) and message not in {
                    "API key is valid.",
                    "Checking API key...",
                }
                check_status_label.configure(
                    style="ApiKeyWarning.TLabel" if is_warning else "TLabel"
                )

            check_status.trace_add("write", update_check_status_style)
            update_check_status_style()

        def build_download_folder(controls: ttk.Frame) -> None:
            controls.columnconfigure(0, weight=1)
            ttk.Entry(controls, textvariable=app.output_dir_text, state="readonly").grid(
                row=0,
                column=0,
                sticky="ew",
            )
            ttk.Button(
                controls,
                text="Browse...",
                command=app._choose_output_folder,
            ).grid(row=0, column=1, padx=(8, 0))

        dark_mode_setting = tk.BooleanVar(master=self.dialog, value=app.dark_theme)

        def update_theme() -> None:
            app.dark_theme = dark_mode_setting.get()
            app._persist_theme()
            app._apply_theme()
            self.dialog.configure(background=app.theme_colors["background"])
            self.canvas.configure(background=app.theme_colors["background"])

        add_setting("API key", "credentials token secret", build_api_key)
        add_setting("Download folder", "location directory output path", build_download_folder)

        def build_appearance(controls: ttk.Frame) -> None:
            ttk.Checkbutton(
                controls,
                text="Use dark mode",
                variable=dark_mode_setting,
                command=update_theme,
            ).pack(anchor="w")

        add_setting("Appearance", "theme dark light colors", build_appearance)

        def build_auto_start(controls: ttk.Frame) -> None:
            ttk.Checkbutton(
                controls,
                text="Start queued downloads when the app opens",
                variable=app.auto_start_downloads,
                command=app._persist_auto_start_downloads,
            ).pack(anchor="w")

        add_setting("Startup downloads", "start download queue launch automatic", build_auto_start)

        def build_auto_updates(controls: ttk.Frame) -> None:
            ttk.Checkbutton(
                controls,
                text="Check for updates when the app opens",
                variable=app.auto_check_updates,
                command=app._persist_auto_check_updates,
            ).pack(anchor="w")

        add_setting("Startup updates", "check updates releases launch", build_auto_updates)

        cache_duration = tk.StringVar(
            master=self.dialog,
            value=str(
                valid_cache_duration_hours(
                    getattr(
                        app,
                        "usenet_finder_cache_duration_hours",
                        DEFAULT_CACHE_DURATION_HOURS,
                    )
                )
            ),
        )
        cache_status = tk.StringVar(master=self.dialog)

        def clear_finder_cache() -> None:
            try:
                app._clear_usenet_finder_cache()
            except (OSError, sqlite3.Error) as error:
                app._log(f"Could not clear Usenet Finder cache: {error}")
                cache_status.set(f"Could not clear cache: {error}")
                return
            app._log("Cleared Usenet Finder search and resolved-link caches.")
            cache_status.set("Cache cleared.")

        def save_cache_duration(*_args: object) -> None:
            try:
                hours = valid_cache_duration_hours(cache_duration.get())
                app._set_usenet_finder_cache_duration(hours)
            except (ValueError, OSError, sqlite3.Error) as error:
                cache_status.set(str(error))
                return
            cache_duration.set(str(hours))
            cache_status.set("Saved.")

        def build_finder_cache(controls: ttk.Frame) -> None:
            ttk.Label(controls, text="Keep search results and resolved links for").grid(
                row=0,
                column=0,
                sticky="w",
            )
            duration = ttk.Spinbox(
                controls,
                from_=MIN_CACHE_DURATION_HOURS,
                to=MAX_CACHE_DURATION_HOURS,
                increment=1,
                textvariable=cache_duration,
                width=7,
                command=save_cache_duration,
            )
            duration.grid(row=0, column=1, padx=(8, 4), sticky="w")
            ttk.Label(controls, text="hours (1-720)").grid(row=0, column=2, sticky="w")
            ttk.Label(controls, textvariable=cache_status).grid(
                row=1,
                column=0,
                columnspan=3,
                sticky="w",
                pady=(4, 0),
            )
            ttk.Button(
                controls,
                text="Clear cached search results and resolved links",
                command=clear_finder_cache,
            ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
            duration.bind("<Return>", save_cache_duration)
            duration.bind("<FocusOut>", save_cache_duration)

        add_setting(
            "Usenet Finder cache",
            "usenet usenet finder search resolved links cache duration expiry clear",
            build_finder_cache,
        )

        def build_columns(controls: ttk.Frame) -> None:
            ttk.Button(
                controls,
                text="Change column visibility and order",
                command=app._show_columns_dialog,
            ).pack(anchor="w")

        add_setting(
            "Configure columns",
            "columns table visibility order file name original link host host status downloaded total remaining eta status verification",
            build_columns,
        )

        def filter_settings(*_args: str) -> None:
            query = filter_var.get().strip().casefold()
            visible_index = 0
            for row, searchable_text in settings_rows:
                if not query or query in searchable_text:
                    row.grid(row=visible_index, column=0, sticky="ew")
                    visible_index += 1
                else:
                    row.grid_remove()
            if visible_index:
                no_matches.grid_remove()
            else:
                no_matches.grid(row=0, column=0, sticky="w", pady=8)

        filter_var.trace_add("write", filter_settings)
        filter_var.set(initial_filter)
        filter_settings()
        if initial_filter:
            filter_entry.focus_set()

        ttk.Button(frame, text="Close", command=self.dialog.destroy).grid(
            row=3,
            column=0,
            sticky="e",
            pady=(10, 0),
        )
        self.dialog.bind("<Escape>", lambda _event: self.dialog.destroy())
        self.dialog.grab_set()
