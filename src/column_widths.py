from __future__ import annotations

import json
import sqlite3

from .secure_store import SecureStore


def load_column_widths(
    secure_store: SecureStore,
    setting: str,
    columns: tuple[str, ...],
    defaults: dict[str, int],
    minimums: dict[str, int] | None = None,
) -> dict[str, int]:
    try:
        saved_widths = json.loads(secure_store.get_setting(setting) or "{}")
    except (json.JSONDecodeError, sqlite3.Error):
        return defaults.copy()
    if not isinstance(saved_widths, dict):
        return defaults.copy()
    minimum_widths = minimums or {}
    return {
        column: (
            saved_widths[column]
            if type(saved_widths.get(column)) is int
            and minimum_widths.get(column, 30) <= saved_widths[column] <= 10000
            else defaults[column]
        )
        for column in columns
    }
