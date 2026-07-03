"""Reverse sync: pull highlights made on the reMarkable back into Readwise."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from rem_readwise.models import ReaderDocument
from rem_readwise.readwise import ReadwiseClient, build_highlight_payloads
from rem_readwise.remarkable import RemarkableClient, extract_highlights
from rem_readwise.sync.state import SyncState

logger = logging.getLogger(__name__)


@dataclass
class ReverseResult:
    documents_scanned: int = 0
    highlights_found: int = 0
    highlights_pushed: int = 0
    documents_unmatched: int = 0
    documents_renamed: int = 0
    failed: int = 0


class ReverseSync:
    def __init__(
        self,
        readwise: ReadwiseClient,
        remarkable: RemarkableClient,
        state: SyncState,
        *,
        folder: str,
        work_dir: Path,
        dry_run: bool = False,
    ) -> None:
        self._readwise = readwise
        self._remarkable = remarkable
        self._state = state
        self._folder = folder
        self._work_dir = work_dir
        self._dry_run = dry_run
        self._doc_index: dict[str, ReaderDocument] = {}

    def run(self, documents: list[ReaderDocument] | None = None) -> ReverseResult:
        """Scan the reMarkable folder and push new highlights to Readwise.

        ``documents`` (the Reader docs from the forward pass) lets us attach the
        correct title/author/source_url to each highlight without re-listing.
        """
        result = ReverseResult()
        if documents:
            self._doc_index = {doc.id: doc for doc in documents}

        download_dir = self._work_dir / "downloads"
        if download_dir.exists():
            shutil.rmtree(download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)

        healed = False
        for entry in self._remarkable.list_folder(self._folder):
            if entry.is_dir:
                continue
            reader_id = self._resolve_reader_id(entry.name, result)
            if not reader_id:
                logger.debug("No Reader mapping for reMarkable doc %r; skipping", entry.name)
                result.documents_unmatched += 1
                continue

            # Backfill the stable device id for docs uploaded before ids were
            # tracked, so a future rename of them is survivable too. One stat
            # per doc, once ever.
            if self._state.device_id_for(reader_id) is None and not self._dry_run:
                device_id = self._remarkable.stat(f"{self._folder}/{entry.name}")
                if device_id:
                    self._state.set_device_id(reader_id, device_id)
                    healed = True

            result.documents_scanned += 1
            try:
                self._process_document(entry.name, reader_id, download_dir, result)
            except Exception:  # noqa: BLE001 - one bad doc shouldn't stop the rest
                logger.exception("Failed processing reMarkable doc %r", entry.name)
                result.failed += 1

        if healed and not self._dry_run:
            self._state.save()

        logger.info(
            "Reverse sync: %d docs scanned, %d highlights found, %d pushed, "
            "%d unmatched, %d renamed",
            result.documents_scanned,
            result.highlights_found,
            result.highlights_pushed,
            result.documents_unmatched,
            result.documents_renamed,
        )
        return result

    def _resolve_reader_id(self, entry_name: str, result: ReverseResult) -> str | None:
        """Map a device doc to its Reader id — by name, then by stable device id.

        The device-id path is what makes renames survivable: when a name lookup
        misses, one ``stat`` recovers the cloud id, and if we know that id the
        doc was renamed on the device — heal the stored name and carry on. Docs
        we never uploaded stat to an unknown id and stay unmatched as before.
        """
        reader_id = self._state.reader_id_for_name(entry_name)
        if reader_id:
            return reader_id

        device_id = self._remarkable.stat(f"{self._folder}/{entry_name}")
        if not device_id:
            return None
        reader_id = self._state.reader_id_for_device_id(device_id)
        if not reader_id:
            return None

        old_name = self._state.remarkable_name_for(reader_id)
        logger.info(
            "Device doc renamed %r -> %r; healing the mapping (id %s)",
            old_name,
            entry_name,
            device_id,
        )
        result.documents_renamed += 1
        if not self._dry_run:
            self._state.rename_document(reader_id, entry_name)
            self._state.save()
        return reader_id

    def _process_document(
        self,
        remarkable_name: str,
        reader_id: str,
        download_dir: Path,
        result: ReverseResult,
    ) -> None:
        remote_path = f"{self._folder}/{remarkable_name}"
        archive = self._remarkable.download(remote_path, download_dir / remarkable_name)
        highlights = extract_highlights(archive)
        result.highlights_found += len(highlights)

        doc = self._doc_index.get(reader_id) or ReaderDocument(id=reader_id, title=remarkable_name)

        # Only push highlights we have not already sent for this document.
        fresh = [
            hl
            for hl in highlights
            if not self._state.is_pushed(reader_id, hl.dedup_key(reader_id))
        ]
        if not fresh:
            return

        payloads = build_highlight_payloads(doc, fresh)
        if self._dry_run:
            logger.info(
                "[dry-run] would push %d highlight(s) for %r", len(payloads), remarkable_name
            )
            result.highlights_pushed += len(payloads)
            return

        self._readwise.create_highlights(payloads)
        for hl in fresh:
            self._state.mark_pushed(reader_id, hl.dedup_key(reader_id))
        self._state.save()
        result.highlights_pushed += len(payloads)
        logger.info("Pushed %d highlight(s) for %r", len(payloads), remarkable_name)
