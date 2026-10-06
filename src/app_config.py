from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .secure_store import SecureStorageError, SecureStore


def setting_is_enabled(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value != "false"


@dataclass(frozen=True)
class AppConfiguration:
    api_key: str
    secure_storage_error: str | None
    auto_check_updates: bool
    auto_start_downloads: bool
    visible_columns: list[str]
    column_order: list[str]
    output_dir: Path
    hosts: dict[str, str]
    dark_theme: bool

    @classmethod
    def load(
        cls,
        secure_store: SecureStore,
        column_order: tuple[str, ...],
        default_columns: tuple[str, ...],
        default_download_directory: Callable[[], Path],
        read_legacy_api_key: Callable[[], str | None],
        remove_legacy_env: Callable[[], None],
    ) -> AppConfiguration:
        api_key = ""
        secure_storage_error = None
        try:
            api_key = secure_store.get_api_key() or ""
            legacy_key = read_legacy_api_key()
            if api_key:
                remove_legacy_env()
            elif legacy_key:
                secure_store.save_api_key(legacy_key)
                if secure_store.get_api_key() != legacy_key:
                    raise SecureStorageError("Could not verify the encrypted API key migration.")
                api_key = legacy_key
                remove_legacy_env()
            else:
                remove_legacy_env()
        except (SecureStorageError, OSError) as error:
            secure_storage_error = str(error)

        visible_columns = list(default_columns)
        saved_columns = secure_store.get_setting("visible_columns")
        if saved_columns:
            try:
                requested_columns = json.loads(saved_columns)
                valid_columns = set(column_order)
                if isinstance(requested_columns, list):
                    visible_columns = [
                        column for column in requested_columns if column in valid_columns
                    ]
                    if "eta" in visible_columns and "time_remaining" not in visible_columns:
                        visible_columns.insert(visible_columns.index("eta"), "time_remaining")
                    if visible_columns == [
                        "filename",
                        "host",
                        "status",
                        "size",
                        "remaining",
                        "time_remaining",
                        "eta",
                    ]:
                        visible_columns.remove("remaining")
                    if visible_columns == [
                        "filename",
                        "host",
                        "status",
                        "size",
                        "time_remaining",
                        "eta",
                    ]:
                        visible_columns = list(default_columns)
                    if not visible_columns:
                        visible_columns = list(default_columns)
            except json.JSONDecodeError:
                pass

        saved_order = secure_store.get_setting("column_order")
        try:
            requested_order = json.loads(saved_order) if saved_order else []
        except json.JSONDecodeError:
            requested_order = []
        if isinstance(requested_order, list):
            configured_order = list(
                dict.fromkeys(
                    column
                    for column in requested_order
                    if isinstance(column, str) and column in column_order
                )
            )
        else:
            configured_order = []
        if not configured_order:
            configured_order = list(column_order)
        else:
            configured_order.extend(
                column for column in column_order if column not in configured_order
            )
        visible_set = set(visible_columns)
        visible_columns = [
            column for column in configured_order if column in visible_set
        ]

        if secure_store.get_setting("progress_column_initialized") != "true":
            configured_order.remove("progress")
            size_position = (
                configured_order.index("size") + 1
                if "size" in configured_order
                else len(configured_order)
            )
            configured_order.insert(size_position, "progress")
            if "progress" not in visible_columns:
                size_position = (
                    visible_columns.index("size") + 1
                    if "size" in visible_columns
                    else len(visible_columns)
                )
                visible_columns.insert(size_position, "progress")
            secure_store.set_setting("column_order", json.dumps(configured_order))
            secure_store.set_setting("visible_columns", json.dumps(visible_columns))
            secure_store.set_setting("progress_column_initialized", "true")

        if secure_store.get_setting("progress_percentage_column_initialized") != "true":
            configured_order.remove("progress_percentage")
            progress_position = (
                configured_order.index("progress") + 1
                if "progress" in configured_order
                else len(configured_order)
            )
            configured_order.insert(progress_position, "progress_percentage")
            secure_store.set_setting("column_order", json.dumps(configured_order))
            secure_store.set_setting("visible_columns", json.dumps(visible_columns))
            secure_store.set_setting("progress_percentage_column_initialized", "true")

        output_dir = Path(
            secure_store.get_setting("output_directory")
            or str(default_download_directory())
        ).expanduser()
        saved_hosts = secure_store.get_setting("hosts")
        try:
            cached_hosts = json.loads(saved_hosts) if saved_hosts else {}
        except json.JSONDecodeError:
            cached_hosts = {}
        hosts = (
            {str(domain): str(status) for domain, status in cached_hosts.items()}
            if isinstance(cached_hosts, dict)
            else {}
        )

        return cls(
            api_key=api_key,
            secure_storage_error=secure_storage_error,
            auto_check_updates=setting_is_enabled(
                secure_store.get_setting("auto_check_updates_on_startup"),
                default=True,
            ),
            auto_start_downloads=setting_is_enabled(
                secure_store.get_setting("auto_start_downloads_on_startup"),
                default=True,
            ),
            visible_columns=visible_columns,
            column_order=configured_order,
            output_dir=output_dir,
            hosts=hosts,
            dark_theme=secure_store.get_setting("dark_theme") == "true",
        )
