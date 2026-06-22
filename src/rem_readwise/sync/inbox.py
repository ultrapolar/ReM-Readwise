"""Inbox sync: upload local PDFs to the reMarkable so their highlights round-trip.

This is the reliable workaround for PDFs you *uploaded* into Readwise Reader,
which Reader won't serve back through the public API. Drop those PDFs into the
inbox folder (``INBOX_DIR``); each new file is uploaded to the reMarkable and
registered in the sync state under a synthetic ``inbox:<name>`` id. The reverse
pass then treats it like any other synced document and pushes the highlights you
make on it back to Readwise (titled after the file).

The original bytes never leave your machine except to go to the reMarkable, and
because the tool is the uploader, the on-device PDF is byte-identical to the
source — so page numbers line up exactly for highlight anchoring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from rem_readwise.models import ReaderDocument
from rem_readwise.remarkable import RemarkableClient
from rem_readwise.sync.state import SyncState
from rem_readwise.util import sanitize_name

logger = logging.getLogger(__name__)

INBOX_DOC_PREFIX = "inbox:"


@dataclass
class InboxResult:
    considered: int = 0
    uploaded: int = 0
    skipped_existing: int = 0
    failed: int = 0


class InboxSync:
    def __init__(
        self,
        remarkable: RemarkableClient,
        state: SyncState,
        *,
        folder: str,
        inbox_dir: Path,
        dry_run: bool = False,
    ) -> None:
        self._remarkable = remarkable
        self._state = state
        self._folder = folder
        self._inbox_dir = inbox_dir
        self._dry_run = dry_run

    def documents(self) -> list[ReaderDocument]:
        """Synthetic Reader documents for every PDF currently in the inbox.

        Used by the reverse pass to title highlights after the source file.
        """
        docs: list[ReaderDocument] = []
        for pdf in self._iter_pdfs():
            name = sanitize_name(pdf.stem)
            docs.append(
                ReaderDocument(id=f"{INBOX_DOC_PREFIX}{name}", title=name, category="pdf")
            )
        return docs

    def run(self) -> InboxResult:
        result = InboxResult()
        if not self._inbox_dir.exists():
            return result
        self._remarkable.ensure_folder(self._folder)

        for pdf in self._iter_pdfs():
            result.considered += 1
            name = sanitize_name(pdf.stem)
            doc_id = f"{INBOX_DOC_PREFIX}{name}"
            if self._state.is_uploaded(doc_id):
                result.skipped_existing += 1
                continue

            if self._dry_run:
                logger.info("[dry-run] would upload inbox PDF %r -> reMarkable:%s",
                            name, self._folder)
                result.uploaded += 1
                continue

            try:
                self._remarkable.upload_pdf(self._ensure_named(pdf, name), self._folder)
                self._state.mark_uploaded(doc_id, name)
                self._state.save()
                result.uploaded += 1
            except Exception:  # noqa: BLE001 - keep going through the inbox
                logger.exception("Failed to upload inbox PDF %r", pdf.name)
                result.failed += 1

        if result.considered:
            logger.info(
                "Inbox sync: %d considered, %d uploaded, %d already there, %d failed",
                result.considered,
                result.uploaded,
                result.skipped_existing,
                result.failed,
            )
        return result

    def _iter_pdfs(self) -> list[Path]:
        if not self._inbox_dir.exists():
            return []
        return sorted(
            p for p in self._inbox_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".pdf"
        )

    def _ensure_named(self, pdf: Path, name: str) -> Path:
        """Return a path whose stem is ``name`` so the reMarkable doc name is
        deterministic (and matches what the reverse pass looks up in state).

        If the file's stem already equals ``name`` we upload it directly;
        otherwise we stage a renamed copy in a hidden subfolder (so it is not
        picked up again on the next scan).
        """
        if pdf.stem == name:
            return pdf
        staged_dir = self._inbox_dir / ".staged"
        staged_dir.mkdir(exist_ok=True)
        staged = staged_dir / f"{name}.pdf"
        staged.write_bytes(pdf.read_bytes())
        return staged
