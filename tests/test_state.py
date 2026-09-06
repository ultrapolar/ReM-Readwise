from rem_readwise.sync.state import SyncState


def test_upload_tracking_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    state = SyncState(path)
    assert not state.is_uploaded("doc1")

    state.mark_uploaded("doc1", "My Paper")
    assert state.is_uploaded("doc1")
    assert state.remarkable_name_for("doc1") == "My Paper"
    assert state.reader_id_for_name("My Paper") == "doc1"
    assert state.reader_id_for_name("Unknown") is None

    state.save()

    # Reload from disk and confirm persistence.
    reloaded = SyncState(path)
    assert reloaded.is_uploaded("doc1")
    assert reloaded.reader_id_for_name("My Paper") == "doc1"


def test_pushed_highlight_tracking(tmp_path):
    path = tmp_path / "state.json"
    state = SyncState(path)
    assert not state.is_pushed("abc")
    state.mark_pushed("abc")
    assert state.is_pushed("abc")
    state.save()

    assert SyncState(path).is_pushed("abc")


def test_corrupt_state_starts_fresh(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json{", encoding="utf-8")
    state = SyncState(path)  # should not raise
    assert state.uploaded_count == 0
    assert state.pushed_count == 0


def test_unique_name_disambiguates_between_different_documents(tmp_path):
    state = SyncState(tmp_path / "state.json")
    assert state.unique_remarkable_name("Notes", "doc1") == "Notes"
    state.mark_uploaded("doc1", "Notes")

    # A different Reader doc with the same sanitized title gets a suffix...
    assert state.unique_remarkable_name("Notes", "doc2") == "Notes (2)"
    state.mark_uploaded("doc2", "Notes (2)")
    assert state.unique_remarkable_name("Notes", "doc3") == "Notes (3)"

    # ...while the owner keeps its own name (re-runs stay idempotent).
    assert state.unique_remarkable_name("Notes", "doc1") == "Notes"
    assert state.unique_remarkable_name("Notes (2)", "doc2") == "Notes (2)"


def test_unique_name_does_not_stack_suffixes(tmp_path):
    state = SyncState(tmp_path / "state.json")
    state.mark_uploaded("a", "Paper (2)")
    assert state.unique_remarkable_name("Paper (2)", "b") == "Paper (3)"


def test_ambiguous_legacy_mapping_is_refused_not_guessed(tmp_path, caplog):
    """An older state file may hold two docs under one name; never pick the first."""
    path = tmp_path / "state.json"
    path.write_text(
        '{"documents": {"doc1": {"remarkable_name": "Notes"}, '
        '"doc2": {"remarkable_name": "Notes"}}, "pushed_highlights": []}',
        encoding="utf-8",
    )
    state = SyncState(path)
    assert any("2 Reader documents" in r.getMessage() for r in caplog.records)

    assert state.reader_id_for_name("Notes") is None
    assert state.is_name_taken("Notes")
    assert state.is_name_taken("Notes", by="doc1")  # doc2 still owns it too
