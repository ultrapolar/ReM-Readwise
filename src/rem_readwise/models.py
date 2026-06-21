"""Core data models shared across the sync pipeline."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


def normalize_text(text: str) -> str:
    """Collapse whitespace and lower-case for stable comparison/hashing."""
    return re.sub(r"\s+", " ", text).strip().lower()


@dataclass(frozen=True)
class ReaderDocument:
    """A document in Readwise Reader."""

    id: str
    title: str
    author: str | None = None
    source_url: str | None = None
    url: str | None = None
    category: str = "pdf"
    location: str | None = None

    @property
    def best_source(self) -> str | None:
        """URL Readwise should use to anchor returned highlights to this doc."""
        return self.source_url or self.url


@dataclass(frozen=True)
class RmHighlight:
    """A single highlight extracted from a reMarkable PDF annotation.

    ``page_index`` is 0-based and corresponds to the underlying PDF page.
    ``order`` is the reading order of this highlight within its page.
    """

    page_index: int
    text: str
    color: str = "yellow"
    order: int = 0

    @property
    def page_number(self) -> int:
        """1-based page number, as humans (and Readwise) count pages."""
        return self.page_index + 1

    def dedup_key(self, reader_doc_id: str) -> str:
        """Stable id for a highlight so re-syncs never duplicate it.

        Deliberately excludes color and order: re-coloring or re-ordering the
        same text on the same page should not create a second highlight.
        """
        payload = f"{reader_doc_id}|{self.page_index}|{normalize_text(self.text)}"
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()
