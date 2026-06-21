"""Forward sync: push Readwise Reader PDFs onto the reMarkable."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from rem_readwise.models import ReaderDocument
from rem_readwise.readwise import ReadwiseClient, ReadwiseDownloadError
from rem_readwise.remarkable import RemarkableClient
from rem_readwise.sync.state import SyncState
from rem_readwise.util import sanitize_name

logger = logging.getLogger(__name__)


@dataclass
class ForwardResult:
    considered: int = 0
    uploaded: int = 0
    skipped_existing: int = 0
    skipped_no_source: int = 0
    failed: int = 0


class ForwardSync:
    def __init__(
        self,
        readwise: ReadwiseClient,
        remarkable: RemarkableClient,
        state: SyncState,
        *,
        folder: str,
        work_dir: Path,
        category: str = "pdf",
        location: str | None = None,
        dry_run: bool = False,
    ) -> None:
        self._readwise = readwise
        self._remarkable = remarkable
        self._state = state
        self._folder = folder
        self._work_dir = work_dir
        self._category = category
        self._location = location
        self._dry_run = dry_run

    def run(self, documents: list[ReaderDocument] | None = None) -> ForwardResult:
        result = ForwardResult()
        self._remarkable.ensure_folder(self._folder)
        pdf_dir = self._work_dir / "pdfs"

        if documents is None:
            documents = list(
                self._readwise.list_documents(
                    category=self._category, location=self._location
                )
            )

        for doc in documents:
            result.considered += 1
            if self._state.is_uploaded(doc.id):
                result.skipped_existing += 1
                continue

            name = sanitize_name(doc.title)
            if self._dry_run:
                logger.info("[dry-run] would upload %r -> reMarkable:%s", name, self._folder)
                result.uploaded += 1
                continue

            try:
                local_pdf = pdf_dir / f"{name}.pdf"
                self._readwise.download_document(doc, local_pdf)
                self._remarkable.upload_pdf(local_pdf, self._folder)
                self._state.mark_uploaded(doc.id, name)
                self._state.save()
                result.uploaded += 1
                local_pdf.unlink(missing_ok=True)
            except ReadwiseDownloadError as exc:
                # Expected for uploaded PDFs Reader won't serve back. Leave the
                # doc unmarked so it retries if a source becomes available.
                logger.warning("Skipping %r: %s", doc.title, exc)
                result.skipped_no_source += 1
            except Exception:  # noqa: BLE001 - keep going through the library
                logger.exception("Failed to upload %r (%s)", doc.title, doc.id)
                result.failed += 1

        logger.info(
            "Forward sync: %d considered, %d uploaded, %d already there, "
            "%d unretrievable, %d failed",
            result.considered,
            result.uploaded,
            result.skipped_existing,
            result.skipped_no_source,
            result.failed,
        )
        return result
