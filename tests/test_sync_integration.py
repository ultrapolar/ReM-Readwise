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
    PRIMARY_FOLDER = "Readwise"

    def __init__(self):
        self.uploaded: list[str] = []  # contents of the primary sync folder
        self.folders: dict[str, list[str]] = {}  # any other folder -> names
        self.archived: list[tuple[str, str]] = []
        self.device_ids: dict[str, str] = {}  # current name -> stable cloud id
        self._id_counter = 0

    def _contents(self, folder: str) -> list[str]:
        if folder == self.PRIMARY_FOLDER:
            return self.uploaded
        return self.folders.setdefault(folder, [])

    def is_authenticated(self):
        return True

    def ensure_folder(self, folder):
        pass

    def upload_pdf(self, local_pdf: Path, folder: str):
        self.uploaded.append(local_pdf.stem)
        self._id_counter += 1
        self.device_ids[local_pdf.stem] = f"dev-{self._id_counter:04d}"

    def stat(self, remote_path: str):
        return self.device_ids.get(remote_path.rsplit("/", 1)[-1])

    def rename_on_device(self, old: str, new: str):
        """Test helper: what happens when the user renames a doc on the tablet."""
        self.device_ids[new] = self.device_ids.pop(old)
        self.uploaded[self.uploaded.index(old)] = new

    def move(self, remote_path: str, dest_folder: str):
        src_folder, name = remote_path.rsplit("/", 1)
        self._contents(src_folder).remove(name)
        self._contents(dest_folder).append(name)
        self.archived.append((name, dest_folder))

    def move_to_done(self, name: str):
        """Test helper: the user drags a finished doc into the Done folder."""
        self.move(f"{self.PRIMARY_FOLDER}/{name}", f"{self.PRIMARY_FOLDER}/Done")
        self.archived.pop()  # user action, not tool bookkeeping

    def list_folder(self, folder):
        return [RemarkableEntry(name=name, is_dir=False) for name in self._contents(folder)]

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


def test_rename_on_device_heals_mapping_and_keeps_highlights_flowing(
    tmp_path, monkeypatch
):
    doc = ReaderDocument(id="42", title="On Photography", author="Susan Sontag")
    readwise = FakeReadwise([doc])
    remarkable = FakeRemarkable()
    state = SyncState(tmp_path / "state.json")
    work = tmp_path / "work"

    ForwardSync(readwise, remarkable, state, folder="Readwise", work_dir=work).run([doc])
    original_name = sanitize_name(doc.title)
    assert state.device_id_for("42") == remarkable.device_ids[original_name]

    # User renames the doc on the tablet; name-based lookup would now orphan it.
    remarkable.rename_on_device(original_name, "sontag notes")

    monkeypatch.setattr(
        reverse_mod,
        "extract_highlights",
        lambda _archive: [RmHighlight(page_index=1, text="Renamed but found")],
    )
    reverse = ReverseSync(readwise, remarkable, state, folder="Readwise", work_dir=work)
    rev = reverse.run([doc])

    assert rev.documents_renamed == 1
    assert rev.documents_unmatched == 0
    assert rev.highlights_pushed == 1
    assert state.remarkable_name_for("42") == "sontag notes"  # healed
    # ...and persisted, so the next cycle needs no stat call at all.
    assert SyncState(tmp_path / "state.json").remarkable_name_for("42") == "sontag notes"


def test_foreign_device_doc_stays_unmatched(tmp_path, monkeypatch):
    remarkable = FakeRemarkable()
    remarkable.uploaded.append("Handwritten Journal")  # never uploaded by us, no id
    state = SyncState(tmp_path / "state.json")

    monkeypatch.setattr(reverse_mod, "extract_highlights", lambda _archive: [])
    rev = ReverseSync(
        FakeReadwise([]), remarkable, state, folder="Readwise", work_dir=tmp_path / "w"
    ).run([])

    assert rev.documents_unmatched == 1
    assert rev.documents_renamed == 0


def test_reverse_backfills_device_ids_for_pre_id_state(tmp_path, monkeypatch):
    remarkable = FakeRemarkable()
    state = SyncState(tmp_path / "state.json")

    # Simulate a doc uploaded by a pre-device-id version: name known, no id.
    remarkable.uploaded.append("Old Doc")
    remarkable.device_ids["Old Doc"] = "dev-legacy"
    state.mark_uploaded("7", "Old Doc")
    assert state.device_id_for("7") is None

    monkeypatch.setattr(reverse_mod, "extract_highlights", lambda _archive: [])
    ReverseSync(
        FakeReadwise([]), remarkable, state, folder="Readwise", work_dir=tmp_path / "w"
    ).run([])

    assert state.device_id_for("7") == "dev-legacy"


