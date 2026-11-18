import json
import time
import pathlib
from abc import ABC, abstractmethod


class ResultCache(ABC):
    @abstractmethod
    def get(self, key: str, ttl: float | None) -> dict | None:
        """Return a cached result dict, or None on miss or expiry."""

    @abstractmethod
    def set(self, key: str, result: dict) -> None:
        """Store a result dict with the current timestamp."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove a key; a missing key is not an error."""


def _entry(result: dict) -> dict:
    return {"result": result, "cached_at": time.time()}


def _read_entry(entry: dict | None, ttl: float | None) -> dict | None:
    if entry is None or "cached_at" not in entry:
        return None
    if ttl is not None and time.time() - entry["cached_at"] > ttl:
        return None
    return entry["result"]


class MemoryCache(ResultCache):
    def __init__(self):
        self._store: dict = {}

    def get(self, key, ttl=None):
        return _read_entry(self._store.get(key), ttl)

    def set(self, key, result):
        self._store[key] = _entry(result)

    def delete(self, key):
        self._store.pop(key, None)


class FileCache(ResultCache):
    def __init__(self, path):
        self._path = pathlib.Path(path)

    def get(self, key, ttl=None):
        if not self._path.exists():
            return None
        with self._path.open() as f:
            return _read_entry(json.load(f).get(key), ttl)

    def set(self, key, result):
        existing = {}
        if self._path.exists():
            with self._path.open() as f:
                existing = json.load(f)
        existing[key] = _entry(result)
        with self._path.open("w") as f:
            json.dump(existing, f)

    def delete(self, key):
        if not self._path.exists():
            return
        with self._path.open() as f:
            existing = json.load(f)
        if existing.pop(key, None) is not None:
            with self._path.open("w") as f:
                json.dump(existing, f)


class UrlMap:
    """SKU -> product URL lookup layered on a ResultCache (long TTL).

    Lets a tool skip its search step: a hit goes straight to the product page.
    Keys are prefixed so they never collide with result-cache keys.
    """

    PREFIX = "map:"

    def __init__(self, cache: ResultCache, ttl: float | None = None):
        self._cache = cache
        self._ttl = ttl

    def _key(self, store: str, sku: str) -> str:
        return f"{self.PREFIX}{store}|{sku.upper()}"

    def get(self, store: str, sku: str) -> str | None:
        hit = self._cache.get(self._key(store, sku), self._ttl)
        return hit["url"] if hit else None

    def set(self, store: str, sku: str, url: str) -> None:
        self._cache.set(self._key(store, sku), {"url": url})

    def delete(self, store: str, sku: str) -> None:
        self._cache.delete(self._key(store, sku))
