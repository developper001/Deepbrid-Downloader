from __future__ import annotations

import json
import platform
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

__version__ = "0.4.0"
GITHUB_REPOSITORY_URL = "https://github.com/developper001/Deepbrid-Downloader"
GITHUB_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/developper001/Deepbrid-Downloader/releases/latest"
)


class UpdateCheckError(Exception):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    download_url: str
    size: int
    sha256: str | None


@dataclass(frozen=True)
class LatestRelease:
    tag: str
    release_url: str
    assets: tuple[ReleaseAsset, ...]


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


def fetch_latest_release_details() -> LatestRelease:
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
    raw_assets = payload.get("assets", [])
    if not isinstance(raw_assets, list):
        raise UpdateCheckError("GitHub returned invalid release assets.")
    assets: list[ReleaseAsset] = []
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        download_url = item.get("browser_download_url")
        size = item.get("size")
        digest = item.get("digest")
        if (
            not isinstance(name, str)
            or not name
            or "/" in name
            or "\\" in name
            or not isinstance(download_url, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
        ):
            continue
        asset_url = urllib.parse.urlparse(download_url)
        if asset_url.scheme != "https" or asset_url.netloc != "github.com":
            continue
        checksum = None
        if isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            checksum = digest.partition(":")[2].lower()
        assets.append(ReleaseAsset(name, download_url, size, checksum))
    return LatestRelease(tag, release_url, tuple(assets))


def fetch_latest_release() -> tuple[str, str]:
    release = fetch_latest_release_details()
    return release.tag, release.release_url


def platform_release_asset(
    release: LatestRelease,
    system: str | None = None,
) -> ReleaseAsset | None:
    system = system or platform.system()
    platform_name = {"Windows": "Windows", "Linux": "Linux", "Darwin": "macOS"}.get(system)
    if platform_name is None:
        return None
    extension = ".exe" if system == "Windows" else ""
    asset_by_name = {asset.name: asset for asset in release.assets}
    stable_name = f"DeepbridDownloader-{platform_name}{extension}"
    if stable_name in asset_by_name:
        return asset_by_name[stable_name]
    version = release.tag.removeprefix("v")
    legacy_name = f"DeepbridDownloader-{platform_name}-{version}{extension}"
    return asset_by_name.get(legacy_name)