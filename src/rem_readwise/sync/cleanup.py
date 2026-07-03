"""Cleanup sync: archive device copies of documents that left Readwise Reader.

Without this, every PDF ever synced stays in the reMarkable folder forever —
delete a doc in Reader (or move it out of the configured ``READWISE_LOCATION``)
and the tablet copy just lingers. This pass moves such copies into an archive
folder on the device (default ``<folder>/Archive``); nothing is ever deleted.

Safety rules, in order:

* only documents *this tool uploaded* (present in the sync state) are touched —
  foreign docs someone dropped into the folder are never moved;
* docs whose reverse processing failed this cycle are skipped, so highlights
  that haven't been pulled yet can't be stranded (reverse runs first and the
  archive folder is never scanned);
* the state entry survives until the next cycle's prune, which then sees the
  doc gone from both sides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rem_readwise.remarkable import RemarkableClient
from rem_readwise.sync.state import SyncState

logger = logging.getLogger(__name__)


@dataclass
class CleanupResult:
    considered: int = 0
    archived: int = 0
    skipped_failed: int = 0
    failed: int = 0


class CleanupSync:
    def __init__(
        self,
        remarkable: RemarkableClient,
        state: SyncState,
        *,
        folder: str,
        archive_folder: str,
        dry_run: bool = False,
    ) -> None:
        self._remarkable = remarkable
        self._state = state
        self._folder = folder
        self._archive_folder = archive_folder
        self._dry_run = dry_run

    def run(
        self,
        active_reader_ids: set[str],
        device_names: set[str],
        skip_names: set[str],
    ) -> CleanupResult:
        """Archive tracked docs that are gone from Reader but still in the folder.

        ``active_reader_ids`` must include inbox docs (they are never in the
        Reader list but must never be archived); ``device_names`` is the sync
        folder listing; ``skip_names`` are docs whose reverse pass failed.
        """
        result = CleanupResult()
        for reader_id, name in self._state.all_documents().items():
            if reader_id in active_reader_ids or not name or name not in device_names:
                continue
            result.considered += 1
            if name in skip_names:
                logger.warning(
                    "Not archiving %r: its highlights could not be pulled this cycle", name
                )
                result.skipped_failed += 1
                continue
            if self._dry_run:
                logger.info(
                    "[dry-run] would archive %r -> reMarkable:%s", name, self._archive_folder
                )
                result.archived += 1
                continue
            try:
                self._remarkable.move(f"{self._folder}/{name}", self._archive_folder)
                result.archived += 1
            except Exception:  # noqa: BLE001 - keep going through the rest
                logger.exception("Failed to archive %r", name)
                result.failed += 1

        if result.considered:
            logger.info(
                "Cleanup: %d gone from Reader — %d archived, "
                "%d skipped (failed reverse), %d failed",
                result.considered,
                result.archived,
                result.skipped_failed,
                result.failed,
            )
        return result
