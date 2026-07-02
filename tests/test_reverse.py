"""ReverseSync edge cases: skips, dry-run, and per-document error recovery."""

from __future__ import annotations

from pathlib import Path

from rem_readwise.models import ReaderDocument, RmHighlight
from rem_readwise.remarkable.client import RemarkableEntry
from rem_readwise.sync import reverse as reverse_mod
from rem_readwise.sync.reverse import ReverseSync
from rem_readwise.sync.state import SyncState
from tests.conftest import FakeReadwise, FakeRemarkable


class ListingRemarkable(FakeRemarkable):
    """FakeRemarkable whose folder listing is set explicitly."""

    def __init__(self, entries: list[RemarkableEntry]):
        super().__init__()
        self.entries = entries

    def list_folder(self, folder):
        return self.entries


def make_reverse(tmp_path, readwise, remarkable, **kwargs):
    state = SyncState(tmp_path / "state.json")
    sync = ReverseSync(
        readwise, remarkable, state, folder="Readwise", work_dir=tmp_path / "work", **kwargs
    )
    return sync, state


def test_directories_and_unmatched_docs_are_skipped(tmp_path):
    remarkable = ListingRemarkable(
        [
            RemarkableEntry(name="Books", is_dir=True),
            RemarkableEntry(name="Mystery Doc", is_dir=False),  # nothing in state
        ]
    )
    reverse, _state = make_reverse(tmp_path, FakeReadwise([]), remarkable)

    result = reverse.run()

    assert result.documents_scanned == 0
    assert result.documents_unmatched == 1
    assert result.failed == 0


def test_one_failing_document_does_not_stop_the_rest(tmp_path, monkeypatch):
    class ExplodingDownload(ListingRemarkable):
        def download(self, remote_path: str, dest_dir: Path) -> Path:
            if "Doc A" in remote_path:
                raise RuntimeError("cloud hiccup")
            return super().download(remote_path, dest_dir)

    remarkable = ExplodingDownload(
        [
            RemarkableEntry(name="Doc A", is_dir=False),
            RemarkableEntry(name="Doc B", is_dir=False),
        ]
    )
    readwise = FakeReadwise([])
    reverse, state = make_reverse(tmp_path, readwise, remarkable)
    state.mark_uploaded("id-a", "Doc A")
    state.mark_uploaded("id-b", "Doc B")
    monkeypatch.setattr(
        reverse_mod,
        "extract_highlights",
        lambda _archive: [RmHighlight(page_index=0, text="from the survivor")],
    )

    result = reverse.run([ReaderDocument(id="id-b", title="Doc B")])

    assert result.documents_scanned == 2
    assert result.failed == 1
    # Doc B was still processed and its highlight pushed.
    assert result.highlights_pushed == 1
    assert [p["title"] for p in readwise.created] == ["Doc B"]


def test_dry_run_counts_pushes_without_calling_readwise(tmp_path, monkeypatch):
    remarkable = ListingRemarkable([RemarkableEntry(name="Doc", is_dir=False)])
    readwise = FakeReadwise([])
    reverse, state = make_reverse(tmp_path, readwise, remarkable, dry_run=True)
    state.mark_uploaded("id-1", "Doc")
    monkeypatch.setattr(
        reverse_mod,
        "extract_highlights",
        lambda _archive: [
            RmHighlight(page_index=0, text="one"),
            RmHighlight(page_index=1, text="two"),
        ],
    )

    result = reverse.run()

    assert result.highlights_pushed == 2
    assert readwise.created == []
    # Nothing marked as pushed, so a real run still sends everything.
    assert state.pushed_count == 0
