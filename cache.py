import json
import logging
import os
import tempfile
import threading
import time
import pathlib
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)


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
    """JSON-file cache. Safe for concurrent threads: writes are serialized and
    atomic (temp file + rename), and an unreadable file counts as an empty cache."""

    def __init__(self, path):
        self._path = pathlib.Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with self._path.open() as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.warning("cache file %s unreadable (%s); treating as empty", self._path, e)
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self._path.parent or ".", prefix=self._path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def get(self, key, ttl=None):
        with self._lock:
            return _read_entry(self._load().get(key), ttl)

    def set(self, key, result):
        with self._lock:
            data = self._load()
            data[key] = _entry(result)
            self._save(data)

    def delete(self, key):
        with self._lock:
            data = self._load()
            if data.pop(key, None) is not None:
                self._save(data)


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
        try:
            hit = self._cache.get(self._key(store, sku), self._ttl)
        except Exception as e:
            log.warning("URL map read failed (%s); treating as a miss", e)
            return None
        return hit["url"] if hit else None

    def set(self, store: str, sku: str, url: str) -> None:
        try:
            self._cache.set(self._key(store, sku), {"url": url})
        except Exception as e:
            log.warning("URL map write failed (%s); continuing without it", e)

    def delete(self, store: str, sku: str) -> None:
        try:
            self._cache.delete(self._key(store, sku))
        except Exception as e:
            log.warning("URL map delete failed (%s); continuing", e)
