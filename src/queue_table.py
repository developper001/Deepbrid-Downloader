from __future__ import annotations

import math
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .deepbrid_client import safe_filename
from .queue_store import QueueItem
from .queue_types import QueueSource, QueueStatus

COLUMN_ORDER = (
    "filename",
    "file_path",
    "extension",
    "link",
    "host",
    "status",
    "size",
    "progress",
    "progress_percentage",
    "remaining",
    "time_remaining",
    "eta",
    "verification",
)

COLUMN_LABELS = {
    "filename": "File name",
    "file_path": "Full path",
    "extension": "Extension",
    "link": "Original link",
    "host": "Host",
    "status": "Host status",
    "size": "Downloaded / total",
    "progress": "Progress bar",
    "progress_percentage": "Progress (%)",
    "remaining": "Remaining",
    "time_remaining": "ETA",
    "eta": "Status",
    "verification": "Verification",
}


def format_bytes(value: int | None) -> str:
    if value is None or value < 0:
        return "Unknown"
    amount = float(value)
    for suffix in ("B", "KB", "MB", "GB", "TB"):
        if amount < 1024 or suffix == "TB":
            return f"{int(amount)} B" if suffix == "B" else f"{amount:.1f} {suffix}"
        amount /= 1024
    return "Unknown"


def progress_indicator_values(
    status: str,
    total: int | None,
    downloaded: int,
) -> tuple[float | None, str]:
    if status == QueueStatus.COMPLETED:
        return 1.0, "100%"
    elif total is None or total <= 0:
        return None, "—"
    fraction = min(1.0, max(0.0, downloaded / total))
    return fraction, f"{round(fraction * 100)}%"


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
    if status in {QueueStatus.COMPLETED, QueueStatus.SKIPPED}:
        return "0s"
    if status != QueueStatus.DOWNLOADING:
        return "—"
    if total is None:
        return "Calculating"
    remaining = max(0, total - downloaded)
    if remaining == 0:
        return "0s"
    return format_duration(remaining / speed) if speed > 0 else "Calculating"


def size_verification_label(status: str, size_verified: bool) -> str:
    if status != QueueStatus.COMPLETED:
        return "—"
    return "Verified" if size_verified else "Not verified"


def full_file_path(output_dir: Path, filename: str) -> str:
    return str((output_dir / filename).resolve())


def sort_queue_item(
    item: QueueItem,
    column: str,
    item_speeds: dict[int, float],
    item_status_messages: dict[int, str],
    average_transfer_speed: float,
    output_dir: Path | None = None,
) -> object:
    if column == "filename":
        return (item.filename or Path(item.url.split("?", 1)[0]).name).casefold()
    if column == "file_path":
        filename = item.filename or safe_filename(None, item.url, item.id)
        return full_file_path(output_dir or Path(), filename).casefold()
    if column == "extension":
        filename = item.filename or Path(item.url.split("?", 1)[0]).name
        return Path(filename).suffix.casefold()
    if column == "link":
        return item.url.casefold()
    if column == "host":
        return item.host_message.casefold()
    if column == "status":
        return item.host_status.casefold()
    if column == "size":
        return item.total if item.total is not None else -1
    if column == "progress":
        fraction, _label = progress_indicator_values(
            item.status,
            item.total,
            item.downloaded,
        )
        return fraction if fraction is not None else -1.0
    if column == "progress_percentage":
        fraction, _label = progress_indicator_values(
            item.status,
            item.total,
            item.downloaded,
        )
        return fraction is not None, fraction if fraction is not None else 0.0
    if column == "remaining":
        return max(0, item.total - item.downloaded) if item.total is not None else -1
    if column == "time_remaining":
        speed = item_speeds.get(item.id, 0) or average_transfer_speed
        eta = format_item_eta(item.status, item.total, item.downloaded, speed)
        return (eta in {"—", "Calculating"}, eta)
    if column == "eta":
        return item_status_messages.get(
            item.id, item.status.replace("_", " ").title()
        ).casefold()
    if column == "verification":
        return size_verification_label(item.status, item.size_verified).casefold()
    return item.id


