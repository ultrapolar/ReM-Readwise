"""SyncEngine orchestration tests: wiring between the three passes."""

from __future__ import annotations

import pytest

from rem_readwise.config import Settings
from rem_readwise.models import ReaderDocument
from rem_readwise.sync import engine as engine_mod
from rem_readwise.sync import reverse as reverse_mod
from rem_readwise.sync.engine import SyncEngine
from rem_readwise.sync.state import SyncState
from tests.conftest import FakeReadwise, FakeRemarkable


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "readwise_token": "test-token",
        "state_path": str(tmp_path / "state.json"),
        "work_dir": str(tmp_path / "work"),
        "inbox_dir": str(tmp_path / "inbox"),
        "rmapi_config": str(tmp_path / "rmapi.conf"),
        **overrides,
    }
    return Settings(_env_file=None, **values)


def make_engine(tmp_path, monkeypatch, readwise, remarkable, **overrides) -> SyncEngine:
    engine = SyncEngine(make_settings(tmp_path, **overrides))
    monkeypatch.setattr(engine, "_make_readwise", lambda: readwise)
    monkeypatch.setattr(engine, "_make_remarkable", lambda: remarkable)
    return engine


def test_run_once_refuses_unpaired_remarkable(tmp_path, monkeypatch):
    remarkable = FakeRemarkable()
    monkeypatch.setattr(remarkable, "is_authenticated", lambda: False)
    engine = make_engine(tmp_path, monkeypatch, FakeReadwise([]), remarkable)
    with pytest.raises(RuntimeError, match="not paired"):
        engine.run_once()


def test_run_once_fetches_library_once_and_persists_state(tmp_path, monkeypatch):
    doc = ReaderDocument(id="42", title="On Photography")
    readwise = FakeReadwise([doc])
    remarkable = FakeRemarkable()
    monkeypatch.setattr(reverse_mod, "extract_highlights", lambda _archive: [])
    engine = make_engine(tmp_path, monkeypatch, readwise, remarkable)

    result = engine.run_once()

    assert result.forward.uploaded == 1
    assert remarkable.uploaded == ["On Photography"]
    # The Reader library must be listed exactly once and shared by both passes.
    assert readwise.list_calls == 1
    # The uploaded doc is visible to the reverse pass in the same cycle.
    assert result.reverse.documents_scanned == 1
    # State was saved to disk: a fresh cycle skips the already-uploaded doc.
    reloaded = SyncState(engine.state._path)
    assert reloaded.is_uploaded("42")


def test_run_once_feeds_inbox_documents_to_reverse_pass(tmp_path, monkeypatch):
    doc = ReaderDocument(id="42", title="On Photography")
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    (inbox_dir / "Local Paper.pdf").write_bytes(b"%PDF-fake")

    monkeypatch.setattr(reverse_mod, "extract_highlights", lambda _archive: [])
    seen: dict = {}
    original_run = reverse_mod.ReverseSync.run

    def spying_run(self, documents=None):
        seen["documents"] = documents
        return original_run(self, documents)

    monkeypatch.setattr(reverse_mod.ReverseSync, "run", spying_run)

    engine = make_engine(tmp_path, monkeypatch, FakeReadwise([doc]), FakeRemarkable())
    result = engine.run_once()

    assert result.inbox.uploaded == 1
    # Reverse must see the Reader library plus the synthetic inbox documents,
    # so highlights on inbox PDFs round-trip like any other doc.
    assert {d.id for d in seen["documents"]} == {"42", "inbox:Local Paper"}
    assert result.reverse.documents_scanned == 2


def test_run_forever_survives_a_failing_cycle(tmp_path, monkeypatch):
    class StopLoop(Exception):
        pass

    engine = make_engine(
        tmp_path,
        monkeypatch,
        FakeReadwise([]),
        FakeRemarkable(),
        sync_interval_seconds=5,  # below the floor; must be clamped to 60
    )

    def failing_run_once():
        raise RuntimeError("cycle blew up")

    monkeypatch.setattr(engine, "run_once", failing_run_once)

    sleeps: list[int] = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise StopLoop

    monkeypatch.setattr(engine_mod.time, "sleep", fake_sleep)

    # StopLoop (not RuntimeError) proves the failed cycle was swallowed and
    # the loop went around again.
    with pytest.raises(StopLoop):
        engine.run_forever()
    assert sleeps == [60, 60]
