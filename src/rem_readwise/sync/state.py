"""Persistent sync state, stored as a small JSON file.

Tracks two things so the sync is idempotent:

* which Reader documents have already been uploaded to the reMarkable (and
  under what document name), and
* which highlights have already been pushed back to Readwise (by dedup key),
  so re-reading the same annotated PDF never creates duplicates.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SyncState:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"documents": {}, "pushed_highlights": []}
        self._pushed: set[str] = set()
        self._load()

    # ── persistence ───────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._data = json.loads(self._path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Could not read state at %s; starting fresh", self._path)
            self._data = {"documents": {}, "pushed_highlights": []}
        self._data.setdefault("documents", {})
        self._data.setdefault("pushed_highlights", [])
        self._pushed = set(self._data["pushed_highlights"])

    def save(self) -> None:
        with self._lock:
            self._data["pushed_highlights"] = sorted(self._pushed)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), "utf-8")
            tmp.replace(self._path)

    # ── uploaded documents ────────────────────────────────────────────────
    def is_uploaded(self, reader_id: str) -> bool:
        return reader_id in self._data["documents"]

    def mark_uploaded(self, reader_id: str, remarkable_name: str) -> None:
        self._data["documents"][reader_id] = {"remarkable_name": remarkable_name}

    def remarkable_name_for(self, reader_id: str) -> str | None:
        entry = self._data["documents"].get(reader_id)
        return entry.get("remarkable_name") if entry else None

    def reader_id_for_name(self, remarkable_name: str) -> str | None:
        for reader_id, entry in self._data["documents"].items():
            if entry.get("remarkable_name") == remarkable_name:
                return reader_id
        return None

    # ── pushed highlights ─────────────────────────────────────────────────
    def is_pushed(self, dedup_key: str) -> bool:
        return dedup_key in self._pushed

    def mark_pushed(self, dedup_key: str) -> None:
        self._pushed.add(dedup_key)

    @property
    def uploaded_count(self) -> int:
        return len(self._data["documents"])

    @property
    def pushed_count(self) -> int:
        return len(self._pushed)
