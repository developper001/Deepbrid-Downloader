from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

__version__ = "0.2.0"
GITHUB_REPOSITORY_URL = "https://github.com/developper001/Deepbrid-Downloader"
GITHUB_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/developper001/Deepbrid-Downloader/releases/latest"
)


class UpdateCheckError(Exception):
    pass


def _version_parts(version: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:[-+][0-9A-Za-z.-]+)?", version.strip())
    if not match:
        return None
    parts = tuple(int(part) for part in match.group(1).split("."))
    return parts + (0,) * max(0, 3 - len(parts))


def is_newer_version(latest_tag: str, current_version: str = __version__) -> bool:
    latest_parts = _version_parts(latest_tag)
    current_parts = _version_parts(current_version)
    return latest_parts is not None and current_parts is not None and latest_parts > current_parts


def fetch_latest_release() -> tuple[str, str]:
    request = urllib.request.Request(
        GITHUB_LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "DeepbridDownloader",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as error:
        error.close()
        raise UpdateCheckError(f"GitHub returned HTTP {error.code}.") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise UpdateCheckError(f"Could not check GitHub releases: {error}") from error

    if not isinstance(payload, dict):
        raise UpdateCheckError("GitHub returned an unexpected release response.")
    tag = payload.get("tag_name")
    release_url = payload.get("html_url")
    if not isinstance(tag, str) or not isinstance(release_url, str):
        raise UpdateCheckError("GitHub release information is incomplete.")
    parsed_url = urllib.parse.urlparse(release_url)
    if parsed_url.netloc != "github.com" or not parsed_url.path.startswith(
        "/developper001/Deepbrid-Downloader/releases/"
    ):
        raise UpdateCheckError("GitHub returned an invalid release link.")
    return tag, release_url