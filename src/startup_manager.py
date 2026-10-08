from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WINDOWS_RUN_VALUE = "DeepbridDownloader"
MACOS_LOGIN_ITEMS_URL = "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"


class StartupConfigurationError(RuntimeError):
    pass


class StartupManager:
    def __init__(
        self,
        *,
        platform_name: str = sys.platform,
        home: Path | None = None,
        executable: Path | None = None,
        project_root: Path | None = None,
        frozen: bool | None = None,
    ):
        self.platform_name = platform_name
        self.home = (home or Path.home()).expanduser()
        self.executable = (executable or Path(sys.executable)).resolve()
        self.project_root = (project_root or Path(__file__).resolve().parent.parent).resolve()
        self.frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen

    @staticmethod
    def _desktop_exec_argument(argument: str) -> str:
        escaped = (
            argument.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("`", "\\`")
            .replace("$", "\\$")
            .replace("%", "%%")
        )
        return f'"{escaped}"'

    @property
    def supports_toggle(self) -> bool:
        return self.platform_name == "win32" or self.platform_name.startswith("linux")

    @property
    def startup_file(self) -> Path:
        return self.home / ".config" / "autostart" / "DeepbridDownloader.desktop"

    def is_enabled(self) -> bool:
        if self.platform_name == "win32":
            try:
                import winreg
            except ImportError:
                return False
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
                    winreg.QueryValueEx(key, WINDOWS_RUN_VALUE)
                return True
            except FileNotFoundError:
                return False
        if self.platform_name.startswith("linux"):
            return self.startup_file.is_file()
        return False

    def configure(self) -> str:
        if self.platform_name == "win32":
            return self._toggle_windows()
        if self.platform_name.startswith("linux"):
            return self._toggle_linux()
        if self.platform_name == "darwin":
            self._open_macos_login_items()
            return (
                "Opened Login Items. Add Deepbrid Downloader using the + button "
                "to start it after login."
            )
        raise StartupConfigurationError(
            f"Automatic startup configuration is not supported on {self.platform_name}."
        )

    def _toggle_windows(self) -> str:
        try:
            import winreg
        except ImportError as error:
            raise StartupConfigurationError(
                "Windows startup settings are unavailable in this Python installation."
            ) from error
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, WINDOWS_RUN_KEY) as key:
                if self.is_enabled():
                    winreg.DeleteValue(key, WINDOWS_RUN_VALUE)
                    return "Automatic startup disabled."
                winreg.SetValueEx(
                    key,
                    WINDOWS_RUN_VALUE,
                    0,
                    winreg.REG_SZ,
                    self._windows_command(),
                )
                return "Deepbrid Downloader will start after you sign in to Windows."
        except OSError as error:
            raise StartupConfigurationError(
                f"Could not update Windows startup settings: {error}"
            ) from error

    def _windows_command(self) -> str:
        if self.frozen:
            return subprocess.list2cmdline([str(self.executable)])
        pythonw = self.executable.with_name("pythonw.exe")
        python = pythonw if pythonw.is_file() else self.executable
        launcher = self.project_root / "launcher.py"
        return subprocess.list2cmdline([str(python), str(launcher)])

    def _toggle_linux(self) -> str:
        if self.is_enabled():
            try:
                self.startup_file.unlink()
            except OSError as error:
                raise StartupConfigurationError(
                    f"Could not remove the startup entry: {error}"
                ) from error
            return "Automatic startup disabled."
        executable = [str(self.executable)] if self.frozen else [
            str(self.executable),
            "-m",
            "src.app",
        ]
        command = " ".join(self._desktop_exec_argument(argument) for argument in executable)
        desktop_entry = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Deepbrid Downloader\n"
            "Comment=Start Deepbrid Downloader after login\n"
            f"Exec={command}\n"
            f"Path={self.project_root}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
        try:
            self.startup_file.parent.mkdir(parents=True, exist_ok=True)
            temporary_file = self.startup_file.with_suffix(".desktop.tmp")
            temporary_file.write_text(desktop_entry, encoding="utf-8")
            os.replace(temporary_file, self.startup_file)
        except OSError as error:
            raise StartupConfigurationError(
                f"Could not create the startup entry: {error}"
            ) from error
        return "Deepbrid Downloader will start after you sign in."

    @staticmethod
    def _open_macos_login_items() -> None:
        try:
            subprocess.Popen(["open", MACOS_LOGIN_ITEMS_URL])
        except OSError as error:
            raise StartupConfigurationError(
                f"Could not open macOS Login Items settings: {error}"
            ) from error
