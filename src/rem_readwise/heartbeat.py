"""Liveness heartbeat and failure alerting for the always-on sync service.

The engine reports the outcome of every cycle to :class:`Heartbeat`, which
turns a silently wedged or repeatedly failing container into something you can
see:

* **Status file** (``STATUS_PATH``): a small JSON document rewritten after
  every cycle (atomically, same tmp-file-then-rename pattern as the sync
  state) recording when the cycle ran, whether it succeeded, and — on success —
  what it did. Point a Docker healthcheck or uptime monitor at ``ok`` and
  ``timestamp`` to detect trouble.
* **Webhook alert** (``ALERT_WEBHOOK_URL``): an optional Discord-compatible
  webhook (``POST {"content": "..."}``). One alert fires when
  ``ALERT_AFTER_FAILURES`` cycles have failed in a row, and one recovery
  message when a later cycle succeeds.

Nothing in here is allowed to take down the loop it watches: status-file and
webhook errors are logged and swallowed.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from rem_readwise.sync.engine import CycleResult

logger = logging.getLogger(__name__)

# Discord caps message content at 2000 chars; keep the error excerpt well under.
MAX_ALERT_ERROR_CHARS = 500
WEBHOOK_TIMEOUT_SECONDS = 10.0


class Heartbeat:
    """Records cycle outcomes: a status file every cycle, alerts on trouble."""

    def __init__(
        self,
        status_path: Path,
        *,
        webhook_url: str | None = None,
        alert_after: int = 3,
    ) -> None:
        self._status_path = status_path
        self._webhook_url = webhook_url or None
        self._alert_after = max(1, alert_after)
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._alerted = False

    # ── cycle outcomes ────────────────────────────────────────────────────
    def record_success(self, result: CycleResult) -> None:
        """Write a healthy status; send the recovery message if we had alerted."""
        failures = self._consecutive_failures
        self._consecutive_failures = 0
        self._last_error = None
        if self._alerted and self._post(
            f"rem-readwise RECOVERED: sync succeeded after {failures} failed cycle(s)."
        ):
            self._alerted = False
        self._write_status(
            ok=True,
            uploaded=result.forward.uploaded + result.inbox.uploaded,
            highlights_pushed=result.reverse.highlights_pushed,
            unmatched=result.reverse.documents_unmatched,
            failed=result.forward.failed + result.inbox.failed + result.reverse.failed,
        )

    def record_failure(self, error: BaseException | str) -> None:
        """Write a failing status; alert once the failure streak hits the bar."""
        self._consecutive_failures += 1
        if isinstance(error, BaseException):
            self._last_error = f"{type(error).__name__}: {error}"
        else:
            self._last_error = str(error)
        if self._consecutive_failures >= self._alert_after and not self._alerted:
            # The flag flips only on delivery, so a webhook blip at the
            # threshold is retried next cycle — one alert per outage, not one
            # per failing cycle.
            self._alerted = self._post(
                f"rem-readwise ALERT: {self._consecutive_failures} consecutive sync "
                f"failure(s). Last error: {self._last_error[:MAX_ALERT_ERROR_CHARS]}"
            )
        self._write_status(ok=False)

    # ── status file ───────────────────────────────────────────────────────
    def _write_status(self, *, ok: bool, **counts: int) -> None:
        status: dict[str, Any] = {
            "timestamp": dt.datetime.now(dt.UTC).isoformat(),
            "ok": ok,
            "consecutive_failures": self._consecutive_failures,
            "last_error": self._last_error,
            **counts,
        }
        try:
            self._status_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._status_path.with_suffix(self._status_path.suffix + ".tmp")
            tmp.write_text(json.dumps(status, indent=2, sort_keys=True), "utf-8")
            tmp.replace(self._status_path)
        except OSError:
            logger.exception("Could not write status file %s", self._status_path)

    # ── webhook ───────────────────────────────────────────────────────────
    def _post(self, content: str) -> bool:
        """POST a Discord-compatible message. True on delivery; never raises."""
        if not self._webhook_url:
            return False
        try:
            resp = httpx.post(
                self._webhook_url,
                json={"content": content},
                timeout=WEBHOOK_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
        except Exception:  # noqa: BLE001 - alerting must never crash the loop
            logger.exception("Alert webhook POST failed")
            return False
        return True
