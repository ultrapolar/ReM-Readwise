"""Small shared helpers."""

from __future__ import annotations

import re

_UNSAFE = re.compile(r'[\\/:*?"<>|]+')
_WS = re.compile(r"\s+")


def sanitize_name(title: str, *, max_length: int = 120) -> str:
    """Make a Reader title safe to use as a reMarkable document / file name.

    The reMarkable document name is derived from the uploaded filename, so this
    must be deterministic: the same title always yields the same name, which is
    how the reverse sync maps an annotated document back to its Reader doc.
    """
    name = _UNSAFE.sub(" ", title)
    name = _WS.sub(" ", name).strip().strip(".")
    if len(name) > max_length:
        name = name[:max_length].rstrip()
    return name or "Untitled"
