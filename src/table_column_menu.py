from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import ttk


def show_column_context_menu(
    table: ttk.Treeview,
    event: tk.Event,
    order: list[str],
    visible: list[str],
    on_change: Callable[[list[str], list[str]], object],
) -> str | None:
    if table.identify_region(event.x, event.y) != "heading":
        return None

    display_columns = list(table["displaycolumns"])
    try:
        column_index = int(table.identify_column(event.x).removeprefix("#")) - 1
        column = display_columns[column_index]
    except (ValueError, IndexError):
        return "break"

    def move_column(direction: int) -> None:
        position = visible.index(column)
        destination = position + direction
        if not 0 <= destination < len(visible):
            return
        new_order = list(order)
        new_visible = list(visible)
        other_column = visible[destination]
        column_position = new_order.index(column)
        other_position = new_order.index(other_column)
        new_order[column_position], new_order[other_position] = (
            new_order[other_position],
            new_order[column_position],
        )
        new_visible[position], new_visible[destination] = (
            new_visible[destination],
            new_visible[position],
        )
        on_change(new_order, new_visible)

    def hide_column() -> None:
        if column not in visible or len(visible) <= 1:
            return
        new_visible = list(visible)
        new_visible.remove(column)
        on_change(list(order), new_visible)

    menu = tk.Menu(table, tearoff=False)
    menu.add_command(
        label="Move left",
        command=lambda: move_column(-1),
        state="normal" if column_index > 0 else "disabled",
    )
    menu.add_command(
        label="Move right",
        command=lambda: move_column(1),
        state="normal" if column_index < len(display_columns) - 1 else "disabled",
    )
    menu.add_separator()
    menu.add_command(
        label="Hide column",
        command=hide_column,
        state="normal" if len(display_columns) > 1 else "disabled",
    )
    table._column_context_menu = menu
    menu.tk_popup(event.x_root, event.y_root)
    return "break"
