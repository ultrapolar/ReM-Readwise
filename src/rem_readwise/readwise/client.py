"""Client for the Readwise Reader API (v3) and Readwise highlights API (v2).

Two halves of the loop live here:

* :meth:`ReadwiseClient.list_documents` / :meth:`download_document` pull PDFs
  *out* of Reader so they can be uploaded to the reMarkable.
* :meth:`ReadwiseClient.create_highlights` pushes device highlights *back*,
  anchored to the original Reader document by ``source_url`` + ``title`` and
  positioned with a page-level ``location``.

Docs: https://readwise.io/reader_api and https://readwise.io/api_deets
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from rem_readwise.models import ReaderDocument, RmHighlight

logger = logging.getLogger(__name__)


def _is_transient(exc: BaseException) -> bool:
    """Retry only on network blips, rate limiting, and server errors."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False

# Readwise caps a highlight's text length; stay safely under it.
MAX_HIGHLIGHT_CHARS = 8000
# The highlights API accepts batches; keep them modest to stay within limits.
HIGHLIGHT_BATCH_SIZE = 100


class ReadwiseError(RuntimeError):
    """Raised when the Readwise API returns an unrecoverable error."""


class ReadwiseDownloadError(ReadwiseError):
    """Raised when a document's source file can't be retrieved as expected."""


