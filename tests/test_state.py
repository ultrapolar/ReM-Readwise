import json
from pathlib import Path

from rem_readwise.sync.state import SyncState


def backup(path: Path, n: int) -> Path:
    return path.with_name(f"{path.name}.{n}")


def docs_in(path: Path) -> set[str]:
    return set(json.loads(path.read_text("utf-8"))["documents"])


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
    assert not state.is_pushed("doc1", "abc")
    state.mark_pushed("doc1", "abc")
    assert state.is_pushed("doc1", "abc")
    state.save()

    assert SyncState(path).is_pushed("doc1", "abc")


def test_corrupt_state_starts_fresh(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("not json{", encoding="utf-8")
    state = SyncState(path)  # should not raise
    assert state.uploaded_count == 0
    assert state.pushed_count == 0


# ── rotating backups ────────────────────────────────────────────────────────


def test_save_rotates_one_backup_per_run_and_caps_them(tmp_path):
    path = tmp_path / "state.json"
    for i in range(5):  # five separate process runs, one save each
        state = SyncState(path, backups=3)
        state.mark_uploaded(f"doc{i}", f"Doc {i}")
        state.save()

    # .1 is the newest snapshot, .3 the oldest still kept; the cap holds.
    assert docs_in(path) == {"doc0", "doc1", "doc2", "doc3", "doc4"}
    assert docs_in(backup(path, 1)) == {"doc0", "doc1", "doc2", "doc3"}
    assert docs_in(backup(path, 2)) == {"doc0", "doc1", "doc2"}
    assert docs_in(backup(path, 3)) == {"doc0", "doc1"}
    assert not backup(path, 4).exists()
    assert not path.with_name(path.name + ".tmp").exists()  # atomic write intact


def test_backups_rotate_once_per_arming_not_per_save(tmp_path):
    path = tmp_path / "state.json"
    seed = SyncState(path)
    seed.mark_uploaded("original", "Original")
    seed.save()

    state = SyncState(path, backups=3)
    for i in range(3):  # several mid-cycle saves...
        state.mark_uploaded(f"doc{i}", f"Doc {i}")
        state.save()

    # ...snapshot the pre-existing file exactly once.
    assert docs_in(backup(path, 1)) == {"original"}
    assert not backup(path, 2).exists()

    state.arm_rotation()  # what the engine does at the start of each cycle
    state.mark_uploaded("late", "Late")
    state.save()
    assert docs_in(backup(path, 1)) == {"original", "doc0", "doc1", "doc2"}
    assert docs_in(backup(path, 2)) == {"original"}


def test_zero_backups_disables_rotation(tmp_path):
    path = tmp_path / "state.json"
    seed = SyncState(path)
    seed.mark_uploaded("doc1", "Doc 1")
    seed.save()

    state = SyncState(path, backups=0)
    state.mark_uploaded("doc2", "Doc 2")
    state.save()

    assert docs_in(path) == {"doc1", "doc2"}
    assert not backup(path, 1).exists()


# ── pruning ─────────────────────────────────────────────────────────────────


def test_prune_removes_docs_gone_from_both_sides_with_their_keys(tmp_path):
    state = SyncState(tmp_path / "state.json")
    state.mark_uploaded("gone", "Gone Doc")
    state.mark_uploaded("kept", "Kept Doc")
    state.mark_pushed("gone", "aaa")
    state.mark_pushed("gone", "bbb")
    state.mark_pushed("kept", "ccc")

    pruned = state.prune(active_reader_ids={"kept"}, device_names={"Kept Doc"})

    assert pruned == 1
    assert not state.is_uploaded("gone")
    assert not state.is_pushed("gone", "aaa")
    assert not state.is_pushed("gone", "bbb")
    assert state.is_uploaded("kept")
    assert state.is_pushed("kept", "ccc")
    assert state.pushed_count == 1


def test_prune_keeps_docs_still_present_on_either_side(tmp_path):
    state = SyncState(tmp_path / "state.json")
    state.mark_uploaded("in-reader", "Reader Only")  # deleted on device only
    state.mark_uploaded("on-device", "Device Only")  # deleted in Reader only
    state.mark_pushed("on-device", "abc")

    pruned = state.prune(active_reader_ids={"in-reader"}, device_names={"Device Only"})

    assert pruned == 0
    assert state.is_uploaded("in-reader")
    assert state.is_uploaded("on-device")
    assert state.is_pushed("on-device", "abc")


def test_prune_never_touches_legacy_unnamespaced_keys(tmp_path):
    path = tmp_path / "state.json"
    legacy_key = "0" * 40  # bare sha1 as written by pre-namespace versions
    path.write_text(
        json.dumps(
            {
                "documents": {"old": {"remarkable_name": "Old Doc"}},
                "pushed_highlights": [legacy_key],
            }
        ),
        encoding="utf-8",
    )
    state = SyncState(path)
    assert state.is_pushed("old", legacy_key)  # legacy keys still deduplicate

    assert state.prune(set(), set()) == 1  # the doc entry itself is pruned...
    assert state.pushed_count == 1  # ...but the unattributable key survives
