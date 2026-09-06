from pathlib import Path

from rem_readwise.sync.inbox import InboxSync
from rem_readwise.sync.state import SyncState


class FakeRemarkable:
    def __init__(self):
        self.uploaded: list[str] = []

    def ensure_folder(self, folder):
        pass

    def upload_pdf(self, local_pdf: Path, folder: str):
        self.uploaded.append(local_pdf.stem)


def _write_pdf(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"%PDF-1.7 fake")
    return path


def test_inbox_uploads_new_pdfs_and_dedupes(tmp_path):
    inbox = tmp_path / "inbox"
    _write_pdf(inbox, "some-paper.pdf")
    remarkable = FakeRemarkable()
    state = SyncState(tmp_path / "state.json")

    sync = InboxSync(remarkable, state, folder="Readwise", inbox_dir=inbox)
    result = sync.run()

    assert result.uploaded == 1
    assert remarkable.uploaded == ["some-paper"]
    assert state.is_uploaded("inbox:some-paper")
    assert state.reader_id_for_name("some-paper") == "inbox:some-paper"

    # Second run uploads nothing new.
    result2 = sync.run()
    assert result2.uploaded == 0
    assert result2.skipped_existing == 1
    assert remarkable.uploaded == ["some-paper"]


def test_inbox_documents_are_synthetic_reader_docs(tmp_path):
    inbox = tmp_path / "inbox"
    _write_pdf(inbox, "On Photography.pdf")
    sync = InboxSync(FakeRemarkable(), SyncState(tmp_path / "s.json"),
                     folder="Readwise", inbox_dir=inbox)
    docs = sync.documents()
    assert len(docs) == 1
    assert docs[0].id == "inbox:On Photography"
    assert docs[0].title == "On Photography"
    assert docs[0].category == "pdf"


def test_inbox_renames_unsafe_filenames_to_match_state(tmp_path):
    inbox = tmp_path / "inbox"
    _write_pdf(inbox, "Weird:Name?.pdf")  # sanitizes to "Weird Name"
    remarkable = FakeRemarkable()
    state = SyncState(tmp_path / "state.json")

    InboxSync(remarkable, state, folder="Readwise", inbox_dir=inbox).run()

    # On-device name matches the sanitized name stored in state.
    assert remarkable.uploaded == ["Weird Name"]
    assert state.is_uploaded("inbox:Weird Name")


def test_missing_inbox_dir_is_noop(tmp_path):
    sync = InboxSync(FakeRemarkable(), SyncState(tmp_path / "s.json"),
                     folder="Readwise", inbox_dir=tmp_path / "does-not-exist")
    result = sync.run()
    assert result.considered == 0
    assert result.uploaded == 0


def test_inbox_file_sharing_a_title_with_a_reader_doc_gets_a_unique_device_name(tmp_path):
    inbox = tmp_path / "inbox"
    _write_pdf(inbox, "Notes.pdf")
    remarkable = FakeRemarkable()
    state = SyncState(tmp_path / "state.json")
    state.mark_uploaded("12345", "Notes")  # a Reader doc already on the device

    result = InboxSync(remarkable, state, folder="Readwise", inbox_dir=inbox).run()

    assert result.uploaded == 1
    assert remarkable.uploaded == ["Notes (2)"]
    assert state.remarkable_name_for("inbox:Notes") == "Notes (2)"
    assert state.reader_id_for_name("Notes") == "12345"
    assert state.reader_id_for_name("Notes (2)") == "inbox:Notes"


def test_two_inbox_files_with_the_same_sanitized_title_are_not_merged(tmp_path, caplog):
    inbox = tmp_path / "inbox"
    _write_pdf(inbox, "A:B.pdf")
    _write_pdf(inbox, "A B.pdf")  # both sanitize to "A B"
    remarkable = FakeRemarkable()
    state = SyncState(tmp_path / "state.json")

    result = InboxSync(remarkable, state, folder="Readwise", inbox_dir=inbox).run()

    assert result.uploaded == 1
    assert result.failed == 1
    assert remarkable.uploaded == ["A B"]
    assert any("rename one of them" in r.getMessage() for r in caplog.records)
