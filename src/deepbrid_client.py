from __future__ import annotations

import json
import os
import re
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable


API_BASE = "https://www.deepbrid.com/api/v1"
APP_USER_AGENT = "DeepBridDownloader/0.2"


class DeepbridError(Exception):
    def __init__(self, message: str, *, retryable: bool = True, status_code: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class DeepbridClient:
    def __init__(self, api_key: str, log: Callable[[str], None] | None = None):
        self.api_key = api_key
        self.log = log or (lambda _message: None)

    def validate_api_key(self) -> None:
        request = urllib.request.Request(
            f"{API_BASE}/user",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
                "User-Agent": APP_USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 401:
                raise DeepbridError(
                    "Deepbrid rejected the API key (HTTP 401). Check the key and try again.",
                    retryable=False,
                    status_code=401,
                ) from error
            raise DeepbridError(
                f"Could not validate the API key (HTTP {error.code}).",
                retryable=False,
                status_code=error.code,
            ) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise DeepbridError(f"Could not validate the API key: {error}") from error
        if not isinstance(result, dict):
            raise DeepbridError("Deepbrid returned an unexpected API-key validation response.")
        if result.get("error") in (401, "401"):
            raise DeepbridError(
                "Deepbrid rejected the API key (HTTP 401). Check the key and try again.",
                retryable=False,
                status_code=401,
            )
        if result.get("error", 0) not in (0, "0"):
            raise DeepbridError(
                f"Deepbrid could not validate the API key: {result.get('message', 'unknown API error')}"
            )

    def generate_link(self, original_url: str) -> tuple[str, str | None]:
        payload = urllib.parse.urlencode({"link": original_url}).encode("utf-8")
        curl_command = " ".join(
            (
                "curl -X POST",
                shlex.quote(f"{API_BASE}/generate/link"),
                "-H", shlex.quote("Authorization: Bearer <REDACTED>"),
                "-H", shlex.quote("Content-Type: application/x-www-form-urlencoded"),
                "-H", shlex.quote("Accept: application/json"),
                "-H", shlex.quote(f"User-Agent: {APP_USER_AGENT}"),
                "--data", shlex.quote(payload.decode("utf-8")),
            )
        )
        request = urllib.request.Request(
            f"{API_BASE}/generate/link",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "User-Agent": APP_USER_AGENT,
            },
            method="POST",
        )
        try:
            self.log(f"Generating premium link for: {original_url}")
            self.log(f"Request: {curl_command}")
            with urllib.request.urlopen(request, timeout=45) as response:
                response_status = response.status
                response_body = response.read().decode("utf-8", errors="replace")
                try:
                    result = json.loads(response_body)
                except json.JSONDecodeError as error:
                    logged_body = response_body.replace(self.api_key, "<REDACTED>")
                    details = f"HTTP {response_status}; response body: {logged_body}"
                    self.log(details)
                    raise DeepbridError(
                        f"Deepbrid returned invalid JSON\n{details}\nRequest: {curl_command}"
                    ) from error
                logged_body = response_body.replace(self.api_key, "<REDACTED>")
                if isinstance(result, dict) and isinstance(result.get("link"), str):
                    logged_body = logged_body.replace(result["link"], "<DOWNLOAD_LINK_REDACTED>")
                self.log(f"HTTP {response.status}; response body: {logged_body}")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace").replace(self.api_key, "<REDACTED>")
            error.close()
            self.log(f"HTTP {error.code}; response body: {detail}")
            try:
                error_payload = json.loads(detail)
            except json.JSONDecodeError:
                error_payload = {}
            if not isinstance(error_payload, dict):
                error_payload = {}
            is_owner_block = (
                error_payload.get("error_code") == 1010
                or error_payload.get("error_name") == "browser_signature_banned"
                or error_payload.get("retryable") is False
                or error_payload.get("owner_action_required") is True
            )
            retryable = not (error.code == 401 or (error.code == 403 and is_owner_block))
            raise DeepbridError(
                f"Deepbrid returned HTTP {error.code}: {detail}\nRequest: {curl_command}",
                retryable=retryable,
                status_code=error.code,
            ) from error
        except (urllib.error.URLError, TimeoutError, UnicodeDecodeError) as error:
            message = f"Could not generate a premium link: {error}"
            self.log(f"Request failed before a valid response: {error}\nRequest: {curl_command}")
            raise DeepbridError(f"{message}\nRequest: {curl_command}") from error

        if not isinstance(result, dict):
            raise DeepbridError(f"Deepbrid returned an unexpected response\nHTTP 200; response body: {logged_body}\nRequest: {curl_command}")
        if result.get("error", 0) != 0:
            message = str(result.get("message", "Link generation failed"))
            raise DeepbridError(f"{message}\nHTTP 200; response body: {logged_body}\nRequest: {curl_command}")
        generated_url = result.get("link")
        if not isinstance(generated_url, str) or not generated_url:
            raise DeepbridError(f"Deepbrid response did not include a download link\nHTTP 200; response body: {logged_body}\nRequest: {curl_command}")
        filename = result.get("filename")
        return generated_url, filename if isinstance(filename, str) else None

    def download(
        self,
        download_url: str,
        filename: str,
        output_dir: Path,
        should_stop: Callable[[], bool],
        on_progress: Callable[[int, int | None], None],
        overwrite_existing: bool = False,
    ) -> bool:
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / filename
        partial = output_dir / f".{filename}.part"
        if destination.is_file() and not overwrite_existing:
            existing_size = destination.stat().st_size
            on_progress(existing_size, existing_size)
            return True
        offset = partial.stat().st_size if partial.exists() else 0
        request = urllib.request.Request(download_url)
        request.add_header("User-Agent", APP_USER_AGENT)
        if offset:
            request.add_header("Range", f"bytes={offset}-")
            range_header = shlex.quote("Range: bytes={}".format(offset))
            curl_command = (
                f"curl -L -H {range_header} "
                "'<TEMPORARY_DOWNLOAD_URL_REDACTED>'"
            )
        else:
            curl_command = "curl -L '<TEMPORARY_DOWNLOAD_URL_REDACTED>'"

        try:
            response = urllib.request.urlopen(request, timeout=60)
        except urllib.error.HTTPError as error:
            if error.code != 416 or not offset:
                detail = error.read().decode("utf-8", errors="replace")
                error.close()
                detail = detail.replace(self.api_key, "<REDACTED>").replace(
                    download_url, "<TEMPORARY_DOWNLOAD_URL_REDACTED>"
                )
                raise DeepbridError(
                    f"Download returned HTTP {error.code}: {detail}\nRequest: {curl_command}"
                ) from error
            error.close()
            partial.unlink(missing_ok=True)
            offset = 0
            response = urllib.request.urlopen(
                urllib.request.Request(download_url, headers={"User-Agent": APP_USER_AGENT}),
                timeout=60,
            )

        with response:
            status = getattr(response, "status", response.getcode())
            append = offset > 0 and status == 206
            if offset and status != 206:
                offset = 0

            total = self._total_size(response.headers, offset)
            downloaded = offset
            mode = "ab" if append else "wb"
            last_progress_report = time.monotonic()
            with partial.open(mode) as output:
                while True:
                    if should_stop():
                        output.flush()
                        on_progress(downloaded, total)
                        return False
                    chunk = response.read(128 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    downloaded += len(chunk)
                    now = time.monotonic()
                    if now - last_progress_report >= 1:
                        on_progress(downloaded, total)
                        last_progress_report = now

        if total is not None and downloaded != total:
            raise DeepbridError(
                f"Download size mismatch: received {downloaded} of {total} bytes."
            )
        os.replace(partial, destination)
        on_progress(downloaded, total or downloaded)
        return True

    @staticmethod
    def fetch_hosts(api_key: str = "") -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": APP_USER_AGENT}
        if api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        request = urllib.request.Request(
            f"{API_BASE}/hosts",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                status_code = response.status
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            error.close()
            raise DeepbridError(
                f"Host API returned HTTP {error.code}; response body: {body[:600]}",
                status_code=error.code,
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise DeepbridError(f"Could not load Deepbrid host status: {error}") from error
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise DeepbridError(
                f"Host API returned invalid JSON (HTTP {status_code}); response body: {body[:600]}"
            ) from error

        hosts: dict[str, str] = {}
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, str):
                    hosts[item.lower()] = "supported"
                elif isinstance(item, dict):
                    hosts.update({str(domain).lower(): str(status) for domain, status in item.items()})
        elif isinstance(payload, dict):
            listed_hosts = payload.get("hosts")
            if isinstance(listed_hosts, list):
                for item in listed_hosts:
                    if isinstance(item, str):
                        hosts[item.lower()] = "supported"
                    elif isinstance(item, dict):
                        hosts.update({str(domain).lower(): str(status) for domain, status in item.items()})
            else:
                hosts = {str(domain).lower(): str(status) for domain, status in payload.items()}
        if not hosts:
            raise DeepbridError(
                f"Host API returned no hosts (HTTP {status_code}); response body: {body[:600]}"
            )
        return hosts

    @staticmethod
    def fetch_host_limits(api_key: str) -> dict[str, str]:
        request = urllib.request.Request(
            f"{API_BASE}/user/limits",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": APP_USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as error:
            error.close()
            raise DeepbridError(f"Host limits API returned HTTP {error.code}.", status_code=error.code) from error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            raise DeepbridError(f"Could not load daily host limits: {error}") from error
        if not isinstance(payload, dict):
            raise DeepbridError("Deepbrid returned an unexpected host-limits response.")
        if payload.get("error", 0) not in (0, "0"):
            error_code = payload.get("error")
            status_code = int(error_code) if str(error_code).isdigit() else None
            raise DeepbridError(
                str(payload.get("message", "Could not load daily host limits.")),
                status_code=status_code,
            )

        limits: dict[str, str] = {}
        for entry in payload.get("hosters", []):
            host_name = entry.get("hoster") or entry.get("domain") if isinstance(entry, dict) else None
            if not isinstance(entry, dict) or not host_name:
                continue
            aliases = [alias.strip() for alias in re.split(r"[,;]", str(host_name).lower()) if alias.strip()]
            if entry.get("type") == "links":
                remaining = entry.get("remaining", "?")
                limit = entry.get("limit", "?")
                formatted_limit = f"{remaining} / {limit} links"
            else:
                remaining = entry.get("remaining_str")
                if not remaining:
                    try:
                        value = float(entry.get("remaining", 0))
                        for unit in ("B", "KB", "MB", "GB", "TB"):
                            if value < 1024 or unit == "TB":
                                remaining = f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
                                break
                            value /= 1024
                    except (TypeError, ValueError):
                        remaining = "Unknown"
                formatted_limit = f"{remaining} remaining"
            for alias in aliases:
                limits[alias] = formatted_limit
        return limits

    @staticmethod
    def _total_size(headers: object, offset: int) -> int | None:
        content_range = headers.get("Content-Range", "")
        match = re.search(r"/([0-9]+)$", content_range)
        if match:
            return int(match.group(1))
        content_length = headers.get("Content-Length")
        if content_length and content_length.isdigit():
            return offset + int(content_length)
        return None


def safe_filename(filename: str | None, original_url: str, item_id: int) -> str:
    name = filename or Path(urllib.parse.unquote(urllib.parse.urlparse(original_url).path)).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    if not name:
        name = f"download-{item_id}"
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update(f"COM{i}" for i in range(1, 10))
    reserved.update(f"LPT{i}" for i in range(1, 10))
    if name.upper().split(".")[0] in reserved:
        name = f"_{name}"
    return name