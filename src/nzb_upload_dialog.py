from __future__ import annotations

import threading
import tkinter as tk
from queue import Queue
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Protocol

from .app_events import BackgroundEvent, UsenetUploadFinishedEvent
from .deepbrid_client import MAX_NZB_FILE_SIZE, DeepbridClient, DeepbridError
from .queue_types import QueueSource
from .remote_jobs import UsenetUpload
from .usenet_finder import is_valid_finder_link


class NzbUploadController(Protocol):
    root: tk.Tk
    api_key: tk.StringVar
    events: Queue[BackgroundEvent]

    def add_usenet_links(
        self,
        links: list[tuple[str, str, int | None]],
        *,
        source_label: str = "Usenet Finder",
        source: QueueSource = QueueSource.USENET,
    ) -> tuple[int, int, int]: ...


class NzbUploadDialog:
    def __init__(self, app: NzbUploadController):
        self.app = app
        self._upload_result: UsenetUpload | None = None
        self.dialog = tk.Toplevel(app.root)
        self.dialog.title("Add NZB")
        self.dialog.geometry("760x480")
        self.dialog.minsize(620, 380)
        self.dialog.transient(app.root)
        self.dialog.protocol("WM_DELETE_WINDOW", self.dialog.destroy)

        frame = ttk.Frame(self.dialog, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=1)

        ttk.Label(
            frame,
            text="Upload an NZB file or submit an NZB URL to Deepbrid.",
        ).grid(row=0, column=0, sticky="w")
        url_row = ttk.Frame(frame)
        url_row.grid(row=1, column=0, sticky="ew", pady=(10, 4))
        url_row.columnconfigure(0, weight=1)
        self.url = tk.StringVar(master=self.dialog)
        self.url_entry = ttk.Entry(url_row, textvariable=self.url)
        self.url_entry.grid(row=0, column=0, sticky="ew")
        self.url_button = ttk.Button(
            url_row,
            text="Upload URL",
            command=self._upload_url,
        )
        self.url_button.grid(row=0, column=1, padx=(8, 0))
        self.file_button = ttk.Button(
            frame,
            text="Choose .nzb file",
            command=self._choose_file,
        )
        self.file_button.grid(row=2, column=0, sticky="w", pady=(4, 8))

        self.files = ttk.Treeview(
            frame,
            columns=("name", "size", "availability"),
            show="headings",
            selectmode="extended",
        )
        self.files.heading("name", text="File")
        self.files.heading("size", text="Size")
        self.files.heading("availability", text="Availability")
        self.files.column("name", width=400, minwidth=160, stretch=True)
        self.files.column("size", width=100, minwidth=80, stretch=False, anchor="e")
        self.files.column("availability", width=150, minwidth=120, stretch=False)
        self.files.grid(row=3, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.files.yview)
        scrollbar.grid(row=3, column=1, sticky="ns")
        self.files.configure(yscrollcommand=scrollbar.set)
        self.files.tag_configure("unavailable", foreground="#888888")

        self.status = tk.StringVar(master=self.dialog, value="Ready")
        ttk.Label(frame, textvariable=self.status).grid(
            row=4,
            column=0,
            sticky="w",
            pady=(8, 0),
        )
        actions = ttk.Frame(frame)
        actions.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        self.add_selected_button = ttk.Button(
            actions,
            text="Add selected",
            command=self._add_selected,
            state="disabled",
        )
        self.add_selected_button.pack(side="left")
        self.add_all_button = ttk.Button(
            actions,
            text="Add all available",
            command=self._add_all,
            state="disabled",
        )
        self.add_all_button.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Close", command=self.dialog.destroy).pack(side="right")
        self.dialog.bind("<Escape>", lambda _event: self.dialog.destroy())
        self.dialog.grab_set()
        self.url_entry.focus_set()

    def _choose_file(self) -> None:
        filename = filedialog.askopenfilename(
            parent=self.dialog,
            title="Select an NZB file",
            filetypes=(("NZB files", "*.nzb"),),
        )
        if filename:
            path = Path(filename)
            if path.suffix.casefold() != ".nzb":
                messagebox.showerror(
                    "Invalid NZB file",
                    "Select a file with the .nzb extension.",
                    parent=self.dialog,
                )
                return
            self._start_upload(path=path)

    def _upload_url(self) -> None:
        url = self.url.get().strip()
        if not url:
            messagebox.showinfo(
                "NZB URL required",
                "Enter the HTTP or HTTPS URL of an NZB file.",
                parent=self.dialog,
            )
            return
        self._start_upload(url=url)

    def _start_upload(self, *, path: Path | None = None, url: str | None = None) -> None:
        api_key = self.app.api_key.get().strip()
        if not api_key:
            messagebox.showerror(
                "API key required",
                "Add your Deepbrid API key in Settings before uploading an NZB.",
                parent=self.dialog,
            )
            return
        self._set_busy(True)
        self.status.set("Uploading and processing NZB...")
        thread = threading.Thread(
            target=self._upload,
            args=(api_key, path, url),
            daemon=True,
        )
        thread.start()

    def _upload(self, api_key: str, path: Path | None, url: str | None) -> None:
        try:
            client = DeepbridClient(api_key)
            if path is not None:
                if path.stat().st_size > MAX_NZB_FILE_SIZE:
                    raise ValueError("NZB files must be no larger than 100 MiB.")
                upload = client.upload_nzb_file(path.read_bytes(), path.name)
            elif url is not None:
                upload = client.upload_nzb_url(url)
            else:
                raise ValueError("Choose an NZB file or enter an NZB URL.")
        except (DeepbridError, OSError, ValueError) as error:
            self.app.events.put(
                UsenetUploadFinishedEvent(self.upload_finished, None, str(error))
            )
        else:
            self.app.events.put(
                UsenetUploadFinishedEvent(self.upload_finished, upload, None)
            )

    def upload_finished(self, upload: UsenetUpload | None, error: str | None) -> None:
        try:
            if not self.dialog.winfo_exists():
                return
            self._set_busy(False)
            if error is not None:
                self.status.set(f"Upload failed: {error}")
                messagebox.showerror("NZB upload failed", error, parent=self.dialog)
                return
            if upload is None:
                raise RuntimeError("NZB upload event has no result or error.")
            self._render_files(upload)
        except tk.TclError:
            return

    def _render_files(self, upload: UsenetUpload) -> None:
        self._upload_result = upload
        self.files.delete(*self.files.get_children())
        for index, file in enumerate(upload.files):
            available = (
                is_valid_finder_link(file.download_url)
                and file.unreachable is None
            )
            self.files.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    file.name,
                    self._format_size(file.size),
                    "Available" if available else file.unreachable or "No download link",
                ),
                tags=() if available else ("unavailable",),
            )
        title = upload.title or "NZB upload"
        self.status.set(f"{title}: {len(upload.files)} file(s) processed")
        available_ids = [
            str(index)
            for index, file in enumerate(upload.files)
            if is_valid_finder_link(file.download_url)
            and file.unreachable is None
        ]
        if available_ids:
            self.add_all_button.configure(state="normal")
            self.add_selected_button.configure(state="normal")
            self.files.selection_set(available_ids)
        else:
            self.add_all_button.configure(state="disabled")
            self.add_selected_button.configure(state="disabled")
            self.status.set(f"{title}: no downloadable files were returned")

    def _add_selected(self) -> None:
        selected = self.files.selection()
        self._add_files(selected)

    def _add_all(self) -> None:
        self._add_files(self.files.get_children())

    def _add_files(self, selected: tuple[str, ...]) -> None:
        upload = self._upload_result
        if upload is None:
            return
        entries: list[tuple[str, str, int | None]] = []
        for item_id in selected:
            values = self.files.item(item_id, "values")
            if not values:
                continue
            index = int(item_id)
            file = upload.files[index]
            if is_valid_finder_link(file.download_url) and file.unreachable is None:
                entries.append((file.download_url, file.name, file.size))
        if not entries:
            self.status.set("Select at least one available file.")
            return
        added, duplicates, invalid = self.app.add_usenet_links(
            entries,
            source_label="NZB upload",
            source=QueueSource.NZB_UPLOAD,
        )
        self.status.set(
            f"Added {added}; skipped {duplicates} duplicate(s) "
            f"and {invalid} invalid file(s)"
        )

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.url_entry.configure(state=state)
        self.url_button.configure(state=state)
        self.file_button.configure(state=state)
        if busy:
            self.add_selected_button.configure(state="disabled")
            self.add_all_button.configure(state="disabled")

    @staticmethod
    def _format_size(size: int | None) -> str:
        if size is None:
            return "-"
        value = float(size)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return "-"
