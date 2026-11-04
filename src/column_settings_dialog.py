from __future__ import annotations

import json
import sqlite3
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Protocol

from .secure_store import SecureStorageError, SecureStore
from .usenet_finder_dialog import (
    DEFAULT_FILE_COLUMNS,
    DEFAULT_HOST_COLUMNS,
    DEFAULT_SEARCH_COLUMNS,
    FILE_COLUMNS,
    FILE_COLUMN_LABELS,
    FILE_COLUMN_MIN_WIDTHS,
    FILE_COLUMN_WIDTHS,
    HOST_COLUMNS,
    HOST_COLUMN_LABELS,
    HOST_COLUMN_MIN_WIDTHS,
    HOST_COLUMN_WIDTHS,
    SEARCH_COLUMNS,
    SEARCH_COLUMN_LABELS,
    SEARCH_COLUMN_MIN_WIDTHS,
    SEARCH_COLUMN_WIDTHS,
    load_table_column_layout,
    load_table_column_widths,
)


class ColumnSettingsController(Protocol):
    secure_store: SecureStore

    def _log(self, message: str) -> None: ...


def show_column_settings(
    app: ColumnSettingsController,
    parent: tk.Misc,
    table_name: str,
    *,
    table: ttk.Treeview | None = None,
    on_apply: Callable[[list[str], list[str]], None] | None = None,
) -> None:
    if table_name == "search":
        columns = SEARCH_COLUMNS
        labels = SEARCH_COLUMN_LABELS
        defaults = DEFAULT_SEARCH_COLUMNS
        order_setting = "usenet_search_column_order"
        visible_setting = "usenet_search_visible_columns"
        width_setting = "usenet_search_column_widths"
        width_defaults = SEARCH_COLUMN_WIDTHS
        width_minimums = SEARCH_COLUMN_MIN_WIDTHS
        title = "Usenet search-result columns"
    elif table_name == "files":
        columns = FILE_COLUMNS
        labels = FILE_COLUMN_LABELS
        defaults = DEFAULT_FILE_COLUMNS
        order_setting = "usenet_file_column_order"
        visible_setting = "usenet_file_visible_columns"
        width_setting = "usenet_file_column_widths"
        width_defaults = FILE_COLUMN_WIDTHS
        width_minimums = FILE_COLUMN_MIN_WIDTHS
        title = "Usenet resolved-file columns"
    elif table_name == "hosts":
        columns = HOST_COLUMNS
        labels = HOST_COLUMN_LABELS
        defaults = DEFAULT_HOST_COLUMNS
        order_setting = "host_status_column_order"
        visible_setting = "host_status_visible_columns"
        width_setting = "host_status_column_widths"
        width_defaults = HOST_COLUMN_WIDTHS
        width_minimums = HOST_COLUMN_MIN_WIDTHS
        title = "Host status columns"
    else:
        raise ValueError(f"Unknown column table: {table_name}")

    working_order, visible = load_table_column_layout(app.secure_store, table_name)
    working_visible = set(visible)
    working_widths = load_table_column_widths(app.secure_store, table_name)

    dialog = tk.Toplevel(parent)
    dialog.title(title)
    dialog.resizable(True, True)
    dialog.geometry("620x460")
    dialog.minsize(580, 420)
    frame = ttk.Frame(dialog, padding=12)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    column_tree = ttk.Treeview(
        frame,
        columns=("name", "visibility", "width"),
        show="headings",
        selectmode="browse",
        height=len(columns),
    )
    column_tree.heading("name", text="Column")
    column_tree.heading("visibility", text="Visibility")
    column_tree.heading("width", text="Width")
    column_tree.column("name", width=220, stretch=True)
    column_tree.column("visibility", width=110, minwidth=100, stretch=False, anchor="center")
    column_tree.column("width", width=70, minwidth=60, stretch=False, anchor="e")
    column_tree.grid(row=0, column=0, sticky="nsew")
    column_scrollbar = ttk.Scrollbar(
        frame,
        orient="vertical",
        command=column_tree.yview,
    )
    column_scrollbar.grid(row=0, column=1, sticky="ns")
    column_tree.configure(yscrollcommand=column_scrollbar.set)

    visible_variable = tk.BooleanVar(master=dialog)
    width_variable = tk.StringVar(master=dialog)

    def selected_column() -> str | None:
        selection = column_tree.selection()
        return selection[0] if selection else None

    def update_controls() -> None:
        column = selected_column()
        if column is None:
            visible_variable.set(False)
            width_variable.set("")
            visibility_check.configure(state="disabled")
            width_spinbox.configure(state="disabled")
            set_width_button.configure(state="disabled")
            move_up_button.configure(state="disabled")
            move_down_button.configure(state="disabled")
            return
        visible_variable.set(column in working_visible)
        width_variable.set(str(working_widths[column]))
        visibility_check.configure(state="normal")
        width_spinbox.configure(state="normal")
        set_width_button.configure(state="normal")
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
                    working_widths[column],
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
        working_visible.update(defaults)
        working_widths.clear()
        working_widths.update(width_defaults)
        render_order(columns[0])

    def apply_width() -> bool:
        column = selected_column()
        if column is None:
            return True
        try:
            width = int(width_variable.get())
        except ValueError:
            messagebox.showwarning(
                "Invalid column width",
                "Enter a whole number for the column width.",
                parent=dialog,
            )
            return False
        if not width_minimums[column] <= width <= 10000:
            messagebox.showwarning(
                "Invalid column width",
                f"Enter a width from {width_minimums[column]} to 10000.",
                parent=dialog,
            )
            return False
        working_widths[column] = width
        render_order(column)
        return True

    def apply_changes() -> None:
        if selected_column() is not None and not apply_width():
            return
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
            app.secure_store.set_setting(order_setting, json.dumps(working_order))
            app.secure_store.set_setting(visible_setting, json.dumps(ordered_visible))
            app.secure_store.set_setting(width_setting, json.dumps(working_widths))
        except (OSError, sqlite3.Error, SecureStorageError) as error:
            app._log(f"Could not save column settings: {error}")
            messagebox.showerror(
                "Could not save columns",
                str(error),
                parent=dialog,
            )
            return
        if table is not None:
            table.configure(displaycolumns=ordered_visible)
            for column, width in working_widths.items():
                table.column(column, width=width)
        if on_apply is not None:
            on_apply(list(working_order), ordered_visible)
        dialog.destroy()

    move_buttons = ttk.Frame(frame)
    move_buttons.grid(row=0, column=2, sticky="ns", padx=(8, 0))
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
    width_controls = ttk.Frame(frame)
    width_controls.grid(row=2, column=0, sticky="w", pady=(8, 0))
    ttk.Label(width_controls, text="Selected column width (pixels)").pack(side="left")
    width_spinbox = ttk.Spinbox(
        width_controls,
        from_=30,
        to=10000,
        textvariable=width_variable,
        width=8,
        state="disabled",
    )
    width_spinbox.pack(side="left", padx=(8, 4))
    set_width_button = ttk.Button(
        width_controls,
        text="Set width",
        command=apply_width,
        state="disabled",
    )
    set_width_button.pack(side="left")
    actions = ttk.Frame(frame)
    actions.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))
    ttk.Button(actions, text="Reset to defaults", command=reset_defaults).pack(side="left")
    ttk.Button(actions, text="Apply", command=apply_changes).pack(
        side="right",
        padx=(0, 8),
    )
    ttk.Button(actions, text="Cancel", command=dialog.destroy).pack(side="right")
    column_tree.bind("<<TreeviewSelect>>", lambda _event: update_controls())
    render_order(working_order[0] if working_order else None)
    dialog.bind("<Escape>", lambda _event: dialog.destroy())
    dialog.grab_set()