def _looks_like_pdf(path: Path) -> bool:
    """True if the file starts with the PDF magic number (``%PDF-``)."""
    try:
        with path.open("rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def parse_color_tags(spec: str) -> dict[str, str]:
    """Parse a ``COLOR_TAGS`` spec like ``"green=important, blue=question"``.

    Keys are highlighter colors (lower-cased); values are the Readwise tag to
    apply. Malformed entries are skipped with a warning rather than rejected —
    a typo in one mapping shouldn't take the sync down.
    """
    mapping: dict[str, str] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        color, sep, tag = chunk.partition("=")
        color, tag = color.strip().lower(), tag.strip().lstrip(".")
        if not sep or not color or not tag:
            logger.warning("Ignoring malformed COLOR_TAGS entry %r", chunk)
            continue
        mapping[color] = tag.replace(" ", "-")
    return mapping


def _color_note(color: str | None, color_tags: dict[str, str] | None) -> str | None:
    """The inline-tag note for a highlight color, or None for no tag.

    Explicit mappings win (and may tag yellow too); otherwise any non-yellow
    color becomes a tag named after itself, and yellow — the default marker —
    stays untagged.
    """
    if not color:
        return None
    color = color.lower()
    if color_tags and color in color_tags:
        return f".{color_tags[color]}"
    if color != "yellow":
        return f".{color}"
    return None


def build_highlight_payloads(
    doc: ReaderDocument,
    highlights: list[RmHighlight],
    *,
    highlighted_at: dt.datetime | None = None,
    color_tags: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Turn device highlights into Readwise highlight-create payloads.

    Pure function (no I/O) so it is easy to unit test. The page number drives
    ``location`` with ``location_type="page"`` so Readwise can anchor each
    snippet to the right page of the original PDF. Highlight colors become
    Readwise inline tags via the note field (see ``_color_note``).
    """
    stamp = (highlighted_at or dt.datetime.now(dt.UTC)).isoformat()
    payloads: list[dict[str, Any]] = []
    for hl in highlights:
        text = hl.text.strip()
        if not text:
            continue
        payload: dict[str, Any] = {
            "text": text[:MAX_HIGHLIGHT_CHARS],
            "title": doc.title,
            "category": "pdf",
            "location": hl.page_number,
            "location_type": "page",
            "highlighted_at": stamp,
            "source_type": "rem_readwise",
        }
        if doc.author:
            payload["author"] = doc.author
        if doc.best_source:
            payload["source_url"] = doc.best_source
        note = _color_note(hl.color, color_tags)
        if note:
            payload["note"] = note
        payloads.append(payload)
    return payloads


class ReadwiseClient:
    """Thin wrapper over the Readwise REST APIs."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = "https://readwise.io/api",
        client: httpx.Client | None = None,
    ) -> None:
        if not token:
            raise ReadwiseError("A Readwise token is required.")
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(timeout=60.0)

    # ── context management ────────────────────────────────────────────────
    def __enter__(self) -> ReadwiseClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Token {self._token}"}

    # ── Reader: list documents ────────────────────────────────────────────
    @retry(
        retry=retry_if_exception(_is_transient),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _list_page(self, params: dict[str, Any]) -> dict[str, Any]:
        resp = self._client.get(
            f"{self._base_url}/v3/list/", headers=self._headers, params=params
        )
        if resp.status_code == 429:
            logger.warning(
                "Readwise rate limited; Retry-After=%s", resp.headers.get("Retry-After")
            )
        resp.raise_for_status()
        return resp.json()

    def list_documents(
        self,
        *,
        category: str | None = "pdf",
        location: str | None = None,
    ) -> Iterator[ReaderDocument]:
        """Yield Reader documents, transparently following pagination."""
        params: dict[str, Any] = {}
        if category:
            params["category"] = category
        if location:
            params["location"] = location

        cursor: str | None = None
        while True:
            page_params = dict(params)
            if cursor:
                page_params["pageCursor"] = cursor
            data = self._list_page(page_params)
            for raw in data.get("results", []):
                yield _parse_document(raw)
            cursor = data.get("nextPageCursor")
            if not cursor:
                break

    # ── Reader: download the PDF bytes ────────────────────────────────────
    def download_document(self, doc: ReaderDocument, dest: Path) -> Path:
        """Download a Reader document's source file to ``dest``.

        Uses the document's ``source_url`` (the most reliable handle the public
        API exposes). Readwise-hosted assets are fetched with the auth header;
        external URLs are fetched anonymously.

        The result is validated: for PDFs we confirm the bytes really are a PDF.
        This matters because Reader does not expose a download URL for files you
        *uploaded* (vs. saved from the web), so ``source_url`` may be empty or
        point at an HTML page. Rather than push a broken "PDF" to the reMarkable,
        we raise :class:`ReadwiseDownloadError` so the caller can skip and retry
        later (the document is not marked as synced).
        """
        url = doc.best_source
        if not url:
            raise ReadwiseDownloadError(
                f"{doc.title!r} has no source_url. Reader does not expose a download "
                "URL for uploaded files; see the README 'limitations' section."
            )
        headers = self._headers if "readwise.io" in url else {}
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._client.stream("GET", url, headers=headers, follow_redirects=True) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("Content-Type", "")
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)

        if doc.category == "pdf" and not _looks_like_pdf(dest):
            dest.unlink(missing_ok=True)
            raise ReadwiseDownloadError(
                f"{doc.title!r}: source_url did not return a PDF "
                f"(Content-Type={content_type!r}, url={url}). This is usually an "
                "uploaded file Reader won't serve back; skipping."
            )
        logger.debug("Downloaded %s -> %s", doc.title, dest)
        return dest

    # ── Readwise: create highlights ───────────────────────────────────────
    @retry(
        retry=retry_if_exception(_is_transient),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _post_highlights(self, batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        resp = self._client.post(
            f"{self._base_url}/v2/highlights/",
            headers=self._headers,
            json={"highlights": batch},
        )
        resp.raise_for_status()
        return resp.json()

    def create_highlights(self, payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Create highlights in Readwise, batching to respect API limits."""
        created: list[dict[str, Any]] = []
        for start in range(0, len(payloads), HIGHLIGHT_BATCH_SIZE):
            batch = payloads[start : start + HIGHLIGHT_BATCH_SIZE]
            if not batch:
                continue
            created.extend(self._post_highlights(batch))
        return created

    # ── Reader: update a document ─────────────────────────────────────────
    @retry(
        retry=retry_if_exception(_is_transient),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def archive_document(self, doc_id: str) -> None:
        """Move a Reader document to the ``archive`` location (v3 update)."""
        resp = self._client.patch(
            f"{self._base_url}/v3/update/{doc_id}/",
            headers=self._headers,
            json={"location": "archive"},
        )
        resp.raise_for_status()


def _parse_document(raw: dict[str, Any]) -> ReaderDocument:
    return ReaderDocument(
        id=str(raw.get("id")),
        title=raw.get("title") or "Untitled",
        author=raw.get("author") or None,
        source_url=raw.get("source_url") or None,
        url=raw.get("url") or None,
        category=raw.get("category") or "pdf",
        location=raw.get("location") or None,
    )
