from __future__ import annotations

import json
import os
import socket
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable, Mapping

import websocket


LOGIN_URL = "https://www.deepbrid.com/login"
FINDER_URL = "https://www.deepbrid.com/usenet-finder"
DEBUGGER_TIMEOUT = 20
DEBUG_PORT_FILE = "DeepbridDevToolsPort"


class UsenetFinderError(Exception):
    pass


class UsenetBrowserSession:
    def __init__(
        self,
        profile_dir: Path,
        log: Callable[[str], None] | None = None,
        browser_path: str | None = None,
    ):
        self.profile_dir = profile_dir
        self.log = log or (lambda _message: None)
        self.browser_path = browser_path
        self._process: subprocess.Popen[bytes] | None = None

    def open(self) -> None:
        if self._debug_port() is not None:
            self.log("Reusing the dedicated Deepbrid Chrome profile.")
            return
        if self._close_legacy_automated_browser():
            self.log("Closed the previous automated browser so the profile can restart in manual mode.")
        if self._process is not None and self._process.poll() is None:
            self.log("The dedicated browser is starting.")
            return
        browser = self.browser_path or self._find_browser()
        if browser is None:
            if sys.platform == "darwin":
                install_help = (
                    "Install Google Chrome or Microsoft Edge for macOS, then reopen Finder.\n\n"
                    "Chrome: https://www.google.com/chrome/\n"
                    "Edge: https://www.microsoft.com/edge/download"
                )
            elif sys.platform.startswith("linux"):
                install_help = (
                    "Install Google Chrome or Microsoft Edge for Linux, then reopen Finder. "
                    "On Ubuntu or Debian, download and install the .deb package from either "
                    "browser's website. Other Linux distributions can use the package offered "
                    "for that distribution.\n\n"
                    "Chrome: https://www.google.com/chrome/\n"
                    "Edge: https://www.microsoft.com/edge/download"
                )
            else:
                install_help = (
                    "Install Google Chrome or Microsoft Edge, then reopen Finder.\n\n"
                    "Chrome: https://www.google.com/chrome/\n"
                    "Edge: https://www.microsoft.com/edge/download"
                )
            raise UsenetFinderError(
                "Usenet Finder requires Google Chrome or Microsoft Edge, but neither browser "
                "was found.\n\n"
                f"{install_help}\n\n"
                "If the browser is already installed, set DEEPBRID_CHROME_PATH to its executable."
            )
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        port = self._find_free_port()
        port_file = self.profile_dir / DEBUG_PORT_FILE
        port_file.write_text(str(port), encoding="ascii")
        arguments = [
            browser,
            f"--user-data-dir={self.profile_dir}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            LOGIN_URL,
        ]
        options: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._process = subprocess.Popen(arguments, **options)
        except OSError as error:
            port_file.unlink(missing_ok=True)
            raise UsenetFinderError(f"Could not start the dedicated browser: {error}") from error
        self.log(
            "Opened dedicated Chrome profile for manual Cloudflare verification and sign-in "
            "with loopback debugging."
        )

    def request_json(self, parameters: Mapping[str, str]) -> dict[str, object]:
        action = parameters.get("do", "request")
        self.log(f"Running Finder {action} request inside the dedicated Chrome session.")
        port = self._wait_for_debug_port()
        deadline = time.monotonic() + 5
        target = self._find_finder_target(port)
        while target is None and time.monotonic() < deadline:
            time.sleep(0.1)
            target = self._find_finder_target(port)
        if target is None:
            raise UsenetFinderError(
                "The dedicated Chrome window is not on Deepbrid. Open Usenet Finder there, "
                "complete any Cloudflare check, and sign in before searching."
            )
        query = urllib.parse.urlencode({"ajax": "1", **parameters})
        endpoint = f"{FINDER_URL}?{query}"
        expression = (
            "(async()=>{"
            "if(location.origin!=='https://www.deepbrid.com')"
            "throw new Error('The active Chrome tab is not on Deepbrid.');"
            f"const response=await fetch({json.dumps(endpoint)},"
            "{credentials:'same-origin',headers:{accept:'application/json'}});"
            "return {status:response.status,contentType:response.headers.get('content-type')||'',"
            "body:await response.text()};"
            "})()"
        )
        response = self._evaluate(target["webSocketDebuggerUrl"], expression)
        from .usenet_finder import parse_browser_response

        payload = parse_browser_response(
            response["status"],
            response["contentType"],
            response["body"],
        )
        count = len(payload.get("results", [])) if isinstance(payload.get("results"), list) else None
        self.log(
            f"Finder {action} response: HTTP {response['status']}"
            + (f", {count} result(s)." if count is not None else ".")
        )
        return payload

    def _wait_for_debug_port(self) -> int:
        deadline = time.monotonic() + DEBUGGER_TIMEOUT
        port_file = self.profile_dir / DEBUG_PORT_FILE
        if not port_file.exists() and self._process is None:
            raise UsenetFinderError(
                "Dedicated Chrome is not open. Click 'Open Chrome / sign in' first."
            )
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise UsenetFinderError(
                    "The dedicated Chrome process closed unexpectedly. Open it again and retry."
                )
            port = self._debug_port()
            if port is not None:
                return port
            time.sleep(0.1)
        raise UsenetFinderError("Timed out waiting for the dedicated Chrome browser connection.")

    def _debug_port(self) -> int | None:
        try:
            port = int((self.profile_dir / DEBUG_PORT_FILE).read_text(encoding="ascii").strip())
        except (OSError, ValueError, IndexError):
            return None
        if not 1 <= port <= 65535:
            return None
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version",
                timeout=1,
            ) as response:
                version = json.loads(response.read().decode("utf-8"))
            if not isinstance(version, dict):
                return None
            browser = version.get("Browser", "")
            if isinstance(browser, str) and browser.startswith(("Chrome/", "Edg/")):
                return port
            return None
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None

    def _close_legacy_automated_browser(self) -> bool:
        legacy_file = self.profile_dir / "DevToolsActivePort"
        try:
            port = int(legacy_file.read_text(encoding="ascii").splitlines()[0])
        except (OSError, ValueError, IndexError):
            return False
        if not 1 <= port <= 65535:
            return False
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version",
                timeout=1,
            ) as response:
                version = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return False
        if not isinstance(version, dict):
            return False
        browser = version.get("Browser", "")
        if not isinstance(browser, str) or not browser.startswith(("Chrome/", "Edg/")):
            return False
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list",
                timeout=3,
            ) as response:
                targets = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            return False
        if not isinstance(targets, list):
            return False
        if not any(
            isinstance(target, dict)
            and target.get("type") == "page"
            and str(target.get("url", "")).startswith(FINDER_URL)
            for target in targets
        ):
            return False
        self._close_browser(port)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/version",
                    timeout=0.3,
                ):
                    time.sleep(0.1)
            except (urllib.error.URLError, TimeoutError, OSError):
                legacy_file.unlink(missing_ok=True)
                return True
            time.sleep(0.1)
        raise UsenetFinderError(
            "The previous automated Deepbrid Chrome window is still open. Close it and "
            "reopen the Finder browser so Cloudflare can verify a normal browser session."
        )

    @staticmethod
    def _find_free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return listener.getsockname()[1]

    @staticmethod
    def _close_browser(port: int) -> None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version",
                timeout=1,
            ) as response:
                version = json.loads(response.read().decode("utf-8"))
            if not isinstance(version, dict):
                return
            browser_socket = version.get("webSocketDebuggerUrl")
            if not isinstance(browser_socket, str):
                return
            connection = websocket.create_connection(
                browser_socket,
                timeout=3,
                suppress_origin=True,
            )
            try:
                connection.send(json.dumps({"id": 1, "method": "Browser.close"}))
            finally:
                connection.close()
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
            websocket.WebSocketException,
        ):
            return

    @staticmethod
    def _find_finder_target(port: int) -> dict[str, object] | None:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list",
                timeout=3,
            ) as response:
                targets = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            raise UsenetFinderError(f"Could not inspect the dedicated Chrome tabs: {error}") from error
        for target in targets:
            if (
                isinstance(target, dict)
                and target.get("type") == "page"
                and str(target.get("url", "")).startswith("https://www.deepbrid.com/")
                and isinstance(target.get("webSocketDebuggerUrl"), str)
            ):
                return target
        return None

    @staticmethod
    def _evaluate(websocket_url: str, expression: str) -> dict[str, object]:
        parsed = urllib.parse.urlparse(websocket_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise UsenetFinderError("Refusing a non-local Chrome DevTools connection.")
        try:
            connection = websocket.create_connection(
                websocket_url,
                timeout=DEBUGGER_TIMEOUT,
                suppress_origin=True,
            )
        except (OSError, websocket.WebSocketException) as error:
            raise UsenetFinderError(
                f"Could not connect to the dedicated Chrome page: {error}"
            ) from error
        try:
            try:
                connection.send(
                    json.dumps(
                        {
                            "id": 1,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": expression,
                                "awaitPromise": True,
                                "returnByValue": True,
                            },
                        }
                    )
                )
            except websocket.WebSocketException as error:
                raise UsenetFinderError(
                    f"Could not send the Finder request to Chrome: {error}"
                ) from error
            deadline = time.monotonic() + DEBUGGER_TIMEOUT
            while time.monotonic() < deadline:
                try:
                    message = json.loads(connection.recv())
                except (websocket.WebSocketException, json.JSONDecodeError) as error:
                    raise UsenetFinderError(f"Chrome Finder request failed: {error}") from error
                if not isinstance(message, dict) or message.get("id") != 1:
                    continue
                result = message.get("result", {}).get("result", {})
                if "exceptionDetails" in message.get("result", {}):
                    details = message["result"]["exceptionDetails"]
                    text = details.get("text", "Finder request failed in Chrome.")
                    raise UsenetFinderError(str(text))
                value = result.get("value")
                if not isinstance(value, dict):
                    raise UsenetFinderError("Chrome returned an unexpected Finder response.")
                status = value.get("status")
                content_type = value.get("contentType")
                body = value.get("body")
                if not isinstance(status, int) or not isinstance(content_type, str) or not isinstance(body, str):
                    raise UsenetFinderError("Chrome returned a malformed Finder response.")
                return {"status": status, "contentType": content_type, "body": body}
            raise UsenetFinderError("Timed out waiting for the Finder response in Chrome.")
        finally:
            try:
                connection.close()
            except websocket.WebSocketException:
                pass

    @staticmethod
    def _find_browser() -> str | None:
        configured = os.environ.get("DEEPBRID_CHROME_PATH")
        if configured and Path(configured).is_file():
            return configured
        names = (
            ("chrome", "google-chrome", "chromium", "microsoft-edge")
            if sys.platform != "win32"
            else ("chrome.exe", "msedge.exe")
        )
        for name in names:
            found = shutil.which(name)
            if found:
                return found
        if sys.platform == "win32":
            roots = (
                Path(os.environ.get("PROGRAMFILES", "C:/Program Files")),
                Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")),
                Path(os.environ.get("LOCALAPPDATA", "")),
            )
            relative_paths = (
                Path("Google/Chrome/Application/chrome.exe"),
                Path("Microsoft/Edge/Application/msedge.exe"),
                Path("Programs/Google/Chrome/Application/chrome.exe"),
            )
            for root in roots:
                for relative_path in relative_paths:
                    candidate = root / relative_path
                    if candidate.is_file():
                        return str(candidate)
        elif sys.platform == "darwin":
            for candidate in (
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            ):
                if candidate.is_file():
                    return str(candidate)
        return None
