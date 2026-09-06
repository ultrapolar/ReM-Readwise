"""Persistent sync state, stored as a small JSON file.

Tracks two things so the sync is idempotent:

* which Reader documents have already been uploaded to the reMarkable (and
  under what document name), and
* which highlights have already been pushed back to Readwise (by dedup key),
  so re-reading the same annotated PDF never creates duplicates.

The reMarkable document name is the *only* link from an annotated document on
the device back to the Reader document its highlights belong to, so names must
be unique per Reader id. ``unique_remarkable_name`` enforces that at upload
time; ``reader_id_for_name`` refuses to guess if an older state file already
holds a duplicate.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SUFFIX = re.compile(r" \((\d+)\)$")


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
        self._warn_about_duplicate_names()

    def _warn_about_duplicate_names(self) -> None:
        by_name: dict[str, list[str]] = {}
        for reader_id, entry in self._data["documents"].items():
            name = entry.get("remarkable_name")
            if name:
                by_name.setdefault(name, []).append(reader_id)
        for name, ids in by_name.items():
            if len(ids) > 1:
                logger.warning(
                    "State maps reMarkable document %r to %d Reader documents (%s); "
                    "highlights from it will not be pushed until this is resolved",
                    name,
                    len(ids),
                    ", ".join(ids),
                )

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

    def is_name_taken(self, remarkable_name: str, *, by: str | None = None) -> bool:
        """True if some *other* Reader document (than ``by``) already owns the name."""
        for reader_id, entry in self._data["documents"].items():
            if entry.get("remarkable_name") == remarkable_name and reader_id != by:
                return True
        return False

    def unique_remarkable_name(self, desired: str, reader_id: str) -> str:
        """Return ``desired``, or ``desired (2)``, ``desired (3)``... — the first
        variant not already used by a different Reader document.

        Two Reader documents can easily share a title ("Notes", "Untitled", the
        same paper saved twice), and ``sanitize_name`` maps distinct titles onto
        the same string. Without this the reverse pass would attach one
        document's highlights to whichever Reader doc happened to be listed
        first.
        """
        if not self.is_name_taken(desired, by=reader_id):
            return desired

        base = _SUFFIX.sub("", desired)
        n = 2
        while True:
            candidate = f"{base} ({n})"
            if not self.is_name_taken(candidate, by=reader_id):
                logger.warning(
                    "reMarkable name %r is already used by another document; "
                    "uploading %s as %r",
                    desired,
                    reader_id,
                    candidate,
                )
                return candidate
            n += 1

    def reader_id_for_name(self, remarkable_name: str) -> str | None:
        """Map a reMarkable document name back to its Reader id.

        Returns ``None`` when there is no mapping *or* when the name is
        ambiguous (a legacy state file that recorded the same name for two
        documents). Skipping is the safe failure: pushing to the wrong document
        would be silent data corruption.
        """
        matches = [
            reader_id
            for reader_id, entry in self._data["documents"].items()
            if entry.get("remarkable_name") == remarkable_name
        ]
        if len(matches) > 1:
            logger.warning(
                "reMarkable document %r maps to %d Reader documents (%s); skipping it. "
                "Remove all but one from the state file, or rename the device documents.",
                remarkable_name,
                len(matches),
                ", ".join(matches),
            )
            return None
        return matches[0] if matches else None

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
