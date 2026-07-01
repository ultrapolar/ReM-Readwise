"""Shared test fixtures and lightweight fakes."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rem_readwise.remarkable.client import RemarkableEntry


@dataclass
class FakeColor:
    """Stand-in for rmscene's PenColor enum (has a ``.name``)."""

    name: str


@dataclass
class FakeRect:
    x: float = 0.0
    y: float = 0.0
    w: float = 1.0
    h: float = 1.0


@dataclass
class FakeGlyphRange:
    """Duck-typed stand-in for rmscene.scene_items.GlyphRange."""

    text: str
    color: FakeColor = field(default_factory=lambda: FakeColor("YELLOW"))
    rectangles: list[FakeRect] = field(default_factory=list)


class FakeReadwise:
    """In-memory ReadwiseClient double (no network)."""

    def __init__(self, docs):
        self._docs = docs
        self.created: list[dict] = []
        self.list_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def list_documents(self, *, category=None, location=None):
        self.list_calls += 1
        yield from self._docs

    def download_document(self, doc, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-fake")
        return dest

    def create_highlights(self, payloads):
        self.created.extend(payloads)
        return payloads


class FakeRemarkable:
    """In-memory RemarkableClient double (no rmapi subprocess)."""

    def __init__(self):
        self.uploaded: list[str] = []

    def is_authenticated(self):
        return True

    def ensure_folder(self, folder):
        pass

    def upload_pdf(self, local_pdf: Path, folder: str):
        self.uploaded.append(local_pdf.stem)

    def list_folder(self, folder):
        return [RemarkableEntry(name=name, is_dir=False) for name in self.uploaded]

    def download(self, remote_path: str, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        archive = dest_dir / "doc.rmdoc"
        archive.write_bytes(b"archive")
        return archive
