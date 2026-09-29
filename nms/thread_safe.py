"""nms.thread_safe — Thread-safe storage for shared state."""

import threading
from typing import Any, Dict, Optional


class ThreadSafeDict:
    """Thread-safe dictionary wrapper for concurrent access."""

    def __init__(self, initial_dict: Optional[Dict] = None):
        self._lock = threading.RLock()
        self._data = dict(initial_dict) if initial_dict else {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def pop(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.pop(key, default)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def items(self):
        with self._lock:
            return list(self._data.items())

    def keys(self):
        with self._lock:
            return list(self._data.keys())

    def values(self):
        with self._lock:
            return list(self._data.values())

    def update(self, other: Dict) -> None:
        with self._lock:
            self._data.update(other)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def __iter__(self):
        with self._lock:
            return iter(list(self._data.keys()))

    def __delitem__(self, key: str) -> None:
        with self._lock:
            del self._data[key]

    def copy(self) -> Dict:
        with self._lock:
            return self._data.copy()
