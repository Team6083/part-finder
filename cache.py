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
