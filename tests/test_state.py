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
