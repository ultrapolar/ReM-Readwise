"""Shared device-doc → Reader-id resolution (name first, stable id fallback)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from rem_readwise.remarkable import RemarkableClient
from rem_readwise.sync.state import SyncState

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Resolution:
    reader_id: str | None
    renamed: bool = False


def resolve_reader_id(
    entry_name: str,
    folder: str,
    remarkable: RemarkableClient,
    state: SyncState,
    *,
    dry_run: bool = False,
) -> Resolution:
    """Map a device doc to its Reader id — by name, then by stable device id.

    The device-id path is what makes renames survivable: when a name lookup
    misses, one ``stat`` recovers the cloud id, and if we know that id the doc
    was renamed on the device — the stored name is healed (and persisted) so
    the next cycle needs no stat at all. Docs we never uploaded stat to an
    unknown id and stay unresolved.
    """
    reader_id = state.reader_id_for_name(entry_name)
    if reader_id:
        return Resolution(reader_id)

    device_id = remarkable.stat(f"{folder}/{entry_name}")
    if not device_id:
        return Resolution(None)
    reader_id = state.reader_id_for_device_id(device_id)
    if not reader_id:
        return Resolution(None)

    logger.info(
        "Device doc renamed %r -> %r; healing the mapping (id %s)",
        state.remarkable_name_for(reader_id),
        entry_name,
        device_id,
    )
    if not dry_run:
        state.rename_document(reader_id, entry_name)
        state.save()
    return Resolution(reader_id, renamed=True)
