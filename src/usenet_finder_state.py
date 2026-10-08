from __future__ import annotations

import json
import math
import time
from collections.abc import Callable

from .secure_store import SecureStore
from .usenet_finder import FinderFile, FinderPackage, FinderResult, FinderSearchPage


SEARCH_SETTING = "usenet_finder_last_search"
SEARCH_HISTORY_SETTING = "usenet_finder_search_history"
RESOLVED_CACHE_SETTING = "usenet_finder_resolved_cache"
SEARCH_CACHE_SETTING = "usenet_finder_search_cache"
RESOLVED_CACHE_PURPOSE = b"usenet-finder-resolved-cache-v1"
SEARCH_CACHE_PURPOSE = b"usenet-finder-search-cache-v1"
CACHE_DURATION_SETTING = "usenet_finder_cache_duration_hours"
DEFAULT_CACHE_DURATION_HOURS = 24
MIN_CACHE_DURATION_HOURS = 1
MAX_CACHE_DURATION_HOURS = 720
CACHE_TTL_SECONDS = DEFAULT_CACHE_DURATION_HOURS * 60 * 60
MAX_SEARCH_HISTORY = 10


class UsenetFinderStateError(ValueError):
    pass


def valid_cache_duration_hours(value: object) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError) as error:
        raise ValueError("Cache duration must be a whole number of hours.") from error
    if isinstance(value, bool) or str(hours) != str(value).strip():
        raise ValueError("Cache duration must be a whole number of hours.")
    if not MIN_CACHE_DURATION_HOURS <= hours <= MAX_CACHE_DURATION_HOURS:
        raise ValueError(
            f"Cache duration must be between {MIN_CACHE_DURATION_HOURS} and "
            f"{MAX_CACHE_DURATION_HOURS} hours."
        )
    return hours


