from __future__ import annotations

import json
import math
import queue
import re
import shutil
import sqlite3
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from platformdirs import user_data_dir, user_downloads_dir

from .app_info import (
    GITHUB_REPOSITORY_URL,
    LatestRelease,
    ReleaseAsset,
    UpdateCheckError,
    __version__ as APP_VERSION,
    fetch_latest_release_details,
    is_newer_version,
    platform_release_asset,
)
from .deepbrid_client import DeepbridClient, DeepbridError, safe_filename
from .link_utils import extract_http_links, extract_supported_links, supported_link_status
from .queue_store import QueueStore
from .secure_store import SecureStorageError, SecureStore
from .single_instance import acquire_single_instance
from .update_manager import UpdateInstallError, download_update_asset, launch_update_helper


def resolve_app_path(*relative_parts: str) -> Path:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base_dir.joinpath(*relative_parts)


IS_FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_ROOT = resolve_app_path()
PROJECT_ROOT = Path(sys.executable).resolve().parent if IS_FROZEN else Path(__file__).resolve().parent.parent
APP_DATA_DIR = Path(user_data_dir("Deepbrid Downloader", "Deepbrid")).expanduser()
OUTPUT_DIR = APP_DATA_DIR / "download"
DATABASE_PATH = APP_DATA_DIR / "queue.sqlite3"
LEGACY_DATABASE_PATH = RESOURCE_ROOT / "src" / "deepbrid_downloader.sqlite3"
LEGACY_QUEUE_DATABASE_PATH = PROJECT_ROOT / "download" / "queue.sqlite3"
LEGACY_STORAGE_PATH = PROJECT_ROOT / "download"
LEGACY_ENV_PATH = PROJECT_ROOT / ".env"
INPUT_PATH = PROJECT_ROOT / "input_links.txt"
README_PATH = RESOURCE_ROOT / "README.md"
LOG_PATH = LEGACY_STORAGE_PATH / "logs.txt"
ICON_PATH = RESOURCE_ROOT / "src" / "deepbrid-logo.png"
ICON_ICO_PATH = RESOURCE_ROOT / "src" / "deepbrid-favicon.ico"
WORDMARK_PATH = RESOURCE_ROOT / "src" / "deepbrid-wordmark.png"
WORDMARK_LIGHT_PATH = RESOURCE_ROOT / "src" / "deepbrid-wordmark-light.png"
COLUMN_ORDER = ("filename", "link", "host", "status", "size", "remaining", "time_remaining", "eta", "verification")
DEFAULT_COLUMNS = ("filename", "host", "size", "time_remaining", "eta", "verification")
LINK_PLACEHOLDER = "Paste supported file-host links or HTML containing links here..."
API_KEY_DASHBOARD_URL = "https://www.deepbrid.com/devices"


def _filter_and_sort_host_rows(
    rows: list[tuple[str, str, str]],
    query: str,
    column: str,
    reverse: bool,
) -> list[tuple[str, str, str]]:
    column_index = {"host": 0, "availability": 1, "limit": 2}[column]
    normalized_query = query.casefold()
    matching_rows = (row for row in rows if normalized_query in row[0].casefold())
    return sorted(matching_rows, key=lambda row: row[column_index].casefold(), reverse=reverse)


def _parse_linked_image_badge(markdown_line: str) -> tuple[str, str] | None:
    match = re.fullmatch(r"\[!\[([^\]]+)\]\(([^)]+)\)\]\(([^)]+)\)", markdown_line.strip())
    if not match:
        return None
    return match.group(1), match.group(3)


def default_download_directory() -> Path:
    downloads = Path(user_downloads_dir()).expanduser()
    if downloads.is_dir():
        return downloads
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR


class _ConsoleStream:
    encoding = "utf-8"
    errors = "replace"

    def __init__(self, log: Callable[[str], None]):
        self.log = log
        self.pending = ""
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self.lock:
            lines = (self.pending + text).splitlines(keepends=True)
            self.pending = ""
            if lines and not lines[-1].endswith(("\n", "\r")):
                self.pending = lines.pop()
        for line in lines:
            self.log(line.rstrip("\r\n"))
        return len(text)

    def flush(self) -> None:
        with self.lock:
            pending = self.pending
            self.pending = ""
        if pending:
            self.log(pending)

    def isatty(self) -> bool:
        return False


def validate_api_key(api_key: str) -> str | None:
    return None if api_key.strip() else "Enter your Deepbrid API key first."


def validate_output_folder(folder: Path | str | None) -> str | None:
    if folder is None or not str(folder).strip():
        return "Choose a download folder before starting."
    folder = Path(folder).expanduser()
    if not folder.exists():
        return f"The download folder does not exist:\n{folder}\nChoose an existing folder or create it first."
    if not folder.is_dir():
        return f"The download location is not a folder:\n{folder}"
    return None


def format_bytes(value: int | None) -> str:
    if value is None or value < 0:
        return "Unknown"
    amount = float(value)
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or suffix == "TB":
            return f"{int(amount)} B" if suffix == "B" else f"{amount:.1f} {suffix}"
        amount /= 1024
    return "Unknown"


def format_duration(seconds: float | None) -> str:
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "Calculating"
    remaining = int(seconds)
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts or seconds:
        parts.append(f"{seconds}s")
    return " ".join(parts[:2])


def format_item_eta(status: str, total: int | None, downloaded: int, speed: float) -> str:
    if status in {"completed", "skipped"}:
        return "0s"
    if status != "downloading":
        return "—"
    if total is None:
        return "Calculating"
    remaining = max(0, total - downloaded)
    if remaining == 0:
        return "0s"
    return format_duration(remaining / speed) if speed > 0 else "Calculating"


def size_verification_label(status: str, size_verified: bool) -> str:
    if status != "completed":
        return "—"
    return "Verified" if size_verified else "Not verified"


def smooth_rate(previous: float, sample: float, weight: float = 0.1) -> float:
    if sample <= 0:
        return previous
    if previous <= 0:
        return sample
    return previous * (1 - weight) + sample * weight


def redact_log_urls(contents: str) -> str:
    contents = re.sub(
        r"https?%3a%2f%2f[^\s&\"'<>]+",
        "<URL REDACTED>",
        contents,
        flags=re.IGNORECASE,
    )
    return re.sub(r"https?://[^\s\"'<>]+", "<URL REDACTED>", contents, flags=re.IGNORECASE)


def redact_legacy_log_file() -> bool:
    if not LOG_PATH.exists():
        return True
    try:
        contents = LOG_PATH.read_text(encoding="utf-8", errors="replace")
        redacted = redact_log_urls(contents)
        if redacted != contents:
            LOG_PATH.write_text(redacted, encoding="utf-8")
        return True
    except OSError:
        return False


def migrate_legacy_database(source: Path, target: Path) -> None:
    if target.exists() or not source.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(source, timeout=30)
    target_connection = sqlite3.connect(target, timeout=30)
    try:
        source_connection.backup(target_connection)
        result = target_connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise sqlite3.DatabaseError("Migrated database did not pass integrity_check.")
    except Exception:
        target_connection.close()
        source_connection.close()
        target.unlink(missing_ok=True)
        raise
    target_connection.close()
    source_connection.close()


def migrate_legacy_databases(
    target: Path = DATABASE_PATH,
    sources: tuple[Path, ...] = (LEGACY_DATABASE_PATH, LEGACY_QUEUE_DATABASE_PATH),
) -> None:
    if target.exists():
        return
    for source in sources:
        migrate_legacy_database(source, target)
        if target.exists():
            return


def remove_legacy_storage(output_directory: Path, selected_output: Path) -> bool:
    if not output_directory.exists():
        return True
    try:
        if selected_output.resolve() == output_directory.resolve():
            return False
        allowed_entries = {"queue.sqlite3", "logs.txt"}
        if any(path.name not in allowed_entries for path in output_directory.iterdir()):
            return False
        shutil.rmtree(output_directory)
        return True
    except OSError:
        return False


def read_legacy_api_key() -> str:
    if not LEGACY_ENV_PATH.exists():
        return ""
    for line in LEGACY_ENV_PATH.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s*DEEPBRID_API_KEY\s*=\s*(.*?)\s*$", line)
        if match:
            return match.group(1).strip("\"'")
    return ""


def remove_legacy_env() -> None:
    if LEGACY_ENV_PATH.exists():
        LEGACY_ENV_PATH.unlink()


class DownloaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        migrate_legacy_databases()
        self.legacy_logs_redacted = redact_legacy_log_file()
        self.secure_store = SecureStore(DATABASE_PATH)
        saved_auto_check = self.secure_store.get_setting("auto_check_updates_on_startup")
        self.auto_check_updates = tk.BooleanVar(master=root, value=saved_auto_check != "false")
        self.store = QueueStore(DATABASE_PATH, self.secure_store)
        self.secure_storage_error: str | None = None
        stored_key = ""
        try:
            stored_key = self.secure_store.get_api_key() or ""
            legacy_key = read_legacy_api_key()
            if stored_key:
                remove_legacy_env()
            elif legacy_key:
                self.secure_store.save_api_key(legacy_key)
                if self.secure_store.get_api_key() != legacy_key:
                    raise SecureStorageError("Could not verify the encrypted API key migration.")
                stored_key = legacy_key
                remove_legacy_env()
            else:
                remove_legacy_env()
        except (SecureStorageError, OSError) as error:
            self.secure_storage_error = str(error)
        self.events: queue.Queue[tuple] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.active_cancel_events: dict[int, threading.Event] = {}
        self.api_key = tk.StringVar(value=stored_key)
        self.status_text = tk.StringVar(value="Ready")
        self.progress_value = tk.DoubleVar(value=0)
        self.queue_progress_text = tk.StringVar(value="Queue: 0/0 files (0%)")
        self.total_eta_text = tk.StringVar(value="Total remaining: calculating")
        self.item_speeds: dict[int, float] = {}
        self.item_status_messages: dict[int, str] = {}
        self.average_transfer_speed = 0.0
        self.last_eta_refresh = time.monotonic()
        self.sort_column = "filename"
        self.sort_reverse = False
        self._last_saved_priority_order: tuple[int, ...] | None = None
        self.visible_columns = list(DEFAULT_COLUMNS)
        saved_columns = self.secure_store.get_setting("visible_columns")
        if saved_columns:
            try:
                requested_columns = json.loads(saved_columns)
                valid_columns = set(COLUMN_ORDER)
                if isinstance(requested_columns, list):
                    self.visible_columns = [column for column in requested_columns if column in valid_columns]
                    if "eta" in self.visible_columns and "time_remaining" not in self.visible_columns:
                        self.visible_columns.insert(self.visible_columns.index("eta"), "time_remaining")
                    if self.visible_columns == [
                        "filename", "host", "status", "size", "remaining", "time_remaining", "eta"
                    ]:
                        self.visible_columns.remove("remaining")
                    if self.visible_columns == [
                        "filename", "host", "status", "size", "time_remaining", "eta"
                    ]:
                        self.visible_columns = list(DEFAULT_COLUMNS)
                    if not self.visible_columns:
                        self.visible_columns = list(DEFAULT_COLUMNS)
            except json.JSONDecodeError:
                pass
        self.column_order = []
        saved_column_order = self.secure_store.get_setting("column_order")
        if saved_column_order:
            try:
                requested_order = json.loads(saved_column_order)
                if isinstance(requested_order, list):
                    self.column_order = list(dict.fromkeys(
                        column
                        for column in requested_order
                        if isinstance(column, str) and column in COLUMN_ORDER
                    ))
            except json.JSONDecodeError:
                pass
        if not self.column_order:
            self.column_order = list(COLUMN_ORDER)
        else:
            self.column_order.extend(
                column for column in COLUMN_ORDER if column not in self.column_order
            )
        visible_set = set(self.visible_columns)
        self.visible_columns = [
            column for column in self.column_order if column in visible_set
        ]
        self.output_dir = Path(
            self.secure_store.get_setting("output_directory") or str(default_download_directory())
        ).expanduser()
        self.output_dir_text = tk.StringVar(value=str(self.output_dir))
        self.hosts: dict[str, str] = {}
        self.host_limits: dict[str, str] = {}
        self.hosts_popup: tk.Toplevel | None = None
        self._hosts_popup_update = None
        saved_hosts = self.secure_store.get_setting("hosts")
        if saved_hosts:
            try:
                cached_hosts = json.loads(saved_hosts)
                if isinstance(cached_hosts, dict):
                    self.hosts = {str(domain): str(status) for domain, status in cached_hosts.items()}
            except json.JSONDecodeError:
                pass
        self.console_visible = False
        self.dark_theme = self._load_theme_preference()
        self.key_save_after: str | None = None

        self._build_ui()
        self._install_console_capture()
        self._set_window_icon()
        self.api_key.trace_add("write", self._schedule_key_save)
        if self.hosts:
            self._apply_host_statuses(self.hosts)
        self._import_input_file()
        self._refresh_rows()
        self._save_visible_queue_order()
        if remove_legacy_storage(LEGACY_STORAGE_PATH, self.output_dir):
            self._log("Migrated legacy queue data and removed the old download folder.")
        elif LEGACY_STORAGE_PATH.exists():
            self._log("The old download folder contains user files or is selected as the output; it was retained.")
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.after(1000, self._process_events)
        self._apply_theme()
        if self.secure_storage_error:
            self._log(f"Secure API-key storage unavailable: {self.secure_storage_error}")
        if not self.legacy_logs_redacted:
            self._log("Could not redact URLs from the existing log file; check that it is writable.")
        self._refresh_hosts()
        self.root.after_idle(self._check_for_updates_on_startup)

    def _build_ui(self) -> None:
        self.root.title("Deepbrid Downloader")
        self.root.geometry("1100x620")
        self.root.minsize(800, 450)

        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=0)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(4, weight=1)

        self.brand_logo_light = tk.PhotoImage(file=str(WORDMARK_LIGHT_PATH))
        self.brand_logo_dark = tk.PhotoImage(file=str(WORDMARK_PATH))
        self.brand_logo_label = ttk.Label(main, image=self.brand_logo_light)
        self.brand_logo_label.grid(row=0, column=0, rowspan=2, sticky="w", padx=(0, 18))

        key_row = ttk.Frame(main)
        key_row.grid(row=0, column=1, sticky="ew", pady=(0, 8))
        key_row.columnconfigure(1, weight=1)
        ttk.Label(key_row, text="API key").grid(row=0, column=0, padx=(0, 8))
        self.api_key_entry = ttk.Entry(key_row, textvariable=self.api_key, show="*")
        self.api_key_entry.grid(row=0, column=1, sticky="ew")
        self.key_visibility_button = ttk.Button(key_row, text="Show", command=self._toggle_key_visibility)
        self.key_visibility_button.grid(row=0, column=2, padx=(8, 0))
        self.api_key_page_button = ttk.Button(
            key_row,
            text="Get API key",
            command=self._open_api_key_dashboard,
        )
        self.api_key_page_button.grid(row=0, column=3, padx=(8, 0))

        folder_row = ttk.Frame(main)
        folder_row.grid(row=1, column=1, sticky="ew", pady=(0, 8))
        folder_row.columnconfigure(1, weight=1)
        ttk.Label(folder_row, text="Download folder").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(folder_row, textvariable=self.output_dir_text, state="readonly").grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Button(folder_row, text="Browse...", command=self._choose_output_folder).grid(
            row=0, column=2, padx=(8, 0)
        )
        self.theme_button = ttk.Button(folder_row, text="Dark mode", command=self._toggle_theme)
        self.theme_button.grid(row=0, column=3, padx=(8, 0))

        add_row = ttk.Frame(main)
        add_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        add_row.columnconfigure(0, weight=1)
        self.links_input = tk.Text(add_row, height=3, wrap="word", undo=True)
        self.links_input.grid(row=0, column=0, sticky="ew")
        self.link_placeholder_active = True
        self.links_input.insert("1.0", LINK_PLACEHOLDER)
        self.links_input.bind("<FocusIn>", self._clear_link_placeholder)
        self.links_input.bind("<FocusOut>", self._restore_link_placeholder)
        links_scrollbar = ttk.Scrollbar(add_row, orient="vertical", command=self.links_input.yview)
        links_scrollbar.grid(row=0, column=1, sticky="ns")
        self.links_input.configure(yscrollcommand=links_scrollbar.set)
        self.add_links_button = ttk.Button(add_row, text="Add links", command=self._add_link)
        self.add_links_button.grid(row=0, column=2, padx=(8, 0), sticky="ns")
        self.add_links_button.configure(state="normal" if self.hosts else "disabled")

        controls = ttk.Frame(main)
        controls.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.start_button = ttk.Button(controls, text="Start", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop", command=self._stop, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        self.refresh_hosts_button = ttk.Button(
            controls,
            text="Host Status",
            command=lambda: self._refresh_hosts(show_popup=True),
        )
        self.refresh_hosts_button.pack(side="left", padx=(8, 0))
        self.columns_button = ttk.Button(controls, text="Columns", command=self._show_columns_dialog)
        self.columns_button.pack(side="left", padx=(8, 0))
        self.readme_button = ttk.Button(controls, text="About", command=self._show_readme)
        self.readme_button.pack(side="left", padx=(8, 0))
        ttk.Label(controls, textvariable=self.status_text).pack(side="right")

        self.panes = ttk.Panedwindow(main, orient="vertical")
        self.panes.grid(row=4, column=0, columnspan=2, sticky="nsew")
        table_frame = ttk.Frame(self.panes)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.table = ttk.Treeview(
            table_frame,
            columns=COLUMN_ORDER,
            show="headings",
            selectmode="extended",
        )
        self._selection_anchor: str | None = None
        self.column_labels = {
            "filename": "File name",
            "link": "Original link",
            "host": "Host",
            "status": "Host status",
            "size": "Downloaded / total",
            "remaining": "Remaining",
            "time_remaining": "ETA",
            "eta": "Status",
            "verification": "Verification",
        }
        for column, label in self.column_labels.items():
            self.table.heading(column, text=label, command=lambda key=column: self._sort_by(key))
        self.table.column("filename", width=250, minwidth=140, stretch=True)
        self.table.column("link", width=330, minwidth=150, stretch=True)
        self.table.column("host", width=135, minwidth=100, stretch=False)
        self.table.column("status", width=120, minwidth=90, stretch=False)
        self.table.column("size", width=145, minwidth=120, stretch=False, anchor="e")
        self.table.column("remaining", width=105, minwidth=90, stretch=False, anchor="e")
        self.table.column("time_remaining", width=95, minwidth=80, stretch=False, anchor="e")
        self.table.column("eta", width=170, minwidth=130, stretch=False)
        self.table.column("verification", width=110, minwidth=95, stretch=False)
        self.table.configure(displaycolumns=self.visible_columns)
        self.table.grid(row=0, column=0, sticky="nsew")
        table_scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        table_scrollbar.grid(row=0, column=1, sticky="ns")
        table_horizontal_scrollbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self.table.xview)
        table_horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        self.table.configure(
            yscrollcommand=table_scrollbar.set,
            xscrollcommand=table_horizontal_scrollbar.set,
        )
        self.table.bind("<Button-1>", self._select_table_row)
        self.table.bind("<Control-a>", self._select_all_table_rows)
        self.table.bind("<Button-2>" if sys.platform == "darwin" else "<Button-3>", self._show_link_menu)
        self.table.bind("<Control-c>", self._copy_original_link)
        if sys.platform == "darwin":
            self.table.bind("<Command-c>", self._copy_original_link)
        self.link_menu = tk.Menu(self.root, tearoff=False)
        self.link_menu.add_command(label="Copy original link", command=self._copy_original_link)
        self.link_menu.add_command(label="Copy Deepbrid link", command=self._copy_deepbrid_link)
        self.link_menu.add_command(label="Disable link", command=self._toggle_link_enabled)
        self.link_menu.add_separator()
        self.link_menu.add_command(label="Retry link", command=self._retry_item)
        self.link_menu.add_command(label="Force re-download", command=self._force_redownload)
        self.link_menu.add_separator()
        self.link_menu.add_command(label="Remove link", command=self._remove_link)

        self.console_frame = ttk.Frame(self.panes)
        self.console_frame.columnconfigure(0, weight=1)
        self.console_frame.rowconfigure(0, weight=1)
        self.console = tk.Text(self.console_frame, height=8, wrap="word", state="disabled")
        self.console.grid(row=0, column=0, sticky="nsew")
        console_scrollbar = ttk.Scrollbar(self.console_frame, orient="vertical", command=self.console.yview)
        console_scrollbar.grid(row=0, column=1, sticky="ns")
        self.console.configure(yscrollcommand=console_scrollbar.set)

        self.panes.add(table_frame, weight=4)

        bottom_row = ttk.Frame(main)
        bottom_row.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.console_button = ttk.Button(bottom_row, text="Show console", command=self._toggle_console)
        self.console_button.pack(side="left", padx=(0, 10))
        progress_frame = ttk.Frame(bottom_row)
        progress_frame.pack(side="left", fill="both", expand=True)
        progress_frame.columnconfigure(0, weight=1)
        summary_frame = ttk.Frame(progress_frame)
        summary_frame.grid(row=0, column=0, sticky="ew", pady=(0, 3))
        summary_frame.columnconfigure(1, weight=1)
        ttk.Label(summary_frame, textvariable=self.queue_progress_text, anchor="w").grid(
            row=0,
            column=0,
            sticky="w",
        )
        ttk.Label(summary_frame, textvariable=self.total_eta_text, anchor="e").grid(
            row=0,
            column=1,
            sticky="e",
        )
        self.progress = ttk.Progressbar(progress_frame, variable=self.progress_value, maximum=100)
        self.progress.grid(row=1, column=0, sticky="ew")

    def _import_input_file(self) -> None:
        if not INPUT_PATH.exists():
            return
        contents = INPUT_PATH.read_text(encoding="utf-8")
        links = extract_http_links(contents)
        if contents.strip() and not links:
            self._log("input_links.txt contains no valid HTTP/HTTPS links; it was retained for review.")
            return
        try:
            for link in links:
                host = supported_link_status(link, self.hosts) if self.hosts else None
                status, message = host or ("unknown", "Host status not loaded")
                self.store.add(link, status, message)
            INPUT_PATH.unlink()
        except Exception as error:
            self._log(f"Could not migrate input_links.txt into the encrypted queue: {error}")
            return
        self._log(f"Encrypted {len(links)} link(s) into SQLite and removed input_links.txt.")

    def _schedule_key_save(self, *_args: str) -> None:
        if self.key_save_after:
            self.root.after_cancel(self.key_save_after)
        self.key_save_after = self.root.after(800, self._persist_api_key)

    def _persist_api_key(self) -> bool:
        self.key_save_after = None
        key = self.api_key.get().strip()
        try:
            self.secure_store.save_api_key(key)
        except SecureStorageError as error:
            self.status_text.set("Could not securely save API key")
            self._log(str(error))
            return False
        self.status_text.set("API key saved securely" if key else "API key removed")
        self._log("API key encrypted in the SQLite database." if key else "Saved API key removed from the database.")
        return True

    def _add_link(self) -> None:
        if not self.hosts:
            messagebox.showerror("Host list unavailable", "Refresh the supported-host list before adding links.")
            return
        text = "" if self.link_placeholder_active else self.links_input.get("1.0", "end").strip()
        if not text:
            messagebox.showerror("Links required", "Paste links or HTML containing links.")
            return
        links = extract_supported_links(text, self.hosts)
        if not links:
            messagebox.showinfo("No supported links", "No links from supported Deepbrid hosts were found.")
            return
        added_count = sum(self.store.add(link, status, message) for link, status, message in links)
        duplicate_count = len(links) - added_count
        self.links_input.delete("1.0", "end")
        self.link_placeholder_active = False
        self.status_text.set(f"Added {added_count}; skipped {duplicate_count} duplicate(s)")
        self._log(f"Extracted {len(links)} supported link(s); added {added_count}, skipped {duplicate_count} duplicate(s).")
        self._refresh_rows()
        self._save_visible_queue_order()

    def _clear_link_placeholder(self, _event: tk.Event | None = None) -> None:
        if self.link_placeholder_active:
            self.links_input.delete("1.0", "end")
            self.link_placeholder_active = False
            self._apply_link_input_color()

    def _restore_link_placeholder(self, _event: tk.Event | None = None) -> None:
        if not self.links_input.get("1.0", "end").strip():
            self.links_input.delete("1.0", "end")
            self.links_input.insert("1.0", LINK_PLACEHOLDER)
            self.link_placeholder_active = True
            self._apply_link_input_color()

    def _apply_link_input_color(self) -> None:
        if not hasattr(self, "links_input"):
            return
        foreground = "#8793a5" if self.link_placeholder_active else (
            "#e8eee9" if self.dark_theme else "#202b25"
        )
        self.links_input.configure(foreground=foreground)

    def _refresh_hosts(self, show_popup: bool = False) -> None:
        if self.refresh_hosts_button.instate(["disabled"]):
            return
        if show_popup:
            self._show_hosts_popup(self.hosts, self.host_limits, None)
        self.refresh_hosts_button.configure(state="disabled")
        self.status_text.set("Checking host availability...")
        api_key = self.api_key.get().strip()
        self.root.after_idle(
            lambda: threading.Thread(
                target=self._load_hosts,
                args=(api_key,),
                daemon=True,
            ).start()
        )

    def _load_hosts(self, api_key: str) -> None:
        try:
            hosts = DeepbridClient.fetch_hosts(api_key)
        except DeepbridError as error:
            self.events.put(("hosts_error", str(error), error.status_code))
        else:
            limits = {}
            limits_error = None
            limits_error_status = None
            if api_key:
                try:
                    limits = DeepbridClient.fetch_host_limits(api_key)
                except DeepbridError as error:
                    limits_error = str(error)
                    limits_error_status = error.status_code
            self.events.put(("hosts", hosts, limits, limits_error, limits_error_status))

    def _show_hosts_popup(self, hosts: dict[str, str], limits: dict[str, str], limits_error: str | None) -> tk.Toplevel:
        if self.hosts_popup is not None:
            try:
                if self.hosts_popup.winfo_exists():
                    if self._hosts_popup_update is not None:
                        self._hosts_popup_update(hosts, limits, limits_error)
                    self.hosts_popup.deiconify()
                    self.hosts_popup.lift()
                    return self.hosts_popup
            except tk.TclError:
                pass
        popup = tk.Toplevel(self.root)
        popup.title("Deepbrid supported hosts")
        popup.geometry("620x500")
        popup.transient(self.root)
        frame = ttk.Frame(popup, padding=12)
        frame.pack(fill="both", expand=True)
        description = (
            "Host support and current status from Deepbrid. "
            "Daily remaining quota is shown when available."
        )
        ttk.Label(frame, text=description, wraplength=580).pack(anchor="w", pady=(0, 8))
        limits_message = ttk.Label(frame, wraplength=580)
        refresh_message = ttk.Label(frame, wraplength=580)
        api_key_button = ttk.Button(
            frame,
            text="Open Deepbrid API key page",
            command=self._open_api_key_dashboard,
        )
        if limits_error:
            limits_message.configure(text=f"Daily limits unavailable: {limits_error}")
            limits_message.pack(anchor="w", pady=(0, 8))
        search_row = ttk.Frame(frame)
        search_row.pack(fill="x", pady=(0, 8))
        ttk.Label(search_row, text="Search hosts").pack(side="left", padx=(0, 8))
        search_var = tk.StringVar(master=popup)
        search_entry = ttk.Entry(search_row, textvariable=search_var)
        search_entry.pack(side="left", fill="x", expand=True)
        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        table = ttk.Treeview(
            table_frame,
            columns=("host", "availability", "limit"),
            show="headings",
        )
        host_rows: list[tuple[str, str, str]] = []
        sort_state = {"column": "host", "reverse": False}

        def render_rows() -> None:
            rows = _filter_and_sort_host_rows(
                host_rows,
                search_var.get(),
                sort_state["column"],
                sort_state["reverse"],
            )
            table.delete(*table.get_children())
            for row in rows:
                table.insert("", "end", values=row)

        def sort_by(column: str) -> None:
            if sort_state["column"] == column:
                sort_state["reverse"] = not sort_state["reverse"]
            else:
                sort_state["column"] = column
                sort_state["reverse"] = False
            render_rows()

        for column, label in (
            ("host", "Host"),
            ("availability", "Availability"),
            ("limit", "Daily remaining"),
        ):
            table.heading(column, text=label, command=lambda key=column: sort_by(key))
        table.column("host", width=220, anchor="w")
        table.column("availability", width=220, anchor="w")
        table.column("limit", width=140, anchor="e")
        table.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=table.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        table.configure(yscrollcommand=scrollbar.set)

        def update_hosts(
            updated_hosts: dict[str, str],
            updated_limits: dict[str, str],
            updated_limits_error: str | None,
            refresh_error: str | None = None,
            status_code: int | None = None,
        ) -> None:
            host_rows.clear()
            for domain, status in updated_hosts.items():
                normalized_status = status.strip().split("(", 1)[0].strip().lower()
                if normalized_status == "down":
                    availability = "Unavailable"
                elif normalized_status == "up":
                    availability = "Available"
                elif normalized_status == "supported":
                    availability = "Supported; live status unavailable"
                else:
                    availability = status.strip() or "Status unavailable"
                limit = updated_limits.get(domain.lower())
                if limit is None:
                    host_slug = domain.lower().split(".", 1)[0]
                    limit = next(
                        (
                            value
                            for key, value in updated_limits.items()
                            if key.lower().split(".", 1)[0] == host_slug
                        ),
                        "—",
                    )
                host_rows.append((domain, availability, limit))
            if updated_limits_error:
                limits_message.configure(text=f"Daily limits unavailable: {updated_limits_error}")
                if not limits_message.winfo_manager():
                    limits_message.pack(anchor="w", pady=(0, 8))
            else:
                limits_message.pack_forget()
            if refresh_error:
                refresh_message.configure(text=f"Host refresh failed: {refresh_error}")
                if not refresh_message.winfo_manager():
                    refresh_message.pack(anchor="w", pady=(0, 8))
            else:
                refresh_message.pack_forget()
            if status_code == 401:
                if not api_key_button.winfo_manager():
                    api_key_button.pack(anchor="w", pady=(0, 8))
            else:
                api_key_button.pack_forget()
            render_rows()

        search_var.trace_add("write", lambda *_args: render_rows())
        self.hosts_popup = popup
        self._hosts_popup_update = update_hosts
        popup.bind(
            "<Destroy>",
            lambda event, window=popup: self._hosts_popup_destroyed(event, window),
            add="+",
        )
        update_hosts(hosts, limits, limits_error)
        return popup

    def _hosts_popup_destroyed(self, event: tk.Event, popup: tk.Toplevel) -> None:
        if event.widget == popup and self.hosts_popup is popup:
            self.hosts_popup = None
            self._hosts_popup_update = None

    def _apply_host_statuses(self, hosts: dict[str, str]) -> None:
        for item in self.store.list_items():
            result = supported_link_status(item.url, hosts)
            if result is None:
                self.store.update(
                    item.id,
                    host_status="unsupported",
                    host_message="Host not listed by Deepbrid",
                )
            else:
                self.store.update(item.id, host_status=result[0], host_message=result[1])

    def _choose_output_folder(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Download running", "Stop the queue before changing its output folder.")
            return
        selected = filedialog.askdirectory(initialdir=str(self.output_dir), mustexist=True)
        if not selected:
            return
        self.output_dir = Path(selected)
        self.output_dir_text.set(str(self.output_dir))
        self.secure_store.set_setting("output_directory", str(self.output_dir))
        self.status_text.set("Download folder set")
        self._log(f"Download folder set to {self.output_dir}")

    def _selected_items(self):
        selected = set(self.table.selection())
        if not selected:
            return []
        items_by_row = {str(item.id): item for item in self.store.list_items()}
        return [
            items_by_row[row_id]
            for row_id in self.table.get_children("")
            if row_id in selected and row_id in items_by_row
        ]

    def _set_window_icon(self) -> None:
        try:
            self.root.iconbitmap(str(ICON_ICO_PATH))
        except tk.TclError:
            pass
        try:
            self.window_icon = tk.PhotoImage(file=str(ICON_PATH))
            self.root.iconphoto(True, self.window_icon)
        except tk.TclError:
            self.window_icon = None
        if self.window_icon is None:
            self._log("Deepbrid icon asset could not be loaded.")

    @staticmethod
    def readme_text() -> str:
        try:
            return README_PATH.read_text(encoding="utf-8")
        except OSError:
            return "README.md is not available."

    @staticmethod
    def readme_image_paths() -> list[str]:
        try:
            contents = README_PATH.read_text(encoding="utf-8")
        except OSError:
            return []
        paths: list[str] = []
        for line in contents.splitlines():
            match = re.search(r"!\[[^\]]*\]\(([^)]+)\)", line)
            if match:
                paths.append(match.group(1))
        return paths

    @staticmethod
    def _open_readme_link(target: str) -> None:
        if target.startswith(("http://", "https://")):
            webbrowser.open(target)
            return
        local_path = (README_PATH.parent / target).resolve()
        if local_path.is_file():
            webbrowser.open(local_path.as_uri())

    def _show_readme(self) -> None:
        colors = self.theme_colors
        popup = tk.Toplevel(self.root)
        popup.title("Deepbrid Downloader README")
        popup.geometry("980x760")
        popup.minsize(700, 500)
        popup.transient(self.root)
        popup.configure(background=colors["background"])
        popup.grab_set()

        container = ttk.Frame(popup, padding=12)
        container.pack(fill="both", expand=True)
        container.columnconfigure(0, weight=1)
        container.rowconfigure(0, weight=1)

        canvas = tk.Canvas(
            container,
            background=colors["background"],
            highlightthickness=0,
            borderwidth=0,
        )
        canvas.grid(row=0, column=0, sticky="nsew")
        yscrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        yscrollbar.grid(row=0, column=1, sticky="ns")
        xscrollbar = ttk.Scrollbar(container, orient="horizontal", command=canvas.xview)
        xscrollbar.grid(row=1, column=0, sticky="ew")
        canvas.configure(yscrollcommand=yscrollbar.set, xscrollcommand=xscrollbar.set)

        def _on_mouse_wheel(event: tk.Event) -> None:
            if not canvas.winfo_exists():
                return
            if getattr(event, "delta", 0):
                delta = int(-event.delta / 48)
                if not delta:
                    delta = -1 if event.delta > 0 else 1
            else:
                delta = {4: -1, 5: 1}.get(getattr(event, "num", None), 0)
            try:
                canvas.yview_scroll(delta, "units")
            except tk.TclError:
                return

        popup.bind("<MouseWheel>", _on_mouse_wheel, add="+")
        popup.bind("<Shift-MouseWheel>", _on_mouse_wheel, add="+")
        frame = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=frame, anchor="nw")
        badge_row: ttk.Frame | None = None

        def render_line(raw_line: str) -> None:
            nonlocal badge_row
            stripped = raw_line.strip()
            if not stripped:
                badge_row = None
                return

            badge = _parse_linked_image_badge(stripped)
            if badge:
                if badge_row is None:
                    badge_row = ttk.Frame(frame)
                    badge_row.pack(anchor="w", pady=(2, 8))
                label = ttk.Label(
                    badge_row,
                    text=badge[0],
                    cursor="hand2",
                    padding=(6, 3),
                    relief="solid",
                )
                label.pack(side="left", padx=(0, 6))
                label.bind(
                    "<Button-1>",
                    lambda _event, target=badge[1]: self._open_readme_link(target),
                )
                return
            badge_row = None

            image_match = re.match(r"!\[[^\]]*\]\(([^)]+)\)", stripped)
            if image_match:
                image_path = (README_PATH.parent / image_match.group(1)).resolve()
                if image_path.exists():
                    preview = tk.PhotoImage(file=str(image_path))
                    label = ttk.Label(frame, image=preview)
                    label.image = preview
                    label.pack(anchor="w", pady=(8, 4))
                else:
                    ttk.Label(frame, text=f"Missing image: {image_path.name}").pack(anchor="w", pady=(8, 4))
                return

            heading_match = re.match(r"^(#+)\s+(.*)$", stripped)
            if heading_match:
                level = len(heading_match.group(1))
                text = heading_match.group(2)
                font_size = 14 - min(level - 1, 4)
                label = tk.Label(
                    frame,
                    text=text,
                    font=("Segoe UI", font_size, "bold"),
                    anchor="w",
                    justify="left",
                    background=colors["surface"],
                    foreground=colors["foreground"],
                    padx=6,
                    pady=3,
                )
                label.pack(anchor="w", pady=(10 if level == 1 else 6, 4))
                return

            if stripped.startswith("- ") or stripped.startswith("* "):
                label = tk.Label(
                    frame,
                    text=stripped[2:],
                    justify="left",
                    anchor="w",
                    background=colors["background"],
                    foreground=colors["foreground"],
                )
                label.pack(anchor="w", pady=(2, 2))
                return

            if stripped.startswith("```"):
                return

            label = tk.Label(
                frame,
                text=stripped,
                justify="left",
                anchor="w",
                wraplength=850,
                background=colors["background"],
                foreground=colors["foreground"],
            )
            label.pack(anchor="w", pady=(2, 2))

        for line in self.readme_text().splitlines():
            render_line(line)

        frame.update_idletasks()
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))

        actions = ttk.Frame(container)
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(
            actions,
            text="GitHub repository",
            command=lambda: webbrowser.open(GITHUB_REPOSITORY_URL),
        ).pack(side="left")
        update_status = tk.StringVar(master=popup, value=f"Current version: {APP_VERSION}")
        ttk.Label(actions, textvariable=update_status).pack(side="left", padx=(8, 0))
        update_progress = ttk.Progressbar(actions, length=150, mode="determinate", maximum=100)
        release_button = ttk.Button(actions, text="Open release")
        check_button = ttk.Button(
            actions,
            text="Check for updates",
            command=lambda: self._check_for_updates(
                popup,
                update_status,
                check_button,
                release_button,
                update_progress,
            ),
        )
        check_button.pack(side="left", padx=(8, 0))
        ttk.Checkbutton(
            actions,
            text="Check for updates on startup",
            variable=self.auto_check_updates,
            command=self._persist_auto_check_updates,
        ).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Close", command=popup.destroy).pack(side="right")

    def _check_for_updates(
        self,
        popup: tk.Toplevel,
        update_status: tk.StringVar,
        check_button: ttk.Button,
        release_button: ttk.Button,
        update_progress: ttk.Progressbar,
    ) -> None:
        check_button.configure(state="disabled")
        update_status.set("Checking for updates...")
        update_progress.pack_forget()
        threading.Thread(
            target=self._load_update_status,
            args=(popup, update_status, check_button, release_button, update_progress),
            daemon=True,
        ).start()

    def _load_update_status(
        self,
        popup: tk.Toplevel,
        update_status: tk.StringVar,
        check_button: ttk.Button,
        release_button: ttk.Button,
        update_progress: ttk.Progressbar,
    ) -> None:
        try:
            release = fetch_latest_release_details()
            asset = platform_release_asset(release)
            if is_newer_version(release.tag):
                message = f"Version {release.tag} is available."
            else:
                if is_newer_version(APP_VERSION, release.tag):
                    message = f"Current version {APP_VERSION} is newer than the latest published release ({release.tag})."
                else:
                    message = f"You are using the latest version ({APP_VERSION})."
                release = None
                asset = None
        except UpdateCheckError as error:
            message = f"Update check failed: {error}"
            release = None
            asset = None
        self.events.put(
            (
                "update_check",
                popup,
                update_status,
                check_button,
                release_button,
                update_progress,
                message,
                release,
                asset,
            )
        )

    def _persist_auto_check_updates(self) -> None:
        enabled = self.auto_check_updates.get()
        self.secure_store.set_setting("auto_check_updates_on_startup", "true" if enabled else "false")

    def _check_for_updates_on_startup(self) -> None:
        if not self.auto_check_updates.get():
            return
        threading.Thread(target=self._load_startup_update_status, daemon=True).start()

    def _load_startup_update_status(self) -> None:
        try:
            release = fetch_latest_release_details()
        except UpdateCheckError:
            return
        if is_newer_version(release.tag):
            self.events.put(("startup_update", release, platform_release_asset(release)))

    def _prompt_startup_update(
        self,
        release: LatestRelease,
        asset: ReleaseAsset | None,
    ) -> None:
        if not getattr(sys, "frozen", False) or asset is None or not asset.sha256:
            if messagebox.askyesno(
                "Update available",
                f"Version {release.tag} is available. Open the release page?",
                parent=self.root,
            ):
                webbrowser.open(release.release_url)
            return

        message = f"Version {release.tag} is available. Download and install it now?"
        if self.worker and self.worker.is_alive():
            message += " Active downloads will be interrupted; partial files will be kept for resume."
        if not messagebox.askyesno("Update available", message, parent=self.root):
            return

        popup = tk.Toplevel(self.root)
        popup.title("Downloading update")
        popup.geometry("440x110")
        popup.resizable(False, False)
        popup.transient(self.root)
        colors = self.theme_colors
        popup.configure(background=colors["background"])
        frame = ttk.Frame(popup, padding=12)
        frame.pack(fill="both", expand=True)
        status = tk.StringVar(master=popup, value=f"Downloading {release.tag}: 0%")
        ttk.Label(frame, textvariable=status).pack(fill="x", pady=(0, 6))
        progress = ttk.Progressbar(
            frame,
            length=1,
            mode="determinate",
            maximum=max(asset.size, 1),
        )
        progress.pack(fill="x")
        threading.Thread(
            target=self._download_and_install_update,
            args=(popup, status, None, None, progress, release, asset),
            daemon=True,
        ).start()

    def _confirm_update_install(
        self,
        popup: tk.Toplevel,
        update_status: tk.StringVar,
        check_button: ttk.Button,
        release_button: ttk.Button,
        update_progress: ttk.Progressbar,
        release: LatestRelease,
        asset: ReleaseAsset,
    ) -> None:
        if not asset.sha256:
            messagebox.showerror(
                "Update unavailable",
                "The release does not provide a verifiable SHA-256 digest.",
                parent=popup,
            )
            return
        message = f"Download and install version {release.tag}? The app will restart when the update is ready."
        if self.worker and self.worker.is_alive():
            message += " Active downloads will be interrupted; partial files will be kept for resume."
        if not messagebox.askyesno("Install update", message, parent=popup):
            return
        check_button.configure(state="disabled")
        release_button.configure(state="disabled")
        update_progress.configure(maximum=max(asset.size, 1), value=0)
        update_progress.pack(side="left", padx=(8, 0))
        update_status.set(f"Downloading {release.tag}: 0%")
        threading.Thread(
            target=self._download_and_install_update,
            args=(popup, update_status, check_button, release_button, update_progress, release, asset),
            daemon=True,
        ).start()

    def _download_and_install_update(
        self,
        popup: tk.Toplevel,
        update_status: tk.StringVar,
        check_button: ttk.Button | None,
        release_button: ttk.Button | None,
        update_progress: ttk.Progressbar,
        release: LatestRelease,
        asset: ReleaseAsset,
    ) -> None:
        staged_path = None
        try:
            if not getattr(sys, "frozen", False):
                raise UpdateInstallError("Automatic installation is available only in packaged builds.")
            target_path = Path(sys.executable).resolve()
            staged_path = download_update_asset(
                asset,
                target_path.parent,
                lambda received, total: self.events.put(
                    ("update_progress", popup, update_status, update_progress, release.tag, received, total)
                ),
            )
            self.events.put(
                ("update_downloaded", popup, update_status, check_button, release_button, update_progress, staged_path, release.tag)
            )
        except Exception as error:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)
            self.events.put(
                ("update_download_failed", popup, update_status, check_button, release_button, update_progress, str(error))
            )

    def _show_columns_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Columns")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        dialog.geometry("440x390")

        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        tree = ttk.Treeview(
            frame,
            columns=("name", "visibility"),
            show="headings",
            selectmode="browse",
            height=len(COLUMN_ORDER),
        )
        tree.heading("name", text="Column")
        tree.heading("visibility", text="Visibility")
        tree.column("name", width=250, minwidth=190, stretch=True)
        tree.column("visibility", width=85, minwidth=80, stretch=False, anchor="center")
        tree.grid(row=0, column=0, sticky="nsew")

        working_order = list(self.column_order)
        working_visible = set(self.visible_columns)
        visible_variable = tk.BooleanVar(master=dialog)

        def selected_column() -> str | None:
            selected = tree.selection()
            return selected[0] if selected else None

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
            move_up_button.configure(state="normal" if position > 0 else "disabled")
            move_down_button.configure(
                state="normal" if position < len(working_order) - 1 else "disabled"
            )

        def render_order(selected: str | None = None) -> None:
            tree.delete(*tree.get_children())
            for column in working_order:
                tree.insert(
                    "",
                    "end",
                    iid=column,
                    values=(self.column_labels[column], "Visible" if column in working_visible else "Hidden"),
                )
            if selected in working_order:
                tree.selection_set(selected)
                tree.focus(selected)
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
            working_order[:] = COLUMN_ORDER
            working_visible.clear()
            working_visible.update(DEFAULT_COLUMNS)
            render_order(DEFAULT_COLUMNS[0])

        def apply_changes() -> None:
            if not working_visible:
                messagebox.showwarning(
                    "Columns required",
                    "At least one column must remain visible.",
                    parent=dialog,
                )
                return
            self.column_order = list(working_order)
            self.visible_columns = [
                column for column in self.column_order if column in working_visible
            ]
            self.table.configure(displaycolumns=self.visible_columns)
            self.secure_store.set_setting("column_order", json.dumps(self.column_order))
            self.secure_store.set_setting("visible_columns", json.dumps(self.visible_columns))
            dialog.destroy()

        move_buttons = ttk.Frame(frame)
        move_buttons.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        move_up_button = ttk.Button(move_buttons, text="Move up", command=lambda: move_column(-1))
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
        ttk.Button(actions, text="Apply", command=apply_changes).pack(side="right", padx=(0, 8))

        tree.bind("<<TreeviewSelect>>", lambda _event: update_controls())
        render_order(working_order[0] if working_order else None)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.grab_set()

    def _sort_by(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        self._refresh_rows()
        self._save_visible_queue_order()

    def _save_visible_queue_order(self) -> None:
        item_ids = tuple(int(item_id) for item_id in self.table.get_children())
        if item_ids != self._last_saved_priority_order:
            self.store.set_priority_order(list(item_ids))
            self._last_saved_priority_order = item_ids

    def _sort_value(self, item, column: str):
        if column == "filename":
            return (item.filename or Path(item.url.split("?", 1)[0]).name).casefold()
        if column == "link":
            return item.url.casefold()
        if column == "host":
            return item.host_message.casefold()
        if column == "status":
            return item.host_status.casefold()
        if column == "size":
            return item.total if item.total is not None else -1
        if column == "remaining":
            return max(0, item.total - item.downloaded) if item.total is not None else -1
        if column == "time_remaining":
            speed = self.item_speeds.get(item.id, 0) or self.average_transfer_speed
            eta = format_item_eta(item.status, item.total, item.downloaded, speed)
            return (eta in {"—", "Calculating"}, eta)
        if column == "eta":
            return self.item_status_messages.get(
                item.id, item.status.replace("_", " ").title()
            ).casefold()
        if column == "verification":
            return size_verification_label(item.status, item.size_verified).casefold()
        return item.id

    def _force_redownload(self) -> None:
        items = self._selected_items()
        eligible = [item for item in items if item.status != "downloading"]
        if not eligible:
            if items:
                messagebox.showinfo("Download active", "Stop the selected downloads before forcing a re-download.")
            return
        count = len(eligible)
        prompt = (
            "Download these files again and replace the existing copies when the new transfers complete?"
            if count > 1
            else "Download this file again and replace the existing copy when the new transfer completes?"
        )
        if not messagebox.askyesno(
            "Force re-download",
            prompt,
        ):
            return
        for item in eligible:
            self.store.set_force_redownload(item.id)
            self.item_speeds.pop(item.id, None)
            if item.filename:
                partial_file = self.output_dir / f".{item.filename}.part"
                partial_file.unlink(missing_ok=True)
            self._log(f"Forced re-download queued for item {item.id}.")
        self._refresh_rows()

    def _select_table_row(self, event: tk.Event) -> str | None:
        if self.table.identify_region(event.x, event.y) not in {"cell", "tree", "nothing"}:
            return None
        row_id = self.table.identify_row(event.y)
        if not row_id:
            selected = self.table.selection()
            if selected:
                self.table.selection_remove(*selected)
            self._selection_anchor = None
            return "break"

        row_ids = list(self.table.get_children(""))
        if row_id not in row_ids:
            return "break"
        shift_pressed = bool(event.state & 0x0001)
        control_pressed = bool(event.state & 0x0004)
        if shift_pressed and self._selection_anchor in row_ids:
            anchor_index = row_ids.index(self._selection_anchor)
            row_index = row_ids.index(row_id)
            start, end = sorted((anchor_index, row_index))
            selection = row_ids[start : end + 1]
            if control_pressed:
                self.table.selection_add(*selection)
            else:
                self.table.selection_set(*selection)
        elif control_pressed:
            if row_id in self.table.selection():
                self.table.selection_remove(row_id)
            else:
                self.table.selection_add(row_id)
            self._selection_anchor = row_id
        else:
            self.table.selection_set(row_id)
            self._selection_anchor = row_id
        self.table.focus_set()
        self.table.focus(row_id)
        return "break"

    def _select_all_table_rows(self, _event: tk.Event | None = None) -> str:
        row_ids = list(self.table.get_children(""))
        if row_ids:
            self.table.selection_set(*row_ids)
            self._selection_anchor = row_ids[0]
            self.table.focus(row_ids[0])
        else:
            self._selection_anchor = None
        return "break"

    def _show_link_menu(self, event: tk.Event) -> str:
        row_id = self.table.identify_row(event.y)
        if not row_id:
            return "break"
        if row_id not in self.table.selection():
            self.table.selection_set(row_id)
            self._selection_anchor = row_id
        elif self._selection_anchor is None:
            self._selection_anchor = row_id
        self.table.focus(row_id)
        items = self._selected_items()
        if not items:
            return "break"
        multiple = len(items) > 1
        self.link_menu.entryconfigure(
            1,
            state="normal" if any(item.deepbrid_link for item in items) else "disabled",
            label="Copy Deepbrid links" if multiple else "Copy Deepbrid link",
        )
        if all(item.enabled for item in items):
            toggle_label = "Disable links" if multiple else "Disable link"
        elif not any(item.enabled for item in items):
            toggle_label = "Enable links" if multiple else "Enable link"
        else:
            toggle_label = "Toggle enabled state"
        self.link_menu.entryconfigure(0, label="Copy original links" if multiple else "Copy original link")
        self.link_menu.entryconfigure(2, label=toggle_label)
        self.link_menu.entryconfigure(
            4,
            state="normal" if any(item.status in {"failed", "blocked"} for item in items) else "disabled",
            label="Retry links" if multiple else "Retry link",
        )
        self.link_menu.entryconfigure(
            5,
            state="normal" if any(item.status != "downloading" for item in items) else "disabled",
            label="Force re-download links" if multiple else "Force re-download",
        )
        self.link_menu.entryconfigure(
            7,
            state="normal" if any(item.status != "downloading" for item in items) else "disabled",
            label="Remove links" if multiple else "Remove link",
        )
        self.link_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _remove_link(self) -> None:
        items = self._selected_items()
        eligible = [item for item in items if item.status != "downloading"]
        if not eligible:
            if items:
                messagebox.showinfo("Download active", "Stop the selected transfers before removing their links.")
            return
        count = len(eligible)
        prompt = (
            f"Remove these {count} links from the queue? Any completed files will stay in the download folder."
            if count > 1
            else "Remove this link from the queue? Any completed file will stay in the download folder."
        )
        if not messagebox.askyesno(
            "Remove link",
            prompt,
        ):
            return
        removed_any = False
        for item in eligible:
            if not self.store.remove_item(item.id):
                self.status_text.set("Could not remove the active queue item")
                continue
            if item.filename:
                partial = self.output_dir / f".{item.filename}.part"
                partial.unlink(missing_ok=True)
            self.item_speeds.pop(item.id, None)
            self._log(f"Removed queue item {item.id}.")
            removed_any = True
        if not removed_any:
            return
        self._refresh_rows()
        self._save_visible_queue_order()

    def _retry_item(self) -> None:
        eligible = [item for item in self._selected_items() if item.status in {"failed", "blocked"}]
        for item in eligible:
            self.store.retry_item(item.id)
            self.item_speeds.pop(item.id, None)
            self._log(f"Retry requested for queue item {item.id}.")
        if eligible:
            self._refresh_rows()

    def _copy_original_link(self, _event: tk.Event | None = None) -> str:
        items = self._selected_items()
        if items:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(item.url for item in items))
            self.status_text.set("Original links copied" if len(items) > 1 else "Original link copied")
            self._log("Original link(s) copied to clipboard.")
        return "break"

    def _copy_deepbrid_link(self) -> None:
        links = [item.deepbrid_link for item in self._selected_items() if item.deepbrid_link]
        if links:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(links))
            self.status_text.set("Deepbrid links copied" if len(links) > 1 else "Deepbrid link copied")
            self._log("Deepbrid link(s) copied to clipboard.")

    def _toggle_link_enabled(self) -> None:
        items = self._selected_items()
        for item in items:
            self.store.update(item.id, enabled=not item.enabled)
            if item.enabled and item.id in self.active_cancel_events:
                self.active_cancel_events[item.id].set()
            self._log(f"Queue item {item.id} {'enabled' if not item.enabled else 'disabled'}.")
        if items:
            self._refresh_rows()

    def _toggle_key_visibility(self) -> None:
        visible = self.api_key_entry.cget("show") == "*"
        self.api_key_entry.configure(show="" if visible else "*")
        self.key_visibility_button.configure(text="Hide" if visible else "Show")

    def _show_api_key_dialog(self, title: str, message: str) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        content = ttk.Frame(dialog, padding=16)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text=message, wraplength=400, justify="left").pack(anchor="w")
        link = tk.Label(
            content,
            text=API_KEY_DASHBOARD_URL,
            foreground="#0563C1",
            cursor="hand2",
            font=("TkDefaultFont", 9, "underline"),
        )
        link.pack(anchor="w", pady=(12, 0))
        link.bind("<Button-1>", lambda _event: self._open_api_key_dashboard())

        buttons = ttk.Frame(content)
        buttons.pack(fill="x", pady=(16, 0))
        ttk.Button(buttons, text="OK", command=dialog.destroy).pack(side="right")
        dialog.bind("<Return>", lambda _event: dialog.destroy())
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.update_idletasks()
        dialog.geometry(f"+{self.root.winfo_rootx() + 40}+{self.root.winfo_rooty() + 40}")
        dialog.wait_window()

    @staticmethod
    def _open_api_key_dashboard() -> None:
        webbrowser.open(API_KEY_DASHBOARD_URL)

    def _toggle_console(self) -> None:
        self.console_visible = not self.console_visible
        if self.console_visible:
            self.panes.add(self.console_frame, weight=1)
            self.console_button.configure(text="Hide console")
            self.root.after(200, self._position_console_pane)
        else:
            self.panes.forget(self.console_frame)
            self.console_button.configure(text="Show console")

    def _position_console_pane(self) -> None:
        if len(self.panes.panes()) != 2:
            return
        pane_height = self.panes.winfo_height()
        if pane_height > 180:
            top_height = min(pane_height - 125, max(90, int(pane_height * 0.62)))
            self.panes.sashpos(0, top_height)

    def _load_theme_preference(self) -> bool:
        saved_theme = self.secure_store.get_setting("dark_theme")
        return saved_theme == "true"

    def _persist_theme(self) -> None:
        self.secure_store.set_setting("dark_theme", "true" if self.dark_theme else "false")

    def _toggle_theme(self) -> None:
        self.dark_theme = not self.dark_theme
        self._persist_theme()
        self._apply_theme()

    def _apply_theme(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        if self.dark_theme:
            colors = {
                "background": "#202522",
                "surface": "#2b332f",
                "foreground": "#e8eee9",
                "field": "#171c19",
                "accent": "#6d91ff",
                "heading_active": "#3b4841",
                "selection": "#2e426e",
                "scrollbar": "#56615b",
                "scrollbar_active": "#6a766f",
                "scrollbar_trough": "#2b332f",
            }
        else:
            colors = {
                "background": "#edf2ee",
                "surface": "#ffffff",
                "foreground": "#202b25",
                "field": "#ffffff",
                "accent": "#3569f6",
                "heading_active": "#dbe6ff",
                "selection": "#dbe6ff",
                "scrollbar": "#d7ded9",
                "scrollbar_active": "#c3cec8",
                "scrollbar_trough": "#f3f6f4",
            }
        self.theme_colors = colors
        self.root.configure(background=colors["background"])
        style.configure(".", background=colors["background"], foreground=colors["foreground"])
        style.configure("TFrame", background=colors["background"])
        style.configure("TLabel", background=colors["background"], foreground=colors["foreground"])
        style.configure("TButton", background=colors["surface"], foreground=colors["foreground"], padding=(9, 5))
        style.map(
            "TButton",
            background=[("active", colors["accent"])],
            foreground=[("active", colors["foreground"])],
        )
        style.configure(
            "TCheckbutton",
            background=colors["background"],
            foreground=colors["foreground"],
            indicatorcolor=colors["field"],
        )
        style.map(
            "TCheckbutton",
            background=[("active", colors["heading_active"])],
            foreground=[("active", colors["foreground"])],
            indicatorcolor=[("selected", colors["accent"]), ("!selected", colors["field"])],
        )
        style.configure("TEntry", fieldbackground=colors["field"], foreground=colors["foreground"])
        style.configure("Treeview", background=colors["surface"], fieldbackground=colors["surface"], foreground=colors["foreground"], rowheight=25)
        style.configure("Treeview.Heading", background=colors["background"], foreground=colors["foreground"])
        style.map(
            "Treeview.Heading",
            background=[("active", colors["heading_active"])],
            foreground=[("active", colors["foreground"])],
        )
        style.map("Treeview", background=[("selected", colors["selection"])], foreground=[("selected", colors["foreground"])])
        style.configure("Horizontal.TProgressbar", troughcolor=colors["surface"], background=colors["accent"])
        style.configure(
            "TScrollbar",
            background=colors["scrollbar"],
            troughcolor=colors["scrollbar_trough"],
            bordercolor=colors["scrollbar_trough"],
            arrowcolor=colors["foreground"],
            darkcolor=colors["scrollbar"],
            lightcolor=colors["scrollbar"],
        )
        style.map(
            "TScrollbar",
            background=[("active", colors["scrollbar_active"]), ("pressed", colors["scrollbar_active"])],
        )
        self.links_input.configure(
            background=colors["field"],
            foreground=colors["foreground"],
            insertbackground=colors["foreground"],
            selectbackground=colors["selection"],
            selectforeground=colors["foreground"],
        )
        self._apply_link_input_color()
        self.console.configure(
            background=colors["field"],
            foreground=colors["foreground"],
            insertbackground=colors["foreground"],
            selectbackground=colors["selection"],
            selectforeground=colors["foreground"],
        )
        self.brand_logo_label.configure(
            image=self.brand_logo_dark if self.dark_theme else self.brand_logo_light
        )
        self.theme_button.configure(text="Light mode" if self.dark_theme else "Dark mode")
        self._log("Dark theme enabled." if self.dark_theme else "Light theme enabled.")

    def _log(self, message: str) -> None:
        self.events.put(("log", time.strftime("%H:%M:%S"), message))

    def _install_console_capture(self) -> None:
        self._previous_stdout = sys.stdout
        self._previous_stderr = sys.stderr
        self._previous_excepthook = sys.excepthook
        self._previous_thread_excepthook = threading.excepthook
        self._previous_callback_exception = self.root.report_callback_exception
        self._stdout_capture = _ConsoleStream(self._log)
        self._stderr_capture = _ConsoleStream(self._log)
        self._exception_handler = self._report_exception
        self._thread_exception_handler = self._report_thread_exception
        sys.stdout = self._stdout_capture
        sys.stderr = self._stderr_capture
        sys.excepthook = self._exception_handler
        threading.excepthook = self._thread_exception_handler
        self.root.report_callback_exception = self._exception_handler

    def _report_exception(self, exc_type, exc_value, exc_traceback) -> None:
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback)).rstrip()
        self.events.put(("exception", time.strftime("%H:%M:%S"), details))

    def _report_thread_exception(self, args) -> None:
        self._report_exception(args.exc_type, args.exc_value, args.exc_traceback)

    def _restore_console_capture(self) -> None:
        if sys.stdout is self._stdout_capture:
            sys.stdout = self._previous_stdout
        if sys.stderr is self._stderr_capture:
            sys.stderr = self._previous_stderr
        if sys.excepthook is self._exception_handler:
            sys.excepthook = self._previous_excepthook
        if threading.excepthook is self._thread_exception_handler:
            threading.excepthook = self._previous_thread_excepthook
        if self.root.report_callback_exception == self._exception_handler:
            self.root.report_callback_exception = self._previous_callback_exception

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        api_key = self.api_key.get().strip()
        key_error = validate_api_key(api_key)
        if key_error:
            self._show_api_key_dialog("API key required", key_error)
            self.api_key_entry.focus_set()
            return
        folder_error = validate_output_folder(self.output_dir_text.get())
        if folder_error:
            messagebox.showerror("Download folder unavailable", folder_error)
            self.status_text.set("Choose a valid download folder")
            self._log(folder_error.replace("\n", " "))
            return
        if self.key_save_after:
            self.root.after_cancel(self.key_save_after)
            self.key_save_after = None
        if not self._persist_api_key():
            messagebox.showerror("Secure storage unavailable", "The API key could not be saved securely.")
            return
        if not self.store.next_item():
            remaining = self.store.list_items()
            if any(item.status == "queued" and item.enabled for item in remaining):
                self.status_text.set("No queued links have an available supported host")
            else:
                self.status_text.set("Queue is empty")
            return
        self.stop_event.clear()
        self.worker = threading.Thread(
            target=self._run_queue,
            args=(api_key, self.output_dir),
            daemon=True,
        )
        self.worker.start()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_text.set("Running")
        self._log("Download queue started.")

    def _stop(self) -> None:
        self.stop_event.set()
        for cancel_event in self.active_cancel_events.values():
            cancel_event.set()
        self.status_text.set("Stopping after the current network read...")
        self.stop_button.configure(state="disabled")
        self._log("Stop requested; current partial download will be kept.")

    def _run_queue(self, api_key: str, output_dir: Path) -> None:
        client = DeepbridClient(api_key, log=lambda message: self._log(message.replace(api_key, "<REDACTED>")))
        try:
            client.validate_api_key()
        except DeepbridError as error:
            self.events.put(("api_key_error", error.status_code, str(error)))
            self.events.put(("worker_done",))
            return
        while not self.stop_event.is_set():
            item = self.store.next_item()
            if item is None:
                break
            item_cancel_event = threading.Event()
            self.active_cancel_events[item.id] = item_cancel_event
            guessed_name = item.filename or safe_filename(None, item.url, item.id)
            existing_path = output_dir / guessed_name
            if not item.force and existing_path.is_file():
                existing_size = existing_path.stat().st_size
                self.store.update(
                    item.id,
                    status="skipped",
                    filename=guessed_name,
                    downloaded=existing_size,
                    total=existing_size,
                    error=None,
                    force=0,
                )
                self._log(f"Skipped {guessed_name}; the file already exists in {output_dir}.")
                self.events.put(("refresh",))
                self.active_cancel_events.pop(item.id, None)
                continue
            self.store.update(item.id, status="generating", error=None)
            self._log(f"Starting queue item {item.id}: {item.url}")
            self.events.put(("status", item.id, "Generating premium link..."))
            self.events.put(("refresh",))
            generated_url = None
            returned_name = None
            quick_attempts = 0
            blocked = False

            while (
                not self.stop_event.is_set()
                and not item_cancel_event.is_set()
                and generated_url is None
            ):
                try:
                    generated_url, returned_name = client.generate_link(item.url)
                except DeepbridError as error:
                    quick_attempts += 1
                    self._log(f"Link generation attempt {quick_attempts} failed: {error}")
                    if not error.retryable:
                        blocked = True
                        self.store.update(
                            item.id,
                            status="blocked",
                            error=self._stored_error(error),
                        )
                        self.events.put(("status", item.id, "Blocked by Deepbrid"))
                        self._log("Deepbrid marked this response non-retryable. The queue is paused; contact Deepbrid support.")
                        break
                    if quick_attempts < 5:
                        status = f"Link retry {quick_attempts + 1}/5 in 3s"
                        delay = 3
                    else:
                        status = "Retrying link in 1 hour"
                        delay = 3600
                    self.store.update(
                        item.id,
                        status="retrying",
                        error=self._stored_error(error),
                    )
                    self.events.put(("status", item.id, status))
                    if item_cancel_event.wait(delay) or self.stop_event.is_set():
                        break

            if self.stop_event.is_set():
                self.store.update(item.id, status="queued")
                self.active_cancel_events.pop(item.id, None)
                self._log(f"Queue item {item.id} paused.")
                break
            if item_cancel_event.is_set():
                self.store.update(item.id, status="queued")
                self.active_cancel_events.pop(item.id, None)
                self._log(f"Queue item {item.id} was disabled before transfer; moving to the next priority.")
                continue
            if blocked:
                self.active_cancel_events.pop(item.id, None)
                self.events.put(("refresh",))
                break
            if not generated_url:
                self.active_cancel_events.pop(item.id, None)
                break

            filename = item.filename or safe_filename(returned_name, item.url, item.id)
            destination = output_dir / filename
            self.store.update(
                item.id,
                filename=filename,
                deepbrid_link=generated_url,
                status="downloading",
                error=None,
            )
            if destination.is_file() and not item.force:
                existing_size = destination.stat().st_size
                self.store.update(
                    item.id,
                    status="skipped",
                    downloaded=existing_size,
                    total=existing_size,
                    force=0,
                )
                self._log(f"Skipped {filename}; the file already exists in {output_dir}.")
                self.events.put(("refresh",))
                self.active_cancel_events.pop(item.id, None)
                continue
            self._log(f"Downloading {filename} to {output_dir}")
            self.events.put(("status", item.id, "Downloading file..."))

            last_database_update = 0.0
            last_ui_update = 0.0
            last_speed_update = time.monotonic()
            last_speed_bytes = item.downloaded

            def progress(downloaded: int, total: int | None) -> None:
                nonlocal last_database_update, last_ui_update, last_speed_update, last_speed_bytes
                now = time.monotonic()
                if now - last_database_update >= 1:
                    self.store.update(item.id, downloaded=downloaded, total=total)
                    last_database_update = now
                if now - last_ui_update >= 1 or (total and downloaded >= total):
                    elapsed = now - last_speed_update
                    speed = (downloaded - last_speed_bytes) / elapsed if elapsed > 0 else 0
                    if elapsed >= 1:
                        last_speed_update = now
                        last_speed_bytes = downloaded
                    self.events.put(("progress", item.id, downloaded, total, speed))
                    last_ui_update = now

            size_verified = False

            def record_size_verification(verified: bool) -> None:
                nonlocal size_verified
                size_verified = verified

            try:
                completed = client.download(
                    generated_url,
                    filename,
                    output_dir,
                    lambda: self.stop_event.is_set() or item_cancel_event.is_set(),
                    progress,
                    overwrite_existing=item.force,
                    on_size_verified=record_size_verification,
                )
                if completed:
                    final_size = (output_dir / filename).stat().st_size
                    self.store.update(
                        item.id,
                        status="completed",
                        size_verified=size_verified,
                        downloaded=final_size,
                        total=final_size,
                        error=None,
                        force=0,
                    )
                    self.events.put(("status", item.id, "Completed"))
                    self._log(f"Completed {filename} ({final_size} bytes).")
                else:
                    partial_path = output_dir / f".{filename}.part"
                    partial_size = partial_path.stat().st_size if partial_path.exists() else 0
                    self.store.update(item.id, status="queued", downloaded=partial_size)
                    self._log(f"Paused {filename} at {partial_size} bytes.")
            except (DeepbridError, OSError) as error:
                self.store.update(
                    item.id,
                    status="failed",
                    error=self._stored_error(error),
                )
                self.events.put(("status", item.id, "Failed"))
                self._log(f"Download failed for {filename}: {error}")

            self.active_cancel_events.pop(item.id, None)
            self.events.put(("refresh",))
            if self.stop_event.is_set():
                break

        self.events.put(("worker_done",))

    def _refresh_rows(self) -> None:
        items = self.store.list_items()
        existing = set(self.table.get_children())
        seen: set[str] = set()
        items.sort(
            key=lambda item: self._sort_value(item, self.sort_column),
            reverse=self.sort_reverse,
        )
        active_speeds = [
            self.item_speeds.get(item.id, 0)
            for item in items
            if item.status == "downloading"
        ]
        for position, item in enumerate(items):
            row_id = str(item.id)
            seen.add(row_id)
            host_status = item.host_status.replace("_", " ").title()
            if item.status == "retrying":
                queue_status = self.item_status_messages.get(item.id, "Retrying")
            else:
                queue_status = item.status.replace("_", " ").title()
                self.item_status_messages.pop(item.id, None)
            if not item.enabled:
                queue_status = "Disabled"
            filename = item.filename or Path(item.url.split("?", 1)[0]).name or item.url
            remaining = max(0, item.total - item.downloaded) if item.total is not None else None
            values = (
                filename,
                item.url,
                item.host_message,
                host_status,
                f"{format_bytes(item.downloaded)} / {format_bytes(item.total)}",
                format_bytes(remaining),
                format_item_eta(
                    item.status,
                    item.total,
                    item.downloaded,
                    self.item_speeds.get(item.id, 0) or self.average_transfer_speed,
                ),
                queue_status,
                size_verification_label(item.status, item.size_verified),
            )
            if row_id in existing:
                self.table.item(row_id, values=values)
                self.table.move(row_id, "", position)
            else:
                self.table.insert("", "end", iid=row_id, values=values)
        for row_id in existing - seen:
            self.table.delete(row_id)
        self._save_visible_queue_order()
        self._update_queue_progress(items)
        self._update_total_eta(
            items,
            self.average_transfer_speed or max(active_speeds, default=0),
        )

    @staticmethod
    def _average_completed_size(items) -> float | None:
        completed_sizes = [
            item.total
            for item in items
            if item.status == "completed" and item.total is not None and item.total > 0
        ]
        return sum(completed_sizes) / len(completed_sizes) if completed_sizes else None

    def _update_queue_progress(self, items) -> None:
        enabled_items = [item for item in items if item.enabled]
        total_count = len(enabled_items)
        if not total_count:
            self.progress_value.set(0)
            self.queue_progress_text.set("Queue: 0/0 files (0%)")
            return

        settled_statuses = {"completed", "skipped", "failed", "blocked"}
        settled_count = sum(item.status in settled_statuses for item in enabled_items)
        active_item = next(
            (item for item in enabled_items if item.status == "downloading"),
            None,
        )
        active_fraction = 0.0
        if active_item is not None:
            expected_size = active_item.total or self._average_completed_size(items)
            if expected_size is not None and expected_size > 0:
                active_fraction = min(
                    1.0,
                    max(0.0, active_item.downloaded / expected_size),
                )

        progress_percent = min(
            100.0,
            (settled_count + active_fraction) * 100 / total_count,
        )
        self.progress_value.set(progress_percent)
        self.queue_progress_text.set(
            f"Queue: {settled_count}/{total_count} files ({progress_percent:.0f}%)"
        )

    def _update_total_eta(self, items, speed: float) -> None:
        pending = [
            item for item in items
            if item.enabled and item.status in {"queued", "downloading", "generating", "retrying"}
        ]
        if not pending:
            self.total_eta_text.set("Total remaining: 0s")
            return
        unknown_sizes = sum(item.total is None for item in pending)
        bytes_left = sum(
            max(0, item.total - item.downloaded)
            for item in pending
            if item.total is not None
        )
        average_completed_size = self._average_completed_size(items)
        if unknown_sizes and average_completed_size is not None:
            bytes_left += round(unknown_sizes * average_completed_size)

        if bytes_left == 0:
            if unknown_sizes:
                self.total_eta_text.set(f"Total remaining: calculating ({unknown_sizes} size(s) unknown)")
            else:
                self.total_eta_text.set("Total remaining: 0s")
        elif speed > 0:
            estimate = format_duration(bytes_left / speed)
            if unknown_sizes:
                if average_completed_size is not None:
                    average_size_text = format_bytes(round(average_completed_size))
                    self.total_eta_text.set(
                        f"Total remaining: ~{estimate} ({unknown_sizes} unknown @ avg {average_size_text})"
                    )
                else:
                    self.total_eta_text.set(f"Known remaining: {estimate}; {unknown_sizes} size(s) unknown")
            else:
                self.total_eta_text.set(f"Total remaining: {estimate}")
        else:
            if unknown_sizes:
                self.total_eta_text.set(f"Known remaining: calculating; {unknown_sizes} size(s) unknown")
            else:
                self.total_eta_text.set("Total remaining: calculating")

    def _process_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "update_check":
                _, popup, update_status, check_button, release_button, update_progress, message, release, asset = event
                try:
                    if not popup.winfo_exists():
                        continue
                    check_button.configure(state="normal")
                    update_status.set(message)
                    update_progress.pack_forget()
                    if release is not None:
                        if getattr(sys, "frozen", False) and asset is not None and asset.sha256:
                            release_button.configure(
                                text="Download and install",
                                command=lambda window=popup, status=update_status, check=check_button,
                                button=release_button, progress=update_progress, found_release=release,
                                found_asset=asset: self._confirm_update_install(
                                    window, status, check, button, progress, found_release, found_asset
                                ),
                            )
                        else:
                            release_button.configure(
                                text="Open release",
                                command=lambda target=release.release_url: webbrowser.open(target),
                            )
                        if not release_button.winfo_manager():
                            release_button.pack(side="left", padx=(8, 0))
                    else:
                        release_button.pack_forget()
                except tk.TclError:
                    continue
            elif event[0] == "update_progress":
                _, popup, update_status, update_progress, version, received, total = event
                try:
                    if not popup.winfo_exists():
                        continue
                    maximum = max(total, 1)
                    percent = min(100, received * 100 / maximum)
                    update_progress.configure(maximum=maximum, value=received)
                    update_status.set(f"Downloading {version}: {percent:.0f}%")
                except tk.TclError:
                    continue
            elif event[0] == "update_downloaded":
                _, popup, update_status, _check_button, _release_button, _progress, staged_path, version = event
                try:
                    launch_update_helper(Path(staged_path))
                except UpdateInstallError as error:
                    Path(staged_path).unlink(missing_ok=True)
                    self.status_text.set(f"Update install failed: {error}")
                    self._log(f"Could not install update {version}: {error}")
                    try:
                        if popup.winfo_exists():
                            popup.destroy()
                    except tk.TclError:
                        pass
                    messagebox.showerror("Update failed", str(error), parent=self.root)
                else:
                    self.status_text.set(f"Installing {version}; restarting...")
                    self._log(f"Installing update {version}; restarting the application.")
                    try:
                        if popup.winfo_exists():
                            popup.destroy()
                    except tk.TclError:
                        pass
                    self._close()
                    return
            elif event[0] == "update_download_failed":
                _, popup, update_status, check_button, release_button, update_progress, error = event
                self.status_text.set(f"Update download failed: {error}")
                self._log(f"Update download failed: {error}")
                try:
                    if popup.winfo_exists():
                        update_status.set(f"Download failed: {error}")
                        if check_button is not None:
                            check_button.configure(state="normal")
                        if release_button is not None:
                            release_button.configure(state="normal")
                        if check_button is not None or release_button is not None:
                            update_progress.pack_forget()
                        messagebox.showerror("Update download failed", error, parent=popup)
                except tk.TclError:
                    pass
            elif event[0] == "startup_update":
                _, release, asset = event
                self._prompt_startup_update(release, asset)
            elif event[0] == "refresh":
                self._refresh_rows()
            elif event[0] == "hosts":
                _, hosts, limits, limits_error, limits_error_status = event
                self.hosts = hosts
                self.host_limits = limits
                self.secure_store.set_setting("hosts", json.dumps(self.hosts))
                self._apply_host_statuses(self.hosts)
                self.refresh_hosts_button.configure(state="normal")
                self.add_links_button.configure(state="normal")
                self.status_text.set(f"Loaded {len(self.hosts)} supported hosts")
                self._log(f"Loaded availability for {len(self.hosts)} supported hosts.")
                self._refresh_rows()
                if self._hosts_popup_update is not None:
                    self._hosts_popup_update(
                        self.hosts,
                        self.host_limits,
                        limits_error,
                        status_code=limits_error_status,
                    )
                if self.store.recovered_work and self.api_key.get().strip():
                    self.root.after(300, self._start)
            elif event[0] == "hosts_error":
                _, error_message, status_code = event
                self.refresh_hosts_button.configure(state="normal")
                self.status_text.set(f"Host refresh failed: {error_message[:90]}")
                self._log(error_message)
                self.add_links_button.configure(state="normal" if self.hosts else "disabled")
                self._refresh_rows()
                if self._hosts_popup_update is not None:
                    self._hosts_popup_update(
                        self.hosts,
                        self.host_limits,
                        None,
                        refresh_error=error_message,
                        status_code=status_code,
                    )
            elif event[0] in {"log", "exception"}:
                _, timestamp, message = event
                if event[0] == "exception" and not self.console_visible:
                    self._toggle_console()
                self.console.configure(state="normal")
                self.console.insert("end", f"[{timestamp}] {message}\n")
                self.console.see("end")
                self.console.configure(state="disabled")
            elif event[0] == "status":
                _, item_id, message = event
                if message.startswith("Link retry "):
                    self.item_status_messages[item_id] = message
                else:
                    self.item_status_messages.pop(item_id, None)
                self.status_text.set(message)
                self._refresh_rows()
            elif event[0] == "api_key_error":
                _, status_code, message = event
                title = "Invalid API key" if status_code == 401 else "API key validation failed"
                self.status_text.set("Invalid API key" if status_code == 401 else "Could not validate API key")
                self._log(message)
                if status_code == 401:
                    self._show_api_key_dialog(title, message)
                else:
                    messagebox.showerror(title, message)
            elif event[0] == "progress":
                _, item_id, downloaded, total, speed = event
                if speed > 0:
                    self.item_speeds[item_id] = smooth_rate(self.item_speeds.get(item_id, 0), speed, 0.2)
                    self.average_transfer_speed = smooth_rate(self.average_transfer_speed, speed)
                self._refresh_rows()
            elif event[0] == "worker_done":
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.status_text.set("Stopped" if self.stop_event.is_set() else "Queue complete")
                self._refresh_rows()
        now = time.monotonic()
        if now - self.last_eta_refresh >= 1:
            if self.worker and self.worker.is_alive():
                self._refresh_rows()
            self.last_eta_refresh = now
        self.root.after(1000, self._process_events)

    @staticmethod
    def _format_progress(downloaded: int, total: int | None) -> str:
        if total:
            return f"{min(100, downloaded * 100 / total):.1f}%"
        if downloaded:
            return f"{downloaded / (1024 * 1024):.1f} MB"
        return ""

    @staticmethod
    def _stored_error(error: Exception) -> str:
        if isinstance(error, DeepbridError) and error.status_code is not None:
            return f"HTTP {error.status_code}"
        return type(error).__name__

    def _close(self) -> None:
        self._persist_theme()
        self.stop_event.set()
        self._restore_console_capture()
        self.root.destroy()


