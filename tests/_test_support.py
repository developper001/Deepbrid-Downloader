from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import struct
import tempfile
import threading
import tkinter as tk
import unittest
import urllib.error
import urllib.parse
import uuid
from datetime import datetime
from contextlib import closing
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch
from src.deepbrid_client import APP_USER_AGENT, DeepbridClient, DeepbridError
from src.app_events import (
    ApiKeyCheckEvent,
    HostsErrorEvent,
    HostsLoadedEvent,
    StartupApiKeyProblemEvent,
    StartupUpdateEvent,
    UpdateCheckEvent,
)
from src.app_config import (
    AppConfiguration,
    RESETTABLE_SETTINGS,
    USENET_AUTO_OPEN_BROWSER_SETTING,
    reset_application_settings,
)
from src.app_info import (
    GITHUB_LATEST_RELEASE_URL,
    LatestRelease,
    ReleaseAsset,
    UpdateCheckError,
    __version__ as APP_VERSION,
    fetch_latest_release,
    fetch_latest_release_details,
    is_newer_version,
    platform_release_asset,
)
from src.app import (
    API_KEY_DASHBOARD_URL,
    APP_DATA_DIR,
    DATABASE_PATH,
    FILE_HOST_LINKS_DRAFT_PURPOSE,
    FILE_HOST_LINKS_DRAFT_SETTING,
    COLUMN_ORDER,
    DEFAULT_COLUMNS,
    FILE_HOST_LOGO_DARK_PATH,
    FILE_HOST_LOGO_LIGHT_PATH,
    DownloaderApp,
    LEGACY_DATABASE_PATH,
    LEGACY_DATABASE_PATHS,
    LEGACY_QUEUE_DATABASE_PATH,
    IMAGE_ROOT,
    OUTPUT_DIR,
    TORRENT_LOGO_DARK_PATH,
    TORRENT_LOGO_LIGHT_PATH,
    _ConsoleStream,
    append_output_log_line,
    default_download_directory,
    _filter_and_sort_host_rows,
    _parse_linked_image_badge,
    format_bytes,
    format_duration,
    format_item_eta,
    progress_indicator_values,
    size_verification_label,
    _setting_is_enabled,
    migrate_legacy_database,
    migrate_legacy_databases,
    redact_log_urls,
    remove_legacy_storage,
    smooth_rate,
    validate_api_key,
    validate_output_folder,
)
from src.queue_table import QueueTableView, full_file_path
from src.table_column_menu import show_column_context_menu
from src.link_utils import extract_supported_links, supported_link_status
from src.queue_store import QueueStore
from src.settings_dialog import SettingsDialog
from src.secure_store import SecureStore
from src.single_instance import acquire_single_instance
from src.startup_manager import (
    MACOS_LOGIN_ITEMS_URL,
    StartupConfigurationError,
    StartupManager,
)
from src.update_manager import (
    UpdateInstallError,
    apply_update_from_args,
    download_update_asset,
)
from src.usenet_finder import (
    FinderFile,
    FinderPackage,
    FinderResult,
    FinderSearchPage,
    UsenetFinderClient,
    UsenetFinderError,
    file_size_bytes,
)
from src.usenet_browser import UsenetBrowserSession
from src.usenet_finder_dialog import (
    UsenetFinderDialog,
    _configure_horizontal_scrollbar,
    _read_column_widths,
    load_table_column_layout,
    load_table_column_widths,
    save_table_column_layout,
)
from src.usenet_finder_state import (
    CACHE_DURATION_SETTING,
    CACHE_TTL_SECONDS,
    DEFAULT_CACHE_DURATION_HOURS,
    HIDE_AUXILIARY_FILES_SETTING,
    UsenetFinderState,
    valid_cache_duration_hours,
)


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        body, self.body = self.body, b""
        return body

    def getcode(self) -> int:
        return self.status
class FakeSelectionTable:
    def __init__(self, row_ids: list[str]):
        self.row_ids = row_ids
        self.selected: list[str] = []
        self.has_widget_focus = False

    def get_children(self, _parent: str = "") -> tuple[str, ...]:
        return tuple(self.row_ids)

    def identify_region(self, _x: int, _y: int) -> str:
        return "cell" if 0 <= _y < len(self.row_ids) else "nothing"

    def identify_row(self, y: int) -> str:
        return self.row_ids[y] if 0 <= y < len(self.row_ids) else ""

    def selection(self) -> tuple[str, ...]:
        return tuple(self.selected)

    def selection_add(self, *row_ids: str) -> None:
        for row_id in row_ids:
            if row_id not in self.selected:
                self.selected.append(row_id)

    def selection_remove(self, *row_ids: str) -> None:
        self.selected = [row_id for row_id in self.selected if row_id not in row_ids]

    def selection_set(self, *row_ids: str) -> None:
        self.selected = list(row_ids)

    def focus(self, _row_id: str) -> None:
        pass

    def focus_set(self) -> None:
        self.has_widget_focus = True
