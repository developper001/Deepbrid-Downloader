from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path

from .app_info import ReleaseAsset


class UpdateInstallError(Exception):
    pass


def download_update_asset(
    asset: ReleaseAsset,
    directory: Path,
    on_progress: Callable[[int, int], None],
) -> Path:
    if not asset.sha256:
        raise UpdateInstallError("The release asset does not have a SHA-256 digest.")
    directory = directory.resolve()
    staged_path = directory / f".deepbrid-update-{uuid.uuid4().hex}.download"
    request = urllib.request.Request(
        asset.download_url,
        headers={"Accept": "application/octet-stream", "User-Agent": "DeepbridDownloader"},
    )
    digest = hashlib.sha256()
    downloaded = 0
    total = asset.size
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            if total <= 0:
                try:
                    total = int(response.headers.get("Content-Length", "0"))
                except ValueError:
                    total = 0
            on_progress(0, total)
            with staged_path.open("wb") as output:
                while chunk := response.read(64 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    downloaded += len(chunk)
                    on_progress(downloaded, total)
        if asset.size and downloaded != asset.size:
            raise UpdateInstallError(
                f"The update download was incomplete ({downloaded} of {asset.size} bytes)."
            )
        if digest.hexdigest() != asset.sha256:
            raise UpdateInstallError("The downloaded update failed its SHA-256 check.")
        if os.name != "nt":
            staged_path.chmod(0o755)
        return staged_path
    except Exception as error:
        staged_path.unlink(missing_ok=True)
        if isinstance(error, UpdateInstallError):
            raise
        raise UpdateInstallError(f"Could not download the update: {error}") from error


def launch_update_helper(staged_path: Path) -> tuple[Path, Path]:
    if not getattr(sys, "frozen", False):
        raise UpdateInstallError("Automatic installation is available only in packaged builds.")
    target_path = Path(sys.executable).resolve()
    staged_path = staged_path.resolve()
    if staged_path.parent != target_path.parent or not staged_path.name.startswith(
        ".deepbrid-update-"
    ):
        raise UpdateInstallError("The staged update is not beside the application executable.")
    helper_path = target_path.parent / (
        f".deepbrid-update-helper-{uuid.uuid4().hex}{target_path.suffix}"
    )
    try:
        shutil.copy2(target_path, helper_path)
        command = [
            str(helper_path),
            "--apply-update",
            str(staged_path),
            str(target_path),
            str(os.getpid()),
            str(helper_path),
        ]
        creation_flags = 0
        if os.name == "nt":
            creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        subprocess.Popen(
            command,
            cwd=target_path.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
            start_new_session=os.name != "nt",
        )
    except OSError as error:
        helper_path.unlink(missing_ok=True)
        raise UpdateInstallError(f"Could not start the update installer: {error}") from error
    return target_path, helper_path


def _wait_for_process(process_id: int, timeout: float = 120) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        synchronize = 0x00100000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(synchronize, False, process_id)
        if not handle:
            if ctypes.get_last_error() == 87:
                return
            raise UpdateInstallError("Could not wait for the current application to close.")
        try:
            result = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            if result == 0x00000102:
                raise UpdateInstallError("Timed out waiting for the application to close.")
            if result != 0:
                raise UpdateInstallError("Could not wait for the current application to close.")
        finally:
            kernel32.CloseHandle(handle)
        return

    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        if time.monotonic() >= deadline:
            raise UpdateInstallError("Timed out waiting for the application to close.")
        time.sleep(0.2)


def apply_update_from_args(arguments: list[str]) -> int:
    if len(arguments) != 5:
        raise UpdateInstallError("Invalid update-installer arguments.")
    _, staged_text, target_text, process_id_text, helper_text = arguments
    staged_path = Path(staged_text).resolve()
    target_path = Path(target_text).resolve()
    helper_path = Path(helper_text).resolve()
    running_path = Path(sys.executable).resolve()
    if (
        running_path != helper_path
        or target_path.parent != helper_path.parent
        or staged_path.parent != target_path.parent
        or not staged_path.name.startswith(".deepbrid-update-")
        or not helper_path.name.startswith(".deepbrid-update-helper-")
        or not target_path.is_file()
        or not staged_path.is_file()
    ):
        raise UpdateInstallError("The staged update paths are invalid.")
    try:
        process_id = int(process_id_text)
    except ValueError as error:
        raise UpdateInstallError("Invalid application process identifier.") from error
    if process_id <= 0:
        raise UpdateInstallError("Invalid application process identifier.")

    _wait_for_process(process_id)
    backup_path = target_path.parent / f".deepbrid-old-{uuid.uuid4().hex}"
    original_mode = stat.S_IMODE(target_path.stat().st_mode)
    shutil.copy2(target_path, backup_path)
    try:
        os.replace(staged_path, target_path)
        if os.name != "nt":
            target_path.chmod(original_mode | 0o111)
        command = [str(target_path), "--cleanup-update-helper", str(helper_path)]
        subprocess.Popen(
            command,
            cwd=target_path.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=os.name != "nt",
            creationflags=(
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
        )
    except Exception as error:
        os.replace(backup_path, target_path)
        try:
            subprocess.Popen([str(target_path)], cwd=target_path.parent)
        except OSError:
            pass
        raise UpdateInstallError(f"Could not install the update: {error}") from error
    backup_path.unlink(missing_ok=True)
    if os.name != "nt":
        helper_path.unlink(missing_ok=True)
    return 0


def cleanup_update_helper_later(helper_path: str) -> None:
    path = Path(helper_path)
    if (
        not getattr(sys, "frozen", False)
        or not path.name.startswith(".deepbrid-update-helper-")
        or path.parent.resolve() != Path(sys.executable).resolve().parent
    ):
        return

    def remove_when_available() -> None:
        for _ in range(100):
            try:
                path.unlink(missing_ok=True)
                return
            except OSError:
                time.sleep(0.2)

    threading.Thread(target=remove_when_available, daemon=True).start()
