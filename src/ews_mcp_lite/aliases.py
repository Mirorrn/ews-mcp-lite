"""Short id aliases (m1, e2, d3, f4, p5, k6, t7) held in memory.

EWS ids are ~150 characters; models copy short tokens reliably and cheaply.
The store is process-local and lost on restart, which is fine: a stale alias
yields a clean validation error telling the model to re-run the list tool.
"""

import threading

PREFIXES = {
    "m": "message",
    "d": "draft",
    "e": "event",
    "f": "folder",
    "p": "person",
    "k": "task",
    "t": "thread",
}


class AliasStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fwd: dict[str, tuple[str, str | None]] = {}  # alias -> (raw id, changekey)
        self._rev: dict[tuple[str, str], str] = {}  # (prefix, raw id) -> alias
        self._counter: dict[str, int] = {}

    def alias(self, raw_id: str, prefix: str, changekey: str | None = None) -> str:
        if prefix not in PREFIXES:
            raise ValueError(f"unknown alias prefix {prefix!r}")
        with self._lock:
            key = (prefix, raw_id)
            existing = self._rev.get(key)
            if existing:
                if changekey:
                    self._fwd[existing] = (raw_id, changekey)
                return existing
            n = self._counter.get(prefix, 0) + 1
            self._counter[prefix] = n
            alias = f"{prefix}{n}"
            self._fwd[alias] = (raw_id, changekey)
            self._rev[key] = alias
            return alias

    def rebind(self, alias: str, raw_id: str, changekey: str | None = None) -> None:
        """Point an alias at a new raw id (EWS ids change when items move)."""
        with self._lock:
            old = self._fwd.get(alias)
            if old:
                self._rev.pop((alias[0], old[0]), None)
            self._fwd[alias] = (raw_id, changekey)
            self._rev[(alias[0], raw_id)] = alias

    def resolve(self, ref: str, expect: tuple[str, ...] | None = None) -> tuple[str, str | None]:
        """Alias or raw id -> (raw id, changekey). Raises ValueError for stale aliases."""
        ref = (ref or "").strip()
        if not ref:
            raise ValueError("id is required")
        if is_alias(ref):
            if expect and ref[0] not in expect:
                kinds = ", ".join(f"{p}… ({PREFIXES[p]})" for p in expect)
                raise ValueError(f"'{ref}' is a {PREFIXES[ref[0]]} id; expected {kinds}")
            with self._lock:
                hit = self._fwd.get(ref)
            if hit is None:
                raise ValueError(
                    f"Unknown id '{ref}': it is stale or from before a restart. "
                    "Re-run the list/search tool to get fresh ids."
                )
            return hit
        return ref, None  # raw EWS id passed through

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counter)


def is_alias(ref: str) -> bool:
    return len(ref) >= 2 and ref[0] in PREFIXES and ref[1:].isdigit()
