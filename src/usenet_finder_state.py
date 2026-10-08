from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Mapping

from .secure_store import SecureStore
from .usenet_finder import FinderFile, FinderPackage


SEARCH_SETTING = "usenet_finder_last_search"
CACHE_SETTING = "usenet_finder_resolved_cache"
CACHE_PURPOSE = b"usenet-finder-resolved-cache-v1"
CACHE_TTL_SECONDS = 24 * 60 * 60


class UsenetFinderStateError(ValueError):
    pass


class UsenetFinderState:
    def __init__(
        self,
        secure_store: SecureStore,
        clock: Callable[[], float] = time.time,
    ):
        self.secure_store = secure_store
        self.clock = clock
        self._cache_loaded = False
        self._packages: dict[str, tuple[float, FinderPackage]] = {}

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
        self.secure_store.set_setting(
            SEARCH_SETTING,
            json.dumps({"query": query, "category": category}),
        )

    def get_resolved(self, token: str) -> FinderPackage | None:
        self._load_cache()
        cached = self._packages.get(token)
        if cached is None:
            return None
        resolved_at, package = cached
        if self.clock() - resolved_at >= CACHE_TTL_SECONDS:
            del self._packages[token]
            self._save_cache()
            return None
        return package

    def save_resolved(self, token: str, package: FinderPackage) -> None:
        self._load_cache()
        self._packages[token] = (self.clock(), package)
        self._remove_expired()
        self._save_cache()

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        saved = self.secure_store.get_encrypted_setting(CACHE_SETTING, CACHE_PURPOSE)
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
                resolved_at = entry.get("resolved_at")
                if (
                    not isinstance(resolved_at, (int, float))
                    or isinstance(resolved_at, bool)
                    or not math.isfinite(resolved_at)
                ):
                    raise UsenetFinderStateError(
                        "Saved Usenet Finder resolved-link cache has an invalid timestamp."
                    )
                if self.clock() - resolved_at < CACHE_TTL_SECONDS:
                    self._packages[token] = (
                        float(resolved_at),
                        self._package_from_json(entry.get("package")),
                    )
        self._cache_loaded = True
        if saved is not None and len(self._packages) != len(raw_cache):
            self._save_cache()

    def _remove_expired(self) -> None:
        now = self.clock()
        self._packages = {
            token: cached
            for token, cached in self._packages.items()
            if now - cached[0] < CACHE_TTL_SECONDS
        }

    def _save_cache(self) -> None:
        value = {
            token: {
                "resolved_at": resolved_at,
                "package": self._package_to_json(package),
            }
            for token, (resolved_at, package) in self._packages.items()
        }
        self.secure_store.set_encrypted_setting(
            CACHE_SETTING,
            json.dumps(value),
            CACHE_PURPOSE,
        )

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