def test_doc_deleted_in_reader_is_archived_then_pruned_next_cycle(
    tmp_path, monkeypatch
):
    engine = SyncEngine(_engine_settings(tmp_path))
    doc = ReaderDocument(id="42", title="Short Lived")
    remarkable = FakeRemarkable()
    monkeypatch.setattr(engine, "_make_remarkable", lambda: remarkable)
    monkeypatch.setattr(reverse_mod, "extract_highlights", lambda _archive: [])

    # Cycle 1: doc exists in Reader and lands on the device.
    monkeypatch.setattr(engine, "_make_readwise", lambda: FakeReadwise([doc]))
    engine.run_once()
    name = sanitize_name(doc.title)
    assert name in remarkable.uploaded

    # Cycle 2: doc deleted in Reader -> device copy archived, state kept.
    monkeypatch.setattr(engine, "_make_readwise", lambda: FakeReadwise([]))
    result = engine.run_once()
    assert result.cleanup.archived == 1
    assert name not in remarkable.uploaded  # moved out of the sync folder
    assert remarkable.archived == [(name, "Readwise/Archive")]
    assert engine.state.is_uploaded("42")  # pre-cleanup listing shields it

    # Cycle 3: gone from both sides -> state pruned.
    result = engine.run_once()
    assert result.cleanup.archived == 0
    assert not engine.state.is_uploaded("42")


class ArchivingFakeReadwise(FakeReadwise):
    def __init__(self, docs):
        super().__init__(docs)
        self.reader_archived: list[str] = []
        self.archive_fails = False

    def archive_document(self, doc_id: str):
        if self.archive_fails:
            raise RuntimeError("Reader API down")
        self.reader_archived.append(doc_id)


def test_done_folder_doc_archives_in_reader_with_final_highlights(
    tmp_path, monkeypatch
):
    engine = SyncEngine(_engine_settings(tmp_path))
    doc = ReaderDocument(id="42", title="Finished Book")
    remarkable = FakeRemarkable()
    readwise = ArchivingFakeReadwise([doc])
    monkeypatch.setattr(engine, "_make_remarkable", lambda: remarkable)
    monkeypatch.setattr(engine, "_make_readwise", lambda: readwise)
    monkeypatch.setattr(reverse_mod, "extract_highlights", lambda _archive: [])

    engine.run_once()
    name = sanitize_name(doc.title)

    # User finishes the doc and drags it to Done; a last highlight exists.
    remarkable.move_to_done(name)
    monkeypatch.setattr(
        reverse_mod,
        "extract_highlights",
        lambda _archive: [RmHighlight(page_index=9, text="Final thought")],
    )

    result = engine.run_once()

    # The last highlight was pulled from the Done folder before archiving...
    assert any(p["text"] == "Final thought" for p in readwise.created)
    # ...the Reader doc is archived, and the device copy tidied to Archive.
    assert readwise.reader_archived == ["42"]
    assert result.finish.archived_in_reader == 1
    assert remarkable.folders["Readwise/Done"] == []
    assert name in remarkable.folders["Readwise/Archive"]


def test_done_folder_reader_failure_leaves_doc_for_retry(tmp_path, monkeypatch):
    engine = SyncEngine(_engine_settings(tmp_path))
    doc = ReaderDocument(id="42", title="Flaky Finish")
    remarkable = FakeRemarkable()
    readwise = ArchivingFakeReadwise([doc])
    monkeypatch.setattr(engine, "_make_remarkable", lambda: remarkable)
    monkeypatch.setattr(engine, "_make_readwise", lambda: readwise)
    monkeypatch.setattr(reverse_mod, "extract_highlights", lambda _archive: [])

    engine.run_once()
    name = sanitize_name(doc.title)
    remarkable.move_to_done(name)

    readwise.archive_fails = True
    result = engine.run_once()
    assert result.finish.failed == 1
    assert name in remarkable.folders["Readwise/Done"]  # stays for retry

    readwise.archive_fails = False
    result = engine.run_once()
    assert result.finish.archived_in_reader == 1
    assert readwise.reader_archived == ["42"]
    assert name in remarkable.folders["Readwise/Archive"]


def test_foreign_doc_in_done_is_left_alone(tmp_path, monkeypatch):
    engine = SyncEngine(_engine_settings(tmp_path))
    remarkable = FakeRemarkable()
    readwise = ArchivingFakeReadwise([])
    remarkable.folders["Readwise/Done"] = ["Handwritten Journal"]
    monkeypatch.setattr(engine, "_make_remarkable", lambda: remarkable)
    monkeypatch.setattr(engine, "_make_readwise", lambda: readwise)
    monkeypatch.setattr(reverse_mod, "extract_highlights", lambda _archive: [])

    result = engine.run_once()

    assert result.finish.unmatched == 1
    assert readwise.reader_archived == []
    assert remarkable.folders["Readwise/Done"] == ["Handwritten Journal"]


def test_finish_disabled_skips_done_folder(tmp_path, monkeypatch):
    engine = SyncEngine(_engine_settings(tmp_path, finish_to_reader=False))
    remarkable = FakeRemarkable()
    readwise = ArchivingFakeReadwise([])
    remarkable.folders["Readwise/Done"] = ["Whatever"]
    monkeypatch.setattr(engine, "_make_remarkable", lambda: remarkable)
    monkeypatch.setattr(engine, "_make_readwise", lambda: readwise)
    monkeypatch.setattr(reverse_mod, "extract_highlights", lambda _archive: [])

    result = engine.run_once()
    assert result.finish.considered == 0
