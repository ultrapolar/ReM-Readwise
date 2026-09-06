"""End-to-end orchestration test with in-memory fakes (no network/device)."""

from __future__ import annotations

from pathlib import Path

from rem_readwise.models import ReaderDocument, RmHighlight
from rem_readwise.readwise import ReadwiseDownloadError
from rem_readwise.remarkable.client import RemarkableEntry
from rem_readwise.sync import reverse as reverse_mod
from rem_readwise.sync.forward import ForwardSync
from rem_readwise.sync.reverse import ReverseSync
from rem_readwise.sync.state import SyncState
from rem_readwise.util import sanitize_name


class FakeReadwise:
    def __init__(self, docs):
        self._docs = docs
        self.created: list[dict] = []

    def list_documents(self, *, category=None, location=None):
        yield from self._docs

    def download_document(self, doc, dest: Path):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"%PDF-fake")
        return dest

    def create_highlights(self, payloads):
        self.created.extend(payloads)
        return payloads


class FakeRemarkable:
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


def test_full_loop_uploads_then_pushes_highlights_with_dedup(tmp_path, monkeypatch):
    doc = ReaderDocument(
        id="42",
        title="On Photography",
        author="Susan Sontag",
        source_url="https://example.com/p.pdf",
    )
    readwise = FakeReadwise([doc])
    remarkable = FakeRemarkable()
    state = SyncState(tmp_path / "state.json")
    work = tmp_path / "work"

    # ── forward: PDF lands on the device, recorded in state ────────────────
    forward = ForwardSync(
        readwise, remarkable, state, folder="Readwise", work_dir=work
    )
    fwd = forward.run([doc])
    assert fwd.uploaded == 1
    expected_name = sanitize_name(doc.title)
    assert remarkable.uploaded == [expected_name]
    assert state.is_uploaded("42")
    assert state.reader_id_for_name(expected_name) == "42"

    # ── highlight made on device ───────────────────────────────────────────
    device_highlights = [
        RmHighlight(page_index=2, text="A photograph is a trace", color="yellow"),
        RmHighlight(page_index=5, text="To collect photographs", color="green"),
    ]
    monkeypatch.setattr(
        reverse_mod, "extract_highlights", lambda _archive: device_highlights
    )

    # ── reverse: highlights pushed to Readwise, anchored by page ───────────
    reverse = ReverseSync(readwise, remarkable, state, folder="Readwise", work_dir=work)
    rev = reverse.run([doc])
    assert rev.highlights_pushed == 2
    assert len(readwise.created) == 2
    pushed = {p["text"]: p for p in readwise.created}
    assert pushed["A photograph is a trace"]["location"] == 3  # page_index 2 -> page 3
    assert pushed["A photograph is a trace"]["title"] == "On Photography"
    assert pushed["A photograph is a trace"]["source_url"] == "https://example.com/p.pdf"
    assert pushed["To collect photographs"]["note"] == ".green"

    # ── re-running reverse must NOT duplicate (dedup via state) ─────────────
    rev2 = reverse.run([doc])
    assert rev2.highlights_pushed == 0
    assert len(readwise.created) == 2  # unchanged


def test_unretrievable_pdf_is_skipped_and_retried(tmp_path):
    """A doc whose PDF can't be fetched is skipped, not marked uploaded."""

    class FailingReadwise(FakeReadwise):
        def download_document(self, doc, dest):
            raise ReadwiseDownloadError("no source")

    doc = ReaderDocument(id="99", title="Uploaded Only", category="pdf")
    readwise = FailingReadwise([doc])
    remarkable = FakeRemarkable()
    state = SyncState(tmp_path / "state.json")

    forward = ForwardSync(
        readwise, remarkable, state, folder="Readwise", work_dir=tmp_path / "work"
    )
    result = forward.run([doc])

    assert result.uploaded == 0
    assert result.skipped_no_source == 1
    assert remarkable.uploaded == []
    assert not state.is_uploaded("99")  # left for a future retry


def test_two_docs_with_the_same_title_keep_their_highlights_apart(tmp_path, monkeypatch):
    """The reverse pass must never attach one document's highlights to the other."""
    first = ReaderDocument(id="1", title="Notes", source_url="https://example.com/1.pdf")
    second = ReaderDocument(id="2", title="Notes", source_url="https://example.com/2.pdf")
    readwise = FakeReadwise([first, second])
    remarkable = FakeRemarkable()
    state = SyncState(tmp_path / "state.json")
    work = tmp_path / "work"

    fwd = ForwardSync(readwise, remarkable, state, folder="Readwise", work_dir=work).run(
        [first, second]
    )
    assert fwd.uploaded == 2
    assert remarkable.uploaded == ["Notes", "Notes (2)"]
    assert state.reader_id_for_name("Notes") == "1"
    assert state.reader_id_for_name("Notes (2)") == "2"

    # Highlights differ per device document.
    by_name = {
        "Notes": [RmHighlight(page_index=0, text="from the first")],
        "Notes (2)": [RmHighlight(page_index=0, text="from the second")],
    }
    downloaded: list[str] = []

    def fake_download(remote_path, dest_dir):
        downloaded.append(remote_path.rsplit("/", 1)[1])
        dest_dir.mkdir(parents=True, exist_ok=True)
        archive = dest_dir / "doc.rmdoc"
        archive.write_bytes(b"archive")
        return archive

    remarkable.download = fake_download
    monkeypatch.setattr(reverse_mod, "extract_highlights", lambda _a: by_name[downloaded[-1]])

    rev = ReverseSync(readwise, remarkable, state, folder="Readwise", work_dir=work).run(
        [first, second]
    )
    assert rev.highlights_pushed == 2
    pushed = {p["text"]: p["source_url"] for p in readwise.created}
    assert pushed == {
        "from the first": "https://example.com/1.pdf",
        "from the second": "https://example.com/2.pdf",
    }