class UsenetFinderState:
    def __init__(
        self,
        secure_store: SecureStore,
        clock: Callable[[], float] = time.time,
        cache_duration_hours: Callable[[], int] | int = DEFAULT_CACHE_DURATION_HOURS,
    ):
        self.secure_store = secure_store
        self.clock = clock
        self.cache_duration_hours = cache_duration_hours
        self._resolved_cache_loaded = False
        self._resolved_packages: dict[str, tuple[float, FinderPackage]] = {}
        self._search_cache_loaded = False
        self._search_pages: dict[str, tuple[float, FinderSearchPage]] = {}

    def cache_ttl_seconds(self) -> int:
        configured = (
            self.cache_duration_hours()
            if callable(self.cache_duration_hours)
            else self.cache_duration_hours
        )
        try:
            hours = valid_cache_duration_hours(configured)
        except ValueError as error:
            raise UsenetFinderStateError(str(error)) from error
        return hours * 60 * 60

    def cache_duration_hours_value(self) -> int:
        return self.cache_ttl_seconds() // (60 * 60)

    def load_search(self) -> tuple[str, str]:
        saved = self.secure_store.get_setting(SEARCH_SETTING)
        if saved is None:
            return "", ""
        try:
            value = json.loads(saved)
        except json.JSONDecodeError as error:
            raise UsenetFinderStateError(
                "Saved Usenet Finder search settings are malformed."
            ) from error
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("query"), str)
            or not isinstance(value.get("category"), str)
        ):
            raise UsenetFinderStateError("Saved Usenet Finder search settings are invalid.")
        return value["query"], value["category"]

    def save_search(self, query: str, category: str) -> None:
        query = query.strip()
        category = category.strip()
        self.secure_store.set_setting(
            SEARCH_SETTING,
            json.dumps({"query": query, "category": category}),
        )
        history = self.load_search_history()
        current = (query, category)
        history = [entry for entry in history if entry != current]
        self.secure_store.set_setting(
            SEARCH_HISTORY_SETTING,
            json.dumps([current, *history[: MAX_SEARCH_HISTORY - 1]]),
        )

    def load_search_history(self) -> list[tuple[str, str]]:
        saved = self.secure_store.get_setting(SEARCH_HISTORY_SETTING)
        if saved is None:
            return []
        try:
            value = json.loads(saved)
        except json.JSONDecodeError as error:
            raise UsenetFinderStateError("Saved Usenet Finder search history is malformed.") from error
        if not isinstance(value, list):
            raise UsenetFinderStateError("Saved Usenet Finder search history is invalid.")
        history = []
        for entry in value:
            if (
                isinstance(entry, list)
                and len(entry) == 2
                and all(isinstance(item, str) for item in entry)
                and entry[0].strip()
            ):
                history.append((entry[0], entry[1]))
        return history[:MAX_SEARCH_HISTORY]

    def get_search_page(
        self,
        query: str,
        category: str,
        offset: int,
        limit: int,
    ) -> FinderSearchPage | None:
        cached = self.get_search_page_with_expiry(query, category, offset, limit)
        return cached[0] if cached else None

    def get_search_page_with_expiry(
        self,
        query: str,
        category: str,
        offset: int,
        limit: int,
    ) -> tuple[FinderSearchPage, float] | None:
        self._load_search_cache()
        cache_key = self._search_cache_key(query, category, offset, limit)
        cached = self._search_pages.get(cache_key)
        if cached is None:
            return None
        cached_at, page = cached
        expires_at = cached_at + self.cache_ttl_seconds()
        if self.clock() >= expires_at:
            del self._search_pages[cache_key]
            self._save_search_cache()
            return None
        return page, expires_at

    def save_search_page(
        self,
        query: str,
        category: str,
        offset: int,
        limit: int,
        page: FinderSearchPage,
    ) -> None:
        self._load_search_cache()
        self._search_pages[self._search_cache_key(query, category, offset, limit)] = (
            self.clock(),
            page,
        )
        self._remove_expired_search_pages()
        self._save_search_cache()

    def get_resolved(self, token: str) -> FinderPackage | None:
        cached = self.get_resolved_with_expiry(token)
        return cached[0] if cached else None

    def get_resolved_with_expiry(
        self,
        token: str,
    ) -> tuple[FinderPackage, float] | None:
        self._load_resolved_cache()
        cached = self._resolved_packages.get(token)
        if cached is None:
            return None
        resolved_at, package = cached
        expires_at = resolved_at + self.cache_ttl_seconds()
        if self.clock() >= expires_at:
            del self._resolved_packages[token]
            self._save_resolved_cache()
            return None
        return package, expires_at

    def save_resolved(self, token: str, package: FinderPackage) -> None:
        self._load_resolved_cache()
        self._resolved_packages[token] = (self.clock(), package)
        self._remove_expired_resolved_packages()
        self._save_resolved_cache()

    def clear_cache(self) -> None:
        self.secure_store.delete_encrypted_setting(SEARCH_CACHE_SETTING)
        self.secure_store.delete_encrypted_setting(RESOLVED_CACHE_SETTING)
        self._search_pages.clear()
        self._resolved_packages.clear()
        self._search_cache_loaded = True
        self._resolved_cache_loaded = True

    @staticmethod
    def _search_cache_key(query: str, category: str, offset: int, limit: int) -> str:
        return json.dumps((query.strip(), category.strip(), offset, limit))

    def _load_resolved_cache(self) -> None:
        if self._resolved_cache_loaded:
            return
        saved = self.secure_store.get_encrypted_setting(
            RESOLVED_CACHE_SETTING,
            RESOLVED_CACHE_PURPOSE,
        )
        if saved is not None:
            try:
                raw_cache = json.loads(saved)
            except json.JSONDecodeError as error:
                raise UsenetFinderStateError(
                    "Saved Usenet Finder resolved-link cache is malformed."
                ) from error
            if not isinstance(raw_cache, dict):
                raise UsenetFinderStateError("Saved Usenet Finder resolved-link cache is invalid.")
            for token, entry in raw_cache.items():
                if not isinstance(token, str) or not isinstance(entry, dict):
                    raise UsenetFinderStateError(
                        "Saved Usenet Finder resolved-link cache contains an invalid entry."
                    )
                resolved_at = entry.get("cached_at", entry.get("resolved_at"))
                if (
                    not isinstance(resolved_at, (int, float))
                    or isinstance(resolved_at, bool)
                    or not math.isfinite(resolved_at)
                ):
                    raise UsenetFinderStateError(
                        "Saved Usenet Finder resolved-link cache has an invalid timestamp."
                    )
                if self.clock() - resolved_at < self.cache_ttl_seconds():
                    self._resolved_packages[token] = (
                        float(resolved_at),
                        self._package_from_json(entry.get("package")),
                    )
        self._resolved_cache_loaded = True
        if saved is not None and len(self._resolved_packages) != len(raw_cache):
            self._save_resolved_cache()

    def _load_search_cache(self) -> None:
        if self._search_cache_loaded:
            return
        saved = self.secure_store.get_encrypted_setting(
            SEARCH_CACHE_SETTING,
            SEARCH_CACHE_PURPOSE,
        )
        if saved is not None:
            try:
                raw_cache = json.loads(saved)
            except json.JSONDecodeError as error:
                raise UsenetFinderStateError(
                    "Saved Usenet Finder search cache is malformed."
                ) from error
            if not isinstance(raw_cache, dict):
                raise UsenetFinderStateError("Saved Usenet Finder search cache is invalid.")
            for cache_key, entry in raw_cache.items():
                if not isinstance(cache_key, str) or not isinstance(entry, dict):
                    raise UsenetFinderStateError(
                        "Saved Usenet Finder search cache contains an invalid entry."
                    )
                cached_at = entry.get("cached_at", entry.get("resolved_at"))
                if (
                    not isinstance(cached_at, (int, float))
                    or isinstance(cached_at, bool)
                    or not math.isfinite(cached_at)
                ):
                    raise UsenetFinderStateError(
                        "Saved Usenet Finder search cache has an invalid timestamp."
                    )
                if self.clock() - cached_at < self.cache_ttl_seconds():
                    self._search_pages[cache_key] = (
                        float(cached_at),
                        self._search_page_from_json(entry.get("page")),
                    )
        self._search_cache_loaded = True
        if saved is not None and len(self._search_pages) != len(raw_cache):
            self._save_search_cache()

    def _remove_expired_resolved_packages(self) -> None:
        now = self.clock()
        ttl_seconds = self.cache_ttl_seconds()
        self._resolved_packages = {
            token: cached for token, cached in self._resolved_packages.items()
            if now - cached[0] < ttl_seconds
        }

    def _remove_expired_search_pages(self) -> None:
        now = self.clock()
        ttl_seconds = self.cache_ttl_seconds()
        self._search_pages = {
            cache_key: cached for cache_key, cached in self._search_pages.items()
            if now - cached[0] < ttl_seconds
        }

    def _save_resolved_cache(self) -> None:
        value = {
            token: {
                "resolved_at": cached_at,
                "package": self._package_to_json(package),
            }
            for token, (cached_at, package) in self._resolved_packages.items()
        }
        self.secure_store.set_encrypted_setting(
            RESOLVED_CACHE_SETTING,
            json.dumps(value),
            RESOLVED_CACHE_PURPOSE,
        )

    def _save_search_cache(self) -> None:
        value = {
            cache_key: {
                "cached_at": cached_at,
                "page": self._search_page_to_json(page),
            }
            for cache_key, (cached_at, page) in self._search_pages.items()
        }
        self.secure_store.set_encrypted_setting(
            SEARCH_CACHE_SETTING,
            json.dumps(value),
            SEARCH_CACHE_PURPOSE,
        )

    @staticmethod
    def _search_page_to_json(page: FinderSearchPage) -> dict[str, object]:
        return {
            "has_more": page.has_more,
            "results": [
                {
                    "title": result.title,
                    "token": result.token,
                    "category": result.category,
                    "size": result.size,
                    "size_bytes": result.size_bytes,
                    "date": result.date,
                    "metadata": dict(result.metadata),
                }
                for result in page.results
            ],
        }

    @staticmethod
    def _search_page_from_json(value: object) -> FinderSearchPage:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("has_more"), bool)
            or not isinstance(value.get("results"), list)
        ):
            raise UsenetFinderStateError(
                "Saved Usenet Finder search cache contains an invalid page."
            )
        results: list[FinderResult] = []
        for item in value["results"]:
            size_bytes = item.get("size_bytes") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("title"), str)
                or not isinstance(item.get("token"), str)
                or not item["token"]
                or not isinstance(item.get("category"), str)
                or not isinstance(item.get("size"), str)
                or (
                    size_bytes is not None
                    and (
                        not isinstance(size_bytes, int)
                        or isinstance(size_bytes, bool)
                        or size_bytes < 0
                    )
                )
                or not isinstance(item.get("date"), str)
                or not isinstance(item.get("metadata"), dict)
            ):
                raise UsenetFinderStateError(
                    "Saved Usenet Finder search cache contains an invalid result."
                )
            results.append(
                FinderResult(
                    title=item["title"],
                    token=item["token"],
                    category=item["category"],
                    size=item["size"],
                    size_bytes=size_bytes,
                    date=item["date"],
                    metadata=item["metadata"],
                )
            )
        return FinderSearchPage(tuple(results), value["has_more"])

    @staticmethod
    def _package_to_json(package: FinderPackage) -> dict[str, object]:
        return {
            "name": package.name,
            "metadata": dict(package.metadata),
            "files": [
                {
                    "name": file.name,
                    "link": file.link,
                    "size": file.size,
                    "is_video": file.is_video,
                    "inaccessible": file.inaccessible,
                    "metadata": dict(file.metadata),
                }
                for file in package.files
            ],
        }

    @staticmethod
    def _package_from_json(value: object) -> FinderPackage:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("name"), str)
            or not isinstance(value.get("metadata"), dict)
            or not isinstance(value.get("files"), list)
        ):
            raise UsenetFinderStateError(
                "Saved Usenet Finder resolved-link cache contains an invalid package."
            )
        files: list[FinderFile] = []
        for item in value["files"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("name"), str)
                or not isinstance(item.get("link"), str)
                or not isinstance(item.get("size"), str)
                or not isinstance(item.get("is_video"), bool)
                or not isinstance(item.get("metadata"), dict)
                or (
                    item.get("inaccessible") is not None
                    and not isinstance(item.get("inaccessible"), str)
                )
            ):
                raise UsenetFinderStateError(
                    "Saved Usenet Finder resolved-link cache contains an invalid file."
                )
            files.append(
                FinderFile(
                    name=item["name"],
                    link=item["link"],
                    size=item["size"],
                    is_video=item["is_video"],
                    inaccessible=item["inaccessible"],
                    metadata=item["metadata"],
                )
            )
        return FinderPackage(
            name=value["name"],
            files=tuple(files),
            metadata=value["metadata"],
        )
