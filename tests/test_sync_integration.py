"""End-to-end orchestration test with in-memory fakes (no network/device)."""

from __future__ import annotations

from pathlib import Path

from rem_readwise.config import Settings
from rem_readwise.models import ReaderDocument, RmHighlight
from rem_readwise.readwise import ReadwiseDownloadError
from rem_readwise.remarkable.client import RemarkableEntry
from rem_readwise.sync import reverse as reverse_mod
from rem_readwise.sync.engine import SyncEngine
from rem_readwise.sync.forward import ForwardSync
from rem_readwise.sync.reverse import ReverseSync
from rem_readwise.sync.state import SyncState
from rem_readwise.util import sanitize_name


class FakeReadwise:
    def __init__(self, docs):
        self._docs = docs
        self.created: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

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


# ── engine-level prune wiring ──────────────────────────────────────────────


def _engine_settings(tmp_path, **overrides) -> Settings:
    return Settings(
        readwise_token="tok",
        state_path=str(tmp_path / "state.json"),
        status_path=str(tmp_path / "status.json"),
        work_dir=str(tmp_path / "work"),
        inbox_dir=str(tmp_path / "inbox"),
        _env_file=None,
        **overrides,
    )


def test_engine_prunes_state_for_docs_gone_from_both_sides(tmp_path, monkeypatch):
    engine = SyncEngine(_engine_settings(tmp_path))
    # A doc that has vanished from Reader AND from the device, plus its key.
    engine.state.mark_uploaded("stale", "Stale Doc")
    engine.state.mark_pushed("stale", "deadbeef")
    engine.state.save()

    doc = ReaderDocument(id="42", title="Fresh Paper")
    monkeypatch.setattr(engine, "_make_readwise", lambda: FakeReadwise([doc]))
    monkeypatch.setattr(engine, "_make_remarkable", lambda: FakeRemarkable())
    monkeypatch.setattr(reverse_mod, "extract_highlights", lambda _archive: [])

    engine.run_once()

    assert not engine.state.is_uploaded("stale")
    assert not engine.state.is_pushed("stale", "deadbeef")
    assert engine.state.is_uploaded("42")  # the live doc's state survives
    # ...and the pruned state is what got persisted.
    reloaded = SyncState(tmp_path / "state.json")
    assert not reloaded.is_uploaded("stale")
    assert reloaded.is_uploaded("42")


def test_engine_dry_run_never_prunes(tmp_path, monkeypatch):
    engine = SyncEngine(_engine_settings(tmp_path, dry_run=True))
    engine.state.mark_uploaded("stale", "Stale Doc")

    monkeypatch.setattr(engine, "_make_readwise", lambda: FakeReadwise([]))
    monkeypatch.setattr(engine, "_make_remarkable", lambda: FakeRemarkable())

    engine.run_once()

    assert engine.state.is_uploaded("stale")  # dry-run must not drop state
