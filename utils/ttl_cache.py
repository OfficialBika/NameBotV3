from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class TTLCache(Generic[K, V]):
    def __init__(self, max_items: int = 1000, ttl_seconds: int = 60) -> None:
        self.max_items = max(1, int(max_items))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def get(self, key: K) -> V | None:
        now = time.monotonic()
        item = self._data.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= now:
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: K, value: V) -> None:
        self._data[key] = (time.monotonic() + self.ttl_seconds, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)

    def delete(self, key: K) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
