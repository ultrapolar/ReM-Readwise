"""Persistent sync state, stored as a small JSON file.

Tracks two things so the sync is idempotent:

* which Reader documents have already been uploaded to the reMarkable (and
  under what document name), and
* which highlights have already been pushed back to Readwise (by dedup key),
  so re-reading the same annotated PDF never creates duplicates.

Pushed dedup keys are persisted as ``"<reader_id>:<dedup_key>"`` so that
:meth:`SyncState.prune` can drop a document's keys along with its entry.
Bare keys written by older versions still deduplicate (``dedup_key()``
already hashes the reader id into the digest) but can never be attributed
to a document, so pruning leaves them alone.

Backups: losing or corrupting this file makes everything re-upload and
re-push, so ``save()`` keeps rotating copies (``state.json.1`` is the newest,
up to ``backups``). Rotation is *armed*, not automatic: the first ``save()``
after construction — or after :meth:`arm_rotation` — snapshots the previous
on-disk file, and later saves just overwrite in place. The engine re-arms
once per sync cycle, so the several mid-cycle saves cannot churn through
every backup within a single cycle.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SyncState:
    def __init__(self, path: Path, *, backups: int = 3) -> None:
        self._path = path
        self._backups = max(0, backups)
        self._lock = threading.Lock()
        self._rotation_armed = True
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
        # The oldest state files stored a bare name string per document.
        docs = self._data["documents"]
        for reader_id, entry in list(docs.items()):
            if isinstance(entry, str):
                docs[reader_id] = {"remarkable_name": entry}
        self._pushed = set(self._data["pushed_highlights"])

    def save(self) -> None:
        with self._lock:
            self._data["pushed_highlights"] = sorted(self._pushed)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if self._rotation_armed:
                self._rotation_armed = False
                self._rotate_backups()
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), "utf-8")
            tmp.replace(self._path)

    def arm_rotation(self) -> None:
        """Make the next ``save()`` snapshot the current file (see module docs)."""
        with self._lock:
            self._rotation_armed = True

    def _rotate_backups(self) -> None:
        """Shift ``state.json`` -> ``.1`` -> ... -> ``.{backups}``.

        The live file is *copied* (not renamed) into ``.1`` so it stays in
        place until ``save()`` atomically replaces it — a crash mid-rotation
        never loses the current state. Backups are best-effort: a failure is
        logged and must never block saving the real state.
        """
        if self._backups <= 0 or not self._path.exists():
            return
        try:
            for i in range(self._backups - 1, 0, -1):
                older = Path(f"{self._path}.{i}")
                if older.exists():
                    older.replace(f"{self._path}.{i + 1}")
            shutil.copy2(self._path, f"{self._path}.1")
        except OSError:
            logger.warning("Could not rotate backups of %s", self._path, exc_info=True)

    # ── uploaded documents ────────────────────────────────────────────────
    def is_uploaded(self, reader_id: str) -> bool:
        return reader_id in self._data["documents"]

    def mark_uploaded(
        self, reader_id: str, remarkable_name: str, device_id: str | None = None
    ) -> None:
        entry: dict[str, Any] = {"remarkable_name": remarkable_name}
        if device_id:
            entry["device_id"] = device_id
        self._data["documents"][reader_id] = entry

    def remarkable_name_for(self, reader_id: str) -> str | None:
        entry = self._data["documents"].get(reader_id)
        return entry.get("remarkable_name") if entry else None

    def reader_id_for_name(self, remarkable_name: str) -> str | None:
        for reader_id, entry in self._data["documents"].items():
            if entry.get("remarkable_name") == remarkable_name:
                return reader_id
        return None

    # ── device ids (rename-proofing) ──────────────────────────────────────
    def device_id_for(self, reader_id: str) -> str | None:
        entry = self._data["documents"].get(reader_id)
        return entry.get("device_id") if entry else None

    def set_device_id(self, reader_id: str, device_id: str) -> None:
        entry = self._data["documents"].get(reader_id)
        if entry is not None and device_id:
            entry["device_id"] = device_id

    def reader_id_for_device_id(self, device_id: str) -> str | None:
        for reader_id, entry in self._data["documents"].items():
            if entry.get("device_id") == device_id:
                return reader_id
        return None

    def rename_document(self, reader_id: str, new_name: str) -> None:
        """Heal the stored name after an on-device rename (identified by device id)."""
        entry = self._data["documents"].get(reader_id)
        if entry is not None:
            entry["remarkable_name"] = new_name

    # ── pushed highlights ─────────────────────────────────────────────────
    def is_pushed(self, reader_id: str, dedup_key: str) -> bool:
        # Bare keys are what pre-namespace versions stored; still honor them.
        return f"{reader_id}:{dedup_key}" in self._pushed or dedup_key in self._pushed

    def mark_pushed(self, reader_id: str, dedup_key: str) -> None:
        self._pushed.add(f"{reader_id}:{dedup_key}")

    # ── pruning ───────────────────────────────────────────────────────────
    def prune(self, active_reader_ids: set[str], device_names: set[str]) -> int:
        """Forget documents that are gone from BOTH Reader and the device.

        Conservative by design: a document still present on either side keeps
        its entry and pushed dedup keys, so nothing can re-upload or re-push.
        Legacy un-namespaced keys are unattributable and are always kept.
        Returns the number of documents pruned.
        """
        with self._lock:
            stale = {
                reader_id
                for reader_id, entry in self._data["documents"].items()
                if reader_id not in active_reader_ids
                and entry.get("remarkable_name") not in device_names
            }
            for reader_id in stale:
                del self._data["documents"][reader_id]
            if stale:
                # Keys are "<reader_id>:<sha1-hex>"; the digest has no colon,
                # so rsplit recovers the exact namespace (ids may contain ":").
                self._pushed = {
                    key
                    for key in self._pushed
                    if ":" not in key or key.rsplit(":", 1)[0] not in stale
                }
            return len(stale)

    @property
    def uploaded_count(self) -> int:
        return len(self._data["documents"])

    @property
    def pushed_count(self) -> int:
        return len(self._pushed)
