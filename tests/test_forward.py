"""ForwardSync edge cases: skips, dry-run, and per-document error recovery."""

from __future__ import annotations

from pathlib import Path

from rem_readwise.models import ReaderDocument
from rem_readwise.sync.forward import ForwardSync
from rem_readwise.sync.state import SyncState
from tests.conftest import FakeReadwise, FakeRemarkable


def make_forward(tmp_path, readwise, remarkable, **kwargs):
    state = SyncState(tmp_path / "state.json")
    sync = ForwardSync(
        readwise, remarkable, state, folder="Readwise", work_dir=tmp_path / "work", **kwargs
    )
    return sync, state


def test_fetches_library_itself_when_no_documents_given(tmp_path):
    readwise = FakeReadwise([ReaderDocument(id="1", title="Doc")])
    forward, _state = make_forward(tmp_path, readwise, FakeRemarkable())

    result = forward.run()  # no pre-fetched documents

    assert readwise.list_calls == 1
    assert result.uploaded == 1


def test_skips_documents_already_uploaded(tmp_path):
    doc = ReaderDocument(id="1", title="Doc")
    remarkable = FakeRemarkable()
    forward, state = make_forward(tmp_path, FakeReadwise([doc]), remarkable)
    state.mark_uploaded("1", "Doc")

    result = forward.run([doc])

    assert result.skipped_existing == 1
    assert result.uploaded == 0
    assert remarkable.uploaded == []


def test_dry_run_counts_but_touches_nothing(tmp_path):
    doc = ReaderDocument(id="1", title="Doc")
    remarkable = FakeRemarkable()
    forward, state = make_forward(
        tmp_path, FakeReadwise([doc]), remarkable, dry_run=True
    )

    result = forward.run([doc])

    assert result.uploaded == 1
    assert remarkable.uploaded == []
    assert not state.is_uploaded("1")


def test_upload_failure_is_counted_and_does_not_stop_the_run(tmp_path):
    class ExplodingRemarkable(FakeRemarkable):
        def upload_pdf(self, local_pdf: Path, folder: str):
            if local_pdf.stem == "Bad Doc":
                raise RuntimeError("device offline")
            super().upload_pdf(local_pdf, folder)

    docs = [
        ReaderDocument(id="bad", title="Bad Doc"),
        ReaderDocument(id="good", title="Good Doc"),
    ]
    remarkable = ExplodingRemarkable()
    forward, state = make_forward(tmp_path, FakeReadwise(docs), remarkable)

    result = forward.run(docs)

    assert result.failed == 1
    assert result.uploaded == 1
    assert remarkable.uploaded == ["Good Doc"]
    # The failed doc stays unmarked so the next cycle retries it.
    assert not state.is_uploaded("bad")
    assert state.is_uploaded("good")
