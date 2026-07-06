"""Finish pass: docs moved to the Done folder on the tablet archive in Reader.

This makes the tablet the actual reading queue. Drop a finished doc into
``<folder>/Done`` and the next cycle:

1. pulls its remaining highlights — the reverse pass scans the Done folder
   too, and runs before this pass;
2. archives the document in Readwise Reader (v3 update API);
3. moves the device copy on to the archive folder, so Done stays empty.

Inbox docs have no Reader document to archive; their device copy is still
tidied into the archive folder. A doc whose Reader call fails stays in Done
and is retried next cycle (the archive call is idempotent). Docs the tool
never uploaded, and docs whose highlight pull failed this cycle, are left
untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rem_readwise.readwise import ReadwiseClient
from rem_readwise.remarkable import RemarkableClient
from rem_readwise.sync.inbox import INBOX_DOC_PREFIX
from rem_readwise.sync.resolve import resolve_reader_id
from rem_readwise.sync.state import SyncState

logger = logging.getLogger(__name__)


@dataclass
class FinishResult:
    considered: int = 0
    archived_in_reader: int = 0
    moved_to_archive: int = 0
    skipped_failed: int = 0
    unmatched: int = 0
    failed: int = 0


class FinishSync:
    def __init__(
        self,
        readwise: ReadwiseClient,
        remarkable: RemarkableClient,
        state: SyncState,
        *,
        done_folder: str,
        archive_folder: str,
        dry_run: bool = False,
    ) -> None:
        self._readwise = readwise
        self._remarkable = remarkable
        self._state = state
        self._done_folder = done_folder
        self._archive_folder = archive_folder
        self._dry_run = dry_run

    def run(self, skip_names: set[str] | None = None) -> FinishResult:
        """Process the Done folder. ``skip_names`` are docs whose reverse pass
        failed this cycle — archiving them would strand unpulled highlights."""
        skip_names = skip_names or set()
        result = FinishResult()

        for entry in self._remarkable.list_folder(self._done_folder):
            if entry.is_dir:
                continue
            result.considered += 1

            if entry.name in skip_names:
                logger.warning(
                    "Not finishing %r: its highlights could not be pulled this cycle",
                    entry.name,
                )
                result.skipped_failed += 1
                continue

            resolution = resolve_reader_id(
                entry.name, self._done_folder, self._remarkable, self._state,
                dry_run=self._dry_run,
            )
            reader_id = resolution.reader_id
            if not reader_id:
                logger.info(
                    "Doc %r in %s was not uploaded by this tool; leaving it",
                    entry.name,
                    self._done_folder,
                )
                result.unmatched += 1
                continue

            is_inbox_doc = reader_id.startswith(INBOX_DOC_PREFIX)
            if self._dry_run:
                logger.info(
                    "[dry-run] would %sarchive %r and move it to reMarkable:%s",
                    "" if not is_inbox_doc else "(inbox doc, Reader skip) ",
                    entry.name,
                    self._archive_folder,
                )
                if not is_inbox_doc:
                    result.archived_in_reader += 1
                result.moved_to_archive += 1
                continue

            if not is_inbox_doc:
                try:
                    self._readwise.archive_document(reader_id)
                except Exception:  # noqa: BLE001 - retried next cycle
                    logger.exception(
                        "Could not archive %r in Reader; leaving it in %s to retry",
                        entry.name,
                        self._done_folder,
                    )
                    result.failed += 1
                    continue
                result.archived_in_reader += 1
                logger.info("Archived %r in Readwise Reader", entry.name)

            try:
                self._remarkable.move(
                    f"{self._done_folder}/{entry.name}", self._archive_folder
                )
                result.moved_to_archive += 1
            except Exception:  # noqa: BLE001
                # Reader is already archived; a stuck copy in Done just re-runs
                # the (idempotent) archive call next cycle.
                logger.exception("Could not move %r out of %s", entry.name, self._done_folder)
                result.failed += 1

        if result.considered:
            logger.info(
                "Finish: %d in %s — %d archived in Reader, %d moved to archive, "
                "%d skipped, %d unmatched, %d failed",
                result.considered,
                self._done_folder,
                result.archived_in_reader,
                result.moved_to_archive,
                result.skipped_failed,
                result.unmatched,
                result.failed,
            )
        return result
