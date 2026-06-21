"""Extract PDF highlights from a downloaded reMarkable document.

When you highlight text on a PDF on the reMarkable, the device records a
``GlyphRange`` scene item in that page's ``.rm`` file — containing the actual
highlighted *text*, its color, and bounding rectangles. We read those with
rmscene (https://github.com/ricklupton/rmscene) and pair them with the page
order from the document's ``.content`` file to recover the original PDF page
number for each highlight.

The reMarkable archive (a ``.rmdoc``/zip produced by ``rmapi get``) looks like::

    <docUUID>.content          # JSON: file type + ordered page list
    <docUUID>.pdf              # the original PDF
    <docUUID>/<pageUUID>.rm    # one scene file per page (annotations live here)
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from pathlib import Path
from typing import Any

from rem_readwise.models import RmHighlight

logger = logging.getLogger(__name__)


def parse_content_page_order(content: dict[str, Any]) -> list[tuple[str, int]]:
    """Return ``[(page_uuid, pdf_page_index), ...]`` in reading order.

    Supports both the modern ``cPages.pages`` layout (software 3.x) and the
    legacy top-level ``pages`` list. ``pdf_page_index`` honours each page's
    ``redir`` value when present (so inserted pages don't shift the mapping),
    otherwise falls back to the position in the list. Deleted pages are skipped.
    """
    ordered: list[tuple[str, int]] = []

    c_pages = content.get("cPages")
    if isinstance(c_pages, dict) and isinstance(c_pages.get("pages"), list):
        position = 0
        for page in c_pages["pages"]:
            if not isinstance(page, dict):
                continue
            if _is_deleted(page):
                continue
            uuid = page.get("id")
            if not uuid:
                continue
            pdf_index = _redir_index(page)
            ordered.append((str(uuid), pdf_index if pdf_index is not None else position))
            position += 1
        return ordered

    # Legacy format: "pages" is a flat list of page UUIDs in order.
    legacy = content.get("pages")
    if isinstance(legacy, list):
        ordered = [(str(uuid), idx) for idx, uuid in enumerate(legacy) if uuid]
    return ordered


def _is_deleted(page: dict[str, Any]) -> bool:
    deleted = page.get("deleted")
    if isinstance(deleted, dict):
        return bool(deleted.get("value"))
    return bool(deleted)


def _redir_index(page: dict[str, Any]) -> int | None:
    redir = page.get("redir")
    value = redir.get("value") if isinstance(redir, dict) else redir
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _color_name(glyph_range: Any) -> str:
    """Best-effort human color name for a GlyphRange."""
    color = getattr(glyph_range, "color", None)
    name = getattr(color, "name", None)
    if isinstance(name, str) and name:
        return name.lower()
    if color is not None:
        return str(color).lower()
    return "yellow"


def _sort_key(glyph_range: Any) -> tuple[float, float]:
    """Reading-order key (top-to-bottom, then left-to-right) from rectangles."""
    rects = getattr(glyph_range, "rectangles", None) or []
    ys, xs = [], []
    for rect in rects:
        y = getattr(rect, "y", None)
        x = getattr(rect, "x", None)
        if y is not None:
            ys.append(float(y))
        if x is not None:
            xs.append(float(x))
    return (min(ys) if ys else 0.0, min(xs) if xs else 0.0)


def extract_glyph_ranges(rm_bytes: bytes) -> list[Any]:
    """Read a single ``.rm`` file and return its GlyphRange items.

    Isolated here so the byte-level rmscene dependency stays in one place and a
    single malformed page can't abort the whole document.
    """
    try:
        from rmscene import read_blocks
        from rmscene.scene_items import GlyphRange
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise RuntimeError("rmscene is required to parse reMarkable highlights") from exc

    ranges: list[Any] = []
    for block in read_blocks(io.BytesIO(rm_bytes)):
        item = getattr(block, "item", None)
        value = getattr(item, "value", None) if item is not None else None
        if isinstance(value, GlyphRange):
            ranges.append(value)
    return ranges


def highlights_for_page(rm_bytes: bytes, pdf_page_index: int) -> list[RmHighlight]:
    """Extract ordered highlights for one page's ``.rm`` content."""
    try:
        glyph_ranges = extract_glyph_ranges(rm_bytes)
    except Exception:  # noqa: BLE001 - never let one page kill the sync
        logger.exception("Failed to parse a reMarkable page; skipping it")
        return []

    glyph_ranges.sort(key=_sort_key)
    highlights: list[RmHighlight] = []
    for gr in glyph_ranges:
        text = (getattr(gr, "text", "") or "").strip()
        if not text:
            continue
        highlights.append(
            RmHighlight(
                page_index=pdf_page_index,
                text=text,
                color=_color_name(gr),
                order=len(highlights),  # reading order among kept highlights
            )
        )
    return highlights


def extract_highlights(archive_path: Path) -> list[RmHighlight]:
    """Extract all highlights from a downloaded reMarkable archive (zip)."""
    with zipfile.ZipFile(archive_path) as zf:
        names = zf.namelist()
        content_name = _find_suffix(names, ".content")
        if content_name is None:
            logger.warning("No .content file in %s; nothing to extract", archive_path)
            return []
        content = json.loads(zf.read(content_name).decode("utf-8"))
        doc_uuid = content_name[: -len(".content")]
        page_order = parse_content_page_order(content)

        rm_lookup = {name: name for name in names if name.endswith(".rm")}

        highlights: list[RmHighlight] = []
        for page_uuid, pdf_index in page_order:
            rm_name = _resolve_rm(rm_lookup, doc_uuid, page_uuid)
            if rm_name is None:
                continue
            highlights.extend(highlights_for_page(zf.read(rm_name), pdf_index))
    logger.info("Extracted %d highlight(s) from %s", len(highlights), archive_path.name)
    return highlights


def _find_suffix(names: list[str], suffix: str) -> str | None:
    matches = [n for n in names if n.endswith(suffix)]
    # Prefer a top-level file (no nested directory) for the document metadata.
    matches.sort(key=lambda n: (n.count("/"), len(n)))
    return matches[0] if matches else None


def _resolve_rm(rm_lookup: dict[str, str], doc_uuid: str, page_uuid: str) -> str | None:
    candidates = [
        f"{doc_uuid}/{page_uuid}.rm",
        f"{page_uuid}.rm",
    ]
    for cand in candidates:
        if cand in rm_lookup:
            return cand
    # Fall back to any path that ends with this page's filename.
    suffix = f"{page_uuid}.rm"
    for name in rm_lookup:
        if name.endswith(suffix):
            return name
    return None
