from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from .usenet_browser import UsenetBrowserSession, UsenetFinderError


FILE_SIZE_PATTERN = re.compile(
    r"\s*(\d+(?:\.\d+)?)\s*(B|bytes?|KB|KiB|MB|MiB|GB|GiB|TB|TiB)?\s*",
    re.IGNORECASE,
)


def file_size_bytes(size: str) -> int | None:
    match = FILE_SIZE_PATTERN.fullmatch(size)
    if match is None:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or "B").lower()
    if unit in {"b", "byte", "bytes"}:
        multiplier = 1
    else:
        power = "kmgt".index(unit[0]) + 1
        multiplier = (1024 if unit.endswith("ib") else 1000) ** power
    return round(value * multiplier)


@dataclass(frozen=True)
class FinderResult:
    title: str
    token: str
    category: str
    size: str
    size_bytes: int | None
    date: str
    metadata: Mapping[str, object]


@dataclass(frozen=True)
class FinderSearchPage:
    results: tuple[FinderResult, ...]
    has_more: bool


@dataclass(frozen=True)
class FinderFile:
    name: str
    link: str
    size: str
    is_video: bool
    inaccessible: str | None
    metadata: Mapping[str, object]

    @property
    def is_accessible(self) -> bool:
        return not self.inaccessible and is_valid_finder_link(self.link)


def is_valid_finder_link(link: str) -> bool:
    if not link or any(character.isspace() for character in link):
        return False
    try:
        parsed = urlsplit(link)
        _ = parsed.port
        return (
            parsed.scheme.lower() in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


@dataclass(frozen=True)
class FinderPackage:
    name: str
    files: tuple[FinderFile, ...]
    metadata: Mapping[str, object]


class UsenetFinderClient:
    """Usenet Finder requests executed within the authenticated Chrome page."""

    def __init__(self, browser: UsenetBrowserSession):
        self.browser = browser

    def search(
        self,
        query: str,
        category: str = "",
        offset: int = 0,
        limit: int = 15,
    ) -> FinderSearchPage:
        query = query.strip()
        if not query:
            raise ValueError("Search query cannot be empty.")
        if offset < 0:
            raise ValueError("Search offset cannot be negative.")
        if not 1 <= limit <= 100:
            raise ValueError("Search limit must be between 1 and 100.")

        payload = self.browser.request_json(
            {
                "do": "search",
                "q": query,
                "cat": category,
                "offset": str(offset),
                "limit": str(limit),
            }
        )
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise UsenetFinderError("Finder response does not contain a results list.")
        has_more = payload.get("hasMore")
        if not isinstance(has_more, bool):
            raise UsenetFinderError("Finder response does not contain a valid hasMore flag.")
        return FinderSearchPage(tuple(self._parse_result(item) for item in raw_results), has_more)

    def resolve(self, token: str) -> FinderPackage:
        token = token.strip()
        if not token:
            raise ValueError("Finder result token cannot be empty.")
        payload = self.browser.request_json({"do": "process", "token": token})
        raw_files = payload.get("files")
        if not isinstance(raw_files, list):
            raise UsenetFinderError("Finder resolve response does not contain a files list.")
        files = tuple(self._parse_file(item) for item in raw_files)
        package_name = payload.get("pkg", "")
        if not isinstance(package_name, str):
            package_name = ""
        return FinderPackage(package_name, files, payload)

    @staticmethod
    def _parse_result(value: object) -> FinderResult:
        if not isinstance(value, dict):
            raise UsenetFinderError("Finder returned a malformed search result.")
        title = value.get("title")
        token = value.get("token")
        if not isinstance(title, str) or not isinstance(token, str) or not token:
            raise UsenetFinderError("Finder result is missing its title or resolution token.")
        size_bytes = value.get("sizeBytes")
        if not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or size_bytes < 0:
            size_bytes = None
        return FinderResult(
            title=title,
            token=token,
            category=UsenetFinderClient._string_value(value.get("cat")),
            size=UsenetFinderClient._string_value(value.get("size")),
            size_bytes=size_bytes,
            date=UsenetFinderClient._string_value(value.get("date")),
            metadata=value,
        )

    @staticmethod
    def _parse_file(value: object) -> FinderFile:
        if not isinstance(value, dict):
            raise UsenetFinderError("Finder returned a malformed file entry.")
        name = value.get("name")
        link = value.get("link")
        if not isinstance(link, str) or not link:
            link = value.get("url", "")
        if not isinstance(name, str) or not isinstance(link, str):
            raise UsenetFinderError("Finder file entry is missing its name or link.")
        inaccessible = value.get("inaccessible")
        if not isinstance(inaccessible, str) or not inaccessible:
            inaccessible = None
        return FinderFile(
            name=name,
            link=link,
            size=UsenetFinderClient._string_value(value.get("size")),
            is_video=value.get("isVideo") is True,
            inaccessible=inaccessible,
            metadata=value,
        )

    @staticmethod
    def _string_value(value: object) -> str:
        return value if isinstance(value, str) else ""


def parse_browser_response(status_code: int, content_type: str, body: str) -> dict[str, object]:
    if status_code in {401, 403}:
        if "html" in content_type.lower():
            raise UsenetFinderError(
                "Deepbrid returned a challenge or sign-in page. Complete the Cloudflare check "
                "and sign in in the dedicated Chrome window, then retry."
            )
        raise UsenetFinderError(
            f"Finder returned HTTP {status_code}. Check sign-in and Cloudflare status in "
            "the dedicated Chrome window, then retry."
        )
    if status_code < 200 or status_code >= 300:
        raise UsenetFinderError(f"Finder returned HTTP {status_code}.")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        if "html" in content_type.lower() or body.lstrip().lower().startswith(("<!doctype html", "<html")):
            raise UsenetFinderError(
                "Finder returned a challenge or sign-in page instead of JSON. Complete the "
                "Cloudflare check and sign in in the dedicated Chrome window, then retry."
            ) from error
        raise UsenetFinderError("Finder returned invalid JSON.") from error
    if not isinstance(payload, dict):
        raise UsenetFinderError("Finder returned an unexpected response.")
    if payload.get("error"):
        message = payload["error"]
        if (
            isinstance(message, str)
            and " ".join(message.casefold().replace("_", " ").replace("-", " ").split())
            == "premium required"
        ):
            raise UsenetFinderError(
                "Sign in to your Deepbrid account in the Chrome window, then retry "
                "the Usenet Finder search."
            )
        raise UsenetFinderError(message if isinstance(message, str) else "Finder reported an error.")
    return payload
