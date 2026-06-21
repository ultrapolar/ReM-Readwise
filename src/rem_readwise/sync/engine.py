"""Top-level sync engine: one cycle = forward pass then reverse pass."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from rem_readwise.config import Settings
from rem_readwise.readwise import ReadwiseClient
from rem_readwise.remarkable import RemarkableClient
from rem_readwise.sync.forward import ForwardResult, ForwardSync
from rem_readwise.sync.reverse import ReverseResult, ReverseSync
from rem_readwise.sync.state import SyncState

logger = logging.getLogger(__name__)


@dataclass
class CycleResult:
    forward: ForwardResult
    reverse: ReverseResult


class SyncEngine:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._state = SyncState(Path(settings.state_path))

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

            reverse = ReverseSync(
                readwise,
                remarkable,
                self._state,
                folder=settings.remarkable_folder,
                work_dir=work_dir,
                dry_run=settings.dry_run,
            )
            reverse_result = reverse.run(documents)

        self._state.save()
        return CycleResult(forward=forward_result, reverse=reverse_result)

    def run_forever(self) -> None:
        """Run sync cycles forever, sleeping ``sync_interval_seconds`` between."""
        interval = max(60, self._settings.sync_interval_seconds)
        logger.info("Starting sync service (every %d seconds)", interval)
        while True:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - a service must survive a bad cycle
                logger.exception("Sync cycle failed; will retry next interval")
            time.sleep(interval)

    @property
    def state(self) -> SyncState:
        return self._state