class QueueTableView:
    def __init__(self, app, parent: ttk.Panedwindow, visible_columns: list[str]):
        self.app = app
        self.progress_indicator_values: dict[str, tuple[float | None, str]] = {}
        self.progress_indicator_canvases: dict[str, tk.Canvas] = {}
        self._layout_pending = False

        table_frame = ttk.Frame(parent)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.table = ttk.Treeview(
            table_frame,
            columns=COLUMN_ORDER,
            show="headings",
            selectmode="extended",
        )
        for column, label in COLUMN_LABELS.items():
            self.table.heading(
                column,
                text=label,
                command=lambda key=column: app._sort_by(key),
            )
        self.table.column("filename", width=250, minwidth=140, stretch=False)
        self.table.column("file_path", width=420, minwidth=180, stretch=False)
        self.table.column("extension", width=90, minwidth=75, stretch=False)
        self.table.column("link", width=330, minwidth=150, stretch=False)
        self.table.column("host", width=135, minwidth=100, stretch=False)
        self.table.column("status", width=120, minwidth=90, stretch=False)
        self.table.column("size", width=145, minwidth=120, stretch=False, anchor="e")
        self.table.column("progress", width=145, minwidth=125, stretch=False, anchor="center")
        self.table.column("progress_percentage", width=95, minwidth=75, stretch=False, anchor="center")
        self.table.column("remaining", width=105, minwidth=90, stretch=False, anchor="e")
        self.table.column("time_remaining", width=95, minwidth=80, stretch=False, anchor="e")
        self.table.column("eta", width=170, minwidth=130, stretch=False)
        self.table.column("verification", width=110, minwidth=95, stretch=False)
        self.table.configure(displaycolumns=visible_columns)
        self.table.grid(row=0, column=0, sticky="nsew")

        vertical_scrollbar = ttk.Scrollbar(
            table_frame,
            orient="vertical",
            command=self.table.yview,
        )
        vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal_scrollbar = ttk.Scrollbar(
            table_frame,
            orient="horizontal",
            command=self.table.xview,
        )
        horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        self.table.configure(
            yscrollcommand=lambda first, last: app._update_table_scrollbar(
                vertical_scrollbar, first, last
            ),
            xscrollcommand=lambda first, last: app._update_table_scrollbar(
                horizontal_scrollbar,
                first,
                last,
                show_if_needed=True,
            ),
        )
        vertical_scrollbar.configure(command=lambda *args: app._scroll_table("y", *args))
        horizontal_scrollbar.configure(command=lambda *args: app._scroll_table("x", *args))

        for sequence in (
            "<Configure>",
            "<B1-Motion>",
            "<ButtonRelease-1>",
            "<Map>",
            "<Expose>",
            "<<TreeviewSelect>>",
            "<MouseWheel>",
            "<Button-4>",
            "<Button-5>",
        ):
            self.table.bind(sequence, self.schedule_layout, add="+")

        parent.add(table_frame, weight=4)

    def refresh(self) -> None:
        app = self.app
        items = app.store.list_items()
        table = self.table
        existing = set(table.get_children())
        seen: set[str] = set()
        items.sort(
            key=lambda item: app._sort_value(item, app.sort_column),
            reverse=app.sort_reverse,
        )
        active_speeds = [
            app.item_speeds.get(item.id, 0)
            for item in items
            if item.status == QueueStatus.DOWNLOADING
        ]
        for position, item in enumerate(items):
            row_id = str(item.id)
            seen.add(row_id)
            host_status = item.host_status.replace("_", " ").title()
            if item.status == QueueStatus.RETRYING:
                queue_status = app.item_status_messages.get(item.id, "Retrying")
            else:
                queue_status = item.status.replace("_", " ").title()
                app.item_status_messages.pop(item.id, None)
            if not item.enabled:
                queue_status = "Disabled"
            filename = item.filename or Path(item.url.split("?", 1)[0]).name or item.url
            path_filename = item.filename or Path(item.url.split("?", 1)[0]).name
            if not path_filename:
                path_filename = safe_filename(None, item.url, item.id)
            file_path = full_file_path(app.output_dir, path_filename)
            extension = Path(filename).suffix.casefold()
            remaining = max(0, item.total - item.downloaded) if item.total is not None else None
            self.progress_indicator_values[row_id] = progress_indicator_values(
                item.status,
                item.total,
                item.downloaded,
            )
            values = (
                filename,
                file_path,
                extension,
                (
                    f"Torrent job {item.remote_job_id}"
                    if item.source == QueueSource.TORRENT_CLOUD and item.remote_job_id
                    else "Torrent upload"
                    if item.source == QueueSource.TORRENT_CLOUD
                    else item.url
                ),
                item.host_message,
                host_status,
                f"{format_bytes(item.downloaded)} / {format_bytes(item.total)}",
                "",
                self.progress_indicator_values[row_id][1],
                format_bytes(remaining),
                format_item_eta(
                    item.status,
                    item.total,
                    item.downloaded,
                    app.item_speeds.get(item.id, 0) or app.average_transfer_speed,
                ),
                queue_status,
                size_verification_label(item.status, item.size_verified),
            )
            if row_id in existing:
                table.item(row_id, values=values)
                table.move(row_id, "", position)
            else:
                table.insert("", "end", iid=row_id, values=values)
        for row_id in existing - seen:
            table.delete(row_id)
        for row_id in set(self.progress_indicator_values) - seen:
            self.progress_indicator_values.pop(row_id, None)
        self.position_indicators()
        app._save_visible_queue_order()
        app._update_queue_progress(items)
        app._update_total_eta(
            items,
            app.average_transfer_speed or max(active_speeds, default=0),
        )

    def schedule_layout(self, _event: tk.Event | None = None) -> None:
        if self._layout_pending:
            return
        self._layout_pending = True
        self.app.root.after_idle(self.position_indicators)

    def forward_cell_event(self, event: tk.Event, sequence: str) -> str:
        x = event.x_root - self.table.winfo_rootx()
        y = event.y_root - self.table.winfo_rooty()
        options = {"x": x, "y": y, "state": event.state}
        if sequence == "<MouseWheel>":
            options["delta"] = event.delta
        self.table.event_generate(sequence, **options)
        return "break"

    def position_indicators(self) -> None:
        self._layout_pending = False
        table = self.table
        if not hasattr(self.app, "theme_colors") or not table.winfo_ismapped():
            return
        if "progress" not in table["displaycolumns"]:
            visible_rows: set[str] = set()
        else:
            children = table.get_children("")
            first, last = (float(value) for value in table.yview())
            start = max(0, int(first * len(children)))
            end = min(len(children), math.ceil(last * len(children)))
            visible_rows = set(children[start:end])

        positioned_rows: set[str] = set()
        for row_id in visible_rows:
            bounds = table.bbox(row_id, "progress")
            if not bounds:
                continue
            x, y, width, height = bounds
            height = min(height, table.winfo_height() - y)
            if height <= 0:
                continue
            positioned_rows.add(row_id)
            canvas = self.progress_indicator_canvases.get(row_id)
            if canvas is None:
                canvas = tk.Canvas(
                    table,
                    highlightthickness=0,
                    borderwidth=0,
                    takefocus=False,
                )
                for sequence in (
                    "<Button-1>",
                    "<Button-2>",
                    "<Button-3>",
                    "<MouseWheel>",
                    "<Button-4>",
                    "<Button-5>",
                ):
                    canvas.bind(
                        sequence,
                        lambda event, event_sequence=sequence: self.forward_cell_event(
                            event,
                            event_sequence,
                        ),
                    )
                self.progress_indicator_canvases[row_id] = canvas
            canvas.place(x=x, y=y, width=width, height=height, bordermode="inside")
            selected = row_id in table.selection()
            background = (
                self.app.theme_colors["selection"]
                if selected
                else self.app.theme_colors["surface"]
            )
            canvas.configure(background=background)
            canvas.delete("all")
            fraction, _label = self.progress_indicator_values[row_id]
            bar_width = max(0, width - 8)
            bar_height = 10
            bar_x = 4
            bar_y = max(0, (height - bar_height) // 2)
            if fraction is not None and bar_width:
                canvas.create_rectangle(
                    bar_x,
                    bar_y,
                    bar_x + bar_width,
                    bar_y + bar_height,
                    fill=self.app.theme_colors["field"],
                    outline="",
                )
                filled_width = round((bar_width - 1) * fraction)
                if fraction >= 1:
                    filled_width = bar_width - 1
                if filled_width:
                    canvas.create_rectangle(
                        bar_x + 1,
                        bar_y + 1,
                        bar_x + 1 + filled_width,
                        bar_y + bar_height - 1,
                        fill=self.app.theme_colors["accent"],
                        outline="",
                    )
                canvas.create_rectangle(
                    bar_x,
                    bar_y,
                    bar_x + bar_width,
                    bar_y + bar_height,
                    fill="",
                    outline=self.app.theme_colors["progress_border"],
                )
        for row_id in set(self.progress_indicator_canvases) - positioned_rows:
            self.progress_indicator_canvases.pop(row_id).destroy()
