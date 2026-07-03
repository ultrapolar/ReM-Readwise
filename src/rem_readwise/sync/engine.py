"""Top-level sync engine: one cycle = forward pass then reverse pass."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from rem_readwise.config import Settings
from rem_readwise.heartbeat import Heartbeat
from rem_readwise.readwise import ReadwiseClient
from rem_readwise.remarkable import RemarkableClient
from rem_readwise.sync.cleanup import CleanupResult, CleanupSync
from rem_readwise.sync.forward import ForwardResult, ForwardSync
from rem_readwise.sync.inbox import InboxResult, InboxSync
from rem_readwise.sync.reverse import ReverseResult, ReverseSync
from rem_readwise.sync.state import SyncState

logger = logging.getLogger(__name__)


@dataclass
class CycleResult:
    forward: ForwardResult
    inbox: InboxResult
    reverse: ReverseResult
    cleanup: CleanupResult = field(default_factory=CleanupResult)


class SyncEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._state = SyncState(
            Path(settings.state_path), backups=settings.state_backups
        )
        self._heartbeat = Heartbeat(
            Path(settings.status_path),
            webhook_url=settings.alert_webhook_url,
            alert_after=settings.alert_after_failures,
        )

    def _make_readwise(self) -> ReadwiseClient:
        return ReadwiseClient(
            self._settings.require_readwise_token(),
            base_url=self._settings.readwise_base_url,
        )

    def _make_remarkable(self) -> RemarkableClient:
        return RemarkableClient(
            rmapi_path=self._settings.rmapi_path,
            config_path=self._settings.rmapi_config,
        )

    def run_once(self) -> CycleResult:
        """Run a single full sync cycle."""
        settings = self._settings
        work_dir = Path(settings.work_dir)
        remarkable = self._make_remarkable()

        # One state backup per cycle: the first save() this cycle snapshots the
        # previous state file; the later mid-cycle saves overwrite in place.
        self._state.arm_rotation()

        if not remarkable.is_authenticated():
            raise RuntimeError(
                "reMarkable is not paired. Run `rem-readwise auth-remarkable <code>` "
                "with a code from https://my.remarkable.com/device/desktop/connect"
            )

        with self._make_readwise() as readwise:
            # Fetch the Reader library once; share it with both passes so the
            # reverse pass can anchor highlights with the right title/source_url.
            documents = list(
                readwise.list_documents(
                    category=settings.readwise_category,
                    location=settings.readwise_location,
                )
            )
            logger.info(
                "Reader returned %d %s document(s)",
                len(documents),
                settings.readwise_category,
            )

            forward = ForwardSync(
                readwise,
                remarkable,
                self._state,
                folder=settings.remarkable_folder,
                work_dir=work_dir,
                category=settings.readwise_category,
                location=settings.readwise_location,
                dry_run=settings.dry_run,
            )
            forward_result = forward.run(documents)

            # Inbox: local PDFs Reader can't serve back. Uploaded to the device
            # and registered so their highlights round-trip like any other doc.
            inbox = InboxSync(
                remarkable,
                self._state,
                folder=settings.remarkable_folder,
                inbox_dir=Path(settings.inbox_dir),
                dry_run=settings.dry_run,
            )
            inbox_result = inbox.run()

            reverse = ReverseSync(
                readwise,
                remarkable,
                self._state,
                folder=settings.remarkable_folder,
                work_dir=work_dir,
                dry_run=settings.dry_run,
            )
            inbox_docs = inbox.documents()
            reverse_result = reverse.run(documents + inbox_docs)

        active_ids = {doc.id for doc in documents} | {doc.id for doc in inbox_docs}
        device_names = {
            entry.name for entry in remarkable.list_folder(settings.remarkable_folder)
        }

        # Archive device copies of docs that left Reader. Runs after reverse
        # (their last highlights were just pulled) and never touches docs whose
        # reverse pass failed. Prune below reuses this pre-cleanup listing, so
        # a doc archived this cycle keeps its state until the next cycle.
        cleanup_result = CleanupResult()
        if settings.archive_removed:
            cleanup = CleanupSync(
                remarkable,
                self._state,
                folder=settings.remarkable_folder,
                archive_folder=settings.effective_archive_folder,
                dry_run=settings.dry_run,
            )
            cleanup_result = cleanup.run(
                active_ids, device_names, set(reverse_result.failed_names)
            )

        if not settings.dry_run:
            # Forget docs deleted from BOTH Reader and the device; anything
            # still on either side keeps its state (see SyncState.prune).
            pruned = self._state.prune(active_ids, device_names)
            if pruned:
                logger.info("Pruned %d stale document(s) from sync state", pruned)

        self._state.save()
        return CycleResult(
            forward=forward_result,
            inbox=inbox_result,
            reverse=reverse_result,
            cleanup=cleanup_result,
        )

    def run_forever(self) -> None:
        """Run sync cycles forever, sleeping ``sync_interval_seconds`` between."""
        interval = max(60, self._settings.sync_interval_seconds)
        logger.info("Starting sync service (every %d seconds)", interval)
        while True:
            try:
                result = self.run_once()
            except Exception as exc:  # noqa: BLE001 - a service must survive a bad cycle
                logger.exception("Sync cycle failed; will retry next interval")
                self._heartbeat.record_failure(exc)
            else:
                self._heartbeat.record_success(result)
            time.sleep(interval)

    @property
    def state(self) -> SyncState:
        return self._state
