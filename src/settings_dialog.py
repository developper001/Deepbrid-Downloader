from __future__ import annotations

import sqlite3
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Protocol

from .secure_store import SecureStorageError
from .startup_manager import StartupManager
from .usenet_finder_state import (
    DEFAULT_CACHE_DURATION_HOURS,
    MAX_CACHE_DURATION_HOURS,
    MIN_CACHE_DURATION_HOURS,
    valid_cache_duration_hours,
)

SETTINGS_CATEGORIES = (
    "Main",
    "Usenet Finder",
    "Hosts",
    "Startup & Updates",
    "Appearance",
    "Logging",
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
    usenet_finder_hide_auxiliary_files: bool
    usenet_auto_open_browser_for_link_renewal: bool
    append_output_log_enabled: bool
    append_output_log_path: tk.StringVar
    startup_manager: StartupManager

    def _log(self, message: str) -> None: ...
    def _show_readme(self) -> None: ...
    def _open_api_key_dashboard(self) -> None: ...
    def _check_api_key(self, button: ttk.Button, status: tk.StringVar) -> None: ...
    def _choose_output_folder(self) -> None: ...
    def _persist_theme(self) -> None: ...
    def _apply_theme(self) -> None: ...
    def _persist_auto_start_downloads(self) -> None: ...
    def _configure_startup(self, button: ttk.Button, status: tk.StringVar) -> None: ...
    def _persist_auto_check_updates(self) -> None: ...
    def _set_usenet_finder_cache_duration(self, hours: int) -> None: ...
    def _set_usenet_finder_hide_auxiliary_files(self, enabled: bool) -> None: ...
    def _set_usenet_auto_open_browser_for_link_renewal(self, enabled: bool) -> None: ...
    def _clear_usenet_finder_cache(self) -> None: ...
    def _clear_usenet_finder_search_history(self) -> None: ...
    def _show_columns_dialog(self) -> None: ...
    def _show_usenet_column_settings(self, table_name: str, parent: tk.Misc) -> None: ...
    def _show_host_column_settings(self, parent: tk.Misc) -> None: ...
    def _refresh_hosts(self, show_popup: bool = False) -> None: ...
    def _set_append_output_log(self, enabled: bool, path: str) -> None: ...
    def _test_usenet_browser_connection(
        self,
        status: tk.StringVar,
        button: ttk.Button,
    ) -> None: ...


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
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        navigation = ttk.Frame(body, padding=(0, 0, 10, 0))
        navigation.grid(row=0, column=0, sticky="ns")
        ttk.Label(navigation, text="Sections").pack(anchor="w", pady=(0, 6))
        selected_category = tk.StringVar(master=self.dialog, value="Main")
        settings_rows: list[tuple[ttk.Frame, str, str]] = []
        category_headers: dict[str, ttk.Label] = {}
        self.canvas = tk.Canvas(
            body,
            background=app.theme_colors["background"],
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.grid(row=0, column=1, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=2, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        settings_list = ttk.Frame(self.canvas)
        settings_window = self.canvas.create_window((0, 0), window=settings_list, anchor="nw")
        settings_list.columnconfigure(0, weight=1)
        no_matches = ttk.Label(settings_list, text="No matching settings")
        section_title = ttk.Label(settings_list, font=("TkDefaultFont", 12, "bold"))
        section_title.grid(row=0, column=0, sticky="w", pady=(0, 10))
        settings_list.bind(
            "<Configure>",
            lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.bind(
            "<Configure>",
            lambda event: self.canvas.itemconfigure(settings_window, width=event.width),
        )

        def scroll_settings(event: tk.Event) -> str:
            if getattr(event, "delta", 0):
                units = int(-event.delta / 48)
                if not units:
                    units = -1 if event.delta > 0 else 1
            else:
                units = {4: -1, 5: 1}.get(getattr(event, "num", None), 0)
            if units:
                self.canvas.yview_scroll(units, "units")
            return "break"

        self.dialog.bind("<MouseWheel>", scroll_settings, add="+")
        self.dialog.bind("<Button-4>", scroll_settings, add="+")
        self.dialog.bind("<Button-5>", scroll_settings, add="+")

        def add_setting(
            title: str,
            keywords: str,
            build_controls,
            *,
            category: str,
        ) -> None:
            row = ttk.Frame(settings_list, padding=(0, 6))
            row.columnconfigure(1, weight=1)
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
            settings_rows.append(
                (row, category, f"{category} {title} {keywords}".casefold())
            )

        def render_settings(*_args: str) -> None:
            query = filter_var.get().strip().casefold()
            selected = selected_category.get()
            for setting_row, _row_category, _searchable_text in settings_rows:
                setting_row.grid_remove()
            for header in category_headers.values():
                header.grid_remove()
            no_matches.grid_remove()
            if selected == "All settings":
                categories = SETTINGS_CATEGORIES
                section_title.configure(text="All settings")
            else:
                categories = (selected,)
                section_title.configure(text=selected)
            row_index = 1
            matches = 0
            for category in categories:
                category_rows = [
                    row
                    for row, row_category, searchable_text in settings_rows
                    if row_category == category
                    and (not query or query in searchable_text)
                ]
                header = category_headers.get(category)
                if category_rows and selected == "All settings":
                    if header is None:
                        header = ttk.Label(
                            settings_list,
                            text=category,
                            font=("TkDefaultFont", 10, "bold"),
                        )
                        category_headers[category] = header
                    header.grid(row=row_index, column=0, sticky="ew", pady=(8, 2))
                    row_index += 1
                for setting_row in category_rows:
                    setting_row.grid(row=row_index, column=0, sticky="ew")
                    row_index += 1
                    matches += 1
            if not matches:
                no_matches.grid(row=1, column=0, sticky="w", pady=8)
            self.canvas.yview_moveto(0)

        def filter_settings(*_args: str) -> None:
            selected_category.set("All settings")
            render_settings()

        ttk.Radiobutton(
            navigation,
            text="All settings",
            value="All settings",
            variable=selected_category,
            command=render_settings,
        ).pack(anchor="w", fill="x")
        for category in SETTINGS_CATEGORIES:
            ttk.Radiobutton(
                navigation,
                text=category,
                value=category,
                variable=selected_category,
                command=render_settings,
            ).pack(anchor="w", fill="x")

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

        add_setting(
            "API key",
            "credentials token secret",
            build_api_key,
            category="Main",
        )
        add_setting(
            "Download folder",
            "location directory output path",
            build_download_folder,
            category="Main",
        )

        def build_host_status(controls: ttk.Frame) -> None:
            ttk.Button(
                controls,
                text="Host status",
                command=self._show_host_status,
            ).pack(anchor="w")

        add_setting(
            "Host status",
            "supported hosts availability host status daily quota refresh",
            build_host_status,
            category="Hosts",
        )

        def build_host_columns(controls: ttk.Frame) -> None:
            ttk.Label(
                controls,
                text="Choose visible columns and their order in the Host Status table.",
                wraplength=480,
            ).pack(anchor="w", pady=(0, 6))
            ttk.Button(
                controls,
                text="Configure Host Status columns",
                command=lambda: app._show_host_column_settings(self.dialog),
            ).pack(anchor="w")

        add_setting(
            "Host Status columns",
            "host status availability daily remaining quota columns visibility order",
            build_host_columns,
            category="Hosts",
        )

        def build_appearance(controls: ttk.Frame) -> None:
            ttk.Checkbutton(
                controls,
                text="Use dark mode",
                variable=dark_mode_setting,
                command=update_theme,
            ).pack(anchor="w")

        add_setting(
            "Appearance",
            "theme dark light colors",
            build_appearance,
            category="Appearance",
        )

        startup_status = tk.StringVar(master=self.dialog)

        def build_startup_login(controls: ttk.Frame) -> None:
            startup_manager = app.startup_manager
            if startup_manager.platform_name == "darwin":
                button_text = "Open Login Items settings"
            else:
                button_text = (
                    "Disable auto-start"
                    if startup_manager.is_enabled()
                    else "Enable auto-start"
                )
            startup_button = ttk.Button(
                controls,
                text=button_text,
                command=lambda: app._configure_startup(startup_button, startup_status),
            )
            startup_button.pack(side="left")
            ttk.Label(controls, textvariable=startup_status).pack(
                side="left",
                padx=(8, 0),
            )

        add_setting(
            "Start after login",
            "startup login automatically launch open computer boot",
            build_startup_login,
            category="Startup & Updates",
        )

        def build_auto_start(controls: ttk.Frame) -> None:
            ttk.Checkbutton(
                controls,
                text="Start queued downloads when the app opens",
                variable=app.auto_start_downloads,
                command=app._persist_auto_start_downloads,
            ).pack(anchor="w")

        add_setting(
            "Startup downloads",
            "start download queue launch automatic",
            build_auto_start,
            category="Startup & Updates",
        )

        def build_auto_updates(controls: ttk.Frame) -> None:
            ttk.Checkbutton(
                controls,
                text="Check for updates when the app opens",
                variable=app.auto_check_updates,
                command=app._persist_auto_check_updates,
            ).pack(anchor="w")

        add_setting(
            "Startup updates",
            "check updates releases launch",
            build_auto_updates,
            category="Startup & Updates",
        )

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
            except (OSError, SecureStorageError, sqlite3.Error) as error:
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
            category="Usenet Finder",
        )

        hide_auxiliary_files = tk.BooleanVar(
            master=self.dialog,
            value=app.usenet_finder_hide_auxiliary_files,
        )
        auxiliary_files_status = tk.StringVar(master=self.dialog)

        def save_auxiliary_file_filter() -> None:
            enabled = hide_auxiliary_files.get()
            try:
                app._set_usenet_finder_hide_auxiliary_files(enabled)
            except (OSError, SecureStorageError, sqlite3.Error) as error:
                hide_auxiliary_files.set(app.usenet_finder_hide_auxiliary_files)
                auxiliary_files_status.set(f"Could not save setting: {error}")
                app._log(f"Could not save Usenet resolved-file filter setting: {error}")
                return
            auxiliary_files_status.set("Saved.")

        def build_auxiliary_file_filter(controls: ttk.Frame) -> None:
            ttk.Checkbutton(
                controls,
                text="Hide PAR2 and NFO files in resolved results",
                variable=hide_auxiliary_files,
                command=save_auxiliary_file_filter,
            ).pack(anchor="w")
            ttk.Label(controls, textvariable=auxiliary_files_status).pack(
                anchor="w",
                pady=(4, 0),
            )

        add_setting(
            "Usenet resolved files",
            "usenet finder hide par2 nfo auxiliary files filter resolved results",
            build_auxiliary_file_filter,
            category="Usenet Finder",
        )

        history_status = tk.StringVar(master=self.dialog)

        def clear_finder_history() -> None:
            try:
                app._clear_usenet_finder_search_history()
            except (OSError, SecureStorageError, sqlite3.Error) as error:
                app._log(f"Could not clear Usenet Finder search history: {error}")
                history_status.set(f"Could not clear search history: {error}")
                return
            history_status.set("Search history cleared.")

        def build_finder_history(controls: ttk.Frame) -> None:
            ttk.Button(
                controls,
                text="Clear search history",
                command=clear_finder_history,
            ).pack(side="left")
            ttk.Label(controls, textvariable=history_status).pack(
                side="left",
                padx=(8, 0),
            )

        add_setting(
            "Usenet Finder search history",
            "usenet finder recent searches query history clear cleanup",
            build_finder_history,
            category="Usenet Finder",
        )

        browser_test_status = tk.StringVar(master=self.dialog)

        def build_browser_connection_test(controls: ttk.Frame) -> None:
            test_button = ttk.Button(
                controls,
                text="Test browser connection",
                command=lambda: app._test_usenet_browser_connection(
                    browser_test_status,
                    test_button,
                ),
            )
            test_button.pack(side="left")
            ttk.Label(controls, textvariable=browser_test_status).pack(
                side="left",
                padx=(8, 0),
            )

        add_setting(
            "Usenet browser",
            "usenet finder chrome edge browser connection test",
            build_browser_connection_test,
            category="Usenet Finder",
        )

        auto_open_usenet_browser = tk.BooleanVar(
            master=self.dialog,
            value=app.usenet_auto_open_browser_for_link_renewal,
        )
        auto_open_usenet_browser_status = tk.StringVar(master=self.dialog)

        def save_auto_open_usenet_browser() -> None:
            enabled = auto_open_usenet_browser.get()
            try:
                app._set_usenet_auto_open_browser_for_link_renewal(enabled)
            except (OSError, sqlite3.Error) as error:
                auto_open_usenet_browser.set(
                    app.usenet_auto_open_browser_for_link_renewal
                )
                auto_open_usenet_browser_status.set(f"Could not save setting: {error}")
                app._log(f"Could not save automatic Usenet browser setting: {error}")
                return
            auto_open_usenet_browser_status.set("Saved.")

        def build_auto_open_usenet_browser(controls: ttk.Frame) -> None:
            ttk.Checkbutton(
                controls,
                text="Open the dedicated browser when a queued Usenet link needs renewal",
                variable=auto_open_usenet_browser,
                command=save_auto_open_usenet_browser,
            ).pack(anchor="w")
            ttk.Label(
                controls,
                text=(
                    "This opens the app's separate Chrome/Edge profile only when an expired "
                    "queued link needs a fresh Finder link. Sign in and save your password in "
                    "that dedicated profile; credentials saved in your regular browser are "
                    "not shared. You may still need to complete Cloudflare verification."
                ),
                wraplength=480,
                justify="left",
            ).pack(anchor="w", pady=(4, 0))
            ttk.Label(controls, textvariable=auto_open_usenet_browser_status).pack(
                anchor="w",
                pady=(4, 0),
            )

        add_setting(
            "Usenet link renewal",
            "usenet queued expired link renew reopen chrome edge browser automatically",
            build_auto_open_usenet_browser,
            category="Usenet Finder",
        )

        output_log_enabled = tk.BooleanVar(
            master=self.dialog,
            value=app.append_output_log_enabled,
        )
        output_log_path = tk.StringVar(
            master=self.dialog,
            value=str(app.append_output_log_path.get()),
        )
        output_log_status = tk.StringVar(master=self.dialog)

        def save_output_log() -> None:
            try:
                app._set_append_output_log(output_log_enabled.get(), output_log_path.get())
            except (OSError, ValueError, sqlite3.Error) as error:
                output_log_enabled.set(app.append_output_log_enabled)
                output_log_path.set(str(app.append_output_log_path.get()))
                output_log_status.set(f"Could not save output log setting: {error}")
                return
            output_log_status.set(
                "Output is appended to the selected file."
                if output_log_enabled.get()
                else "Output logging is disabled."
            )

        def browse_output_log() -> None:
            selected = filedialog.asksaveasfilename(
                parent=self.dialog,
                title="Choose output log file",
                initialfile=output_log_path.get() or "deepbrid-output.log",
                defaultextension=".log",
                filetypes=(("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*")),
            )
            if selected:
                output_log_path.set(selected)
                save_output_log()

        def build_output_log(controls: ttk.Frame) -> None:
            ttk.Checkbutton(
                controls,
                text="Append application output to a file",
                variable=output_log_enabled,
                command=save_output_log,
            ).grid(row=0, column=0, columnspan=2, sticky="w")
            ttk.Entry(controls, textvariable=output_log_path, state="readonly").grid(
                row=1,
                column=0,
                sticky="ew",
                pady=(6, 0),
            )
            ttk.Button(controls, text="Browse...", command=browse_output_log).grid(
                row=1,
                column=1,
                padx=(8, 0),
                pady=(6, 0),
            )
            ttk.Label(controls, textvariable=output_log_status).grid(
                row=2,
                column=0,
                columnspan=2,
                sticky="w",
                pady=(4, 0),
            )

        add_setting(
            "Output log file",
            "logging output append file diagnostics long term",
            build_output_log,
            category="Logging",
        )

        def build_queue_columns(controls: ttk.Frame) -> None:
            ttk.Label(
                controls,
                text="Choose which columns appear and their order in the main download queue.",
                wraplength=480,
            ).pack(anchor="w", pady=(0, 6))
            ttk.Button(
                controls,
                text="Configure download queue columns",
                command=app._show_columns_dialog,
            ).pack(anchor="w")

        add_setting(
            "Main download queue columns",
            "columns table visibility order file name original link host host status downloaded total remaining eta status verification",
            build_queue_columns,
            category="Main",
        )

        def build_usenet_search_columns(controls: ttk.Frame) -> None:
            ttk.Label(
                controls,
                text="Choose visible columns and their order in Usenet Finder search results.",
                wraplength=480,
            ).pack(anchor="w", pady=(0, 6))
            ttk.Button(
                controls,
                text="Configure Usenet search-result columns",
                command=lambda: app._show_usenet_column_settings("search", self.dialog),
            ).pack(anchor="w")

        add_setting(
            "Usenet Finder search-result columns",
            "usenet finder search results title category size date columns visibility order",
            build_usenet_search_columns,
            category="Usenet Finder",
        )

        def build_usenet_file_columns(controls: ttk.Frame) -> None:
            ttk.Label(
                controls,
                text="Choose visible columns and their order in resolved Usenet files.",
                wraplength=480,
            ).pack(anchor="w", pady=(0, 6))
            ttk.Button(
                controls,
                text="Configure resolved-file columns",
                command=lambda: app._show_usenet_column_settings("files", self.dialog),
            ).pack(anchor="w")

        add_setting(
            "Usenet Finder resolved-file columns",
            "usenet finder resolved files name extension size availability columns visibility order",
            build_usenet_file_columns,
            category="Usenet Finder",
        )

        filter_var.trace_add("write", filter_settings)
        filter_var.set(initial_filter)
        if initial_filter:
            query = initial_filter.strip().casefold()
            matching_categories = [
                category
                for category in SETTINGS_CATEGORIES
                if any(
                    row_category == category and query in searchable_text
                    for _row, row_category, searchable_text in settings_rows
                )
            ]
            if matching_categories:
                selected_category.set(matching_categories[0])
        render_settings()
        def focus_filter() -> None:
            filter_entry.focus_set()
            filter_entry.selection_range(0, "end")

        self.dialog.after_idle(focus_filter)

        footer = ttk.Frame(frame)
        footer.grid(
            row=3,
            column=0,
            sticky="ew",
            pady=(10, 0),
        )
        ttk.Button(footer, text="About", command=app._show_readme).pack(side="left")
        ttk.Button(footer, text="Close", command=self.dialog.destroy).pack(side="right")
        self.dialog.bind("<Escape>", lambda _event: self.dialog.destroy())

    def _show_host_status(self) -> None:
        self.app._refresh_hosts(show_popup=True)
        popup = getattr(self.app, "hosts_popup", None)
        if popup is None:
            return
        try:
            popup.transient("")
            popup.lift()
        except tk.TclError:
            return