def _show_startup_error(root: tk.Tk, details: str) -> None:
    for child in root.winfo_children():
        child.destroy()
    root.title("Deepbrid Downloader - startup error")
    root.geometry("900x500")
    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    output = tk.Text(frame, wrap="word")
    output.grid(row=0, column=0, sticky="nsew")
    scrollbar = ttk.Scrollbar(frame, orient="vertical", command=output.yview)
    scrollbar.grid(row=0, column=1, sticky="ns")
    output.configure(yscrollcommand=scrollbar.set)
    output.insert("end", f"Application startup failed:\n\n{details}")
    output.configure(state="disabled")
    ttk.Button(frame, text="Close", command=root.destroy).grid(row=1, column=0, sticky="e", pady=(8, 0))


def main() -> None:
    try:
        instance_lock = acquire_single_instance(APP_DATA_DIR)
    except Exception:
        root = tk.Tk()
        _show_startup_error(root, traceback.format_exc())
        root.mainloop()
        return
    if instance_lock is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(
            "Already running",
            "Deepbrid Downloader is already running. Switch to the open window instead.",
            parent=root,
        )
        root.destroy()
        return
    root = tk.Tk()
    try:
        DownloaderApp(root)
    except Exception:
        _show_startup_error(root, traceback.format_exc())
    root.mainloop()


if __name__ == "__main__":
    main()