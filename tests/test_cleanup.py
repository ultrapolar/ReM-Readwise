from rem_readwise.sync.cleanup import CleanupSync
from rem_readwise.sync.state import SyncState


class FakeRemarkable:
    def __init__(self, fail_names: set[str] | None = None):
        self.moved: list[tuple[str, str]] = []
        self._fail_names = fail_names or set()

    def move(self, remote_path: str, dest_folder: str):
        name = remote_path.rsplit("/", 1)[-1]
        if name in self._fail_names:
            raise RuntimeError("cloud hiccup")
        self.moved.append((remote_path, dest_folder))


def make_cleanup(remarkable, state, **kwargs):
    return CleanupSync(
        remarkable,
        state,
        folder="Readwise",
        archive_folder="Readwise/Archive",
        **kwargs,
    )


def test_archives_tracked_doc_gone_from_reader(tmp_path):
    state = SyncState(tmp_path / "state.json")
    state.mark_uploaded("42", "Old Paper")
    remarkable = FakeRemarkable()

    result = make_cleanup(remarkable, state).run(
        active_reader_ids=set(), device_names={"Old Paper"}, skip_names=set()
    )

    assert result.archived == 1
    assert remarkable.moved == [("Readwise/Old Paper", "Readwise/Archive")]
    assert state.is_uploaded("42")  # state survives until the next prune


def test_keeps_docs_still_in_reader_and_inbox_docs(tmp_path):
    state = SyncState(tmp_path / "state.json")
    state.mark_uploaded("42", "Still Reading")
    state.mark_uploaded("inbox:notes", "notes")
    remarkable = FakeRemarkable()

    result = make_cleanup(remarkable, state).run(
        active_reader_ids={"42", "inbox:notes"},
        device_names={"Still Reading", "notes"},
        skip_names=set(),
    )

    assert result.considered == 0
    assert remarkable.moved == []


def test_never_touches_foreign_docs(tmp_path):
    # A doc in the folder that we never uploaded has no state entry, so the
    # state-driven iteration cannot even see it.
    state = SyncState(tmp_path / "state.json")
    remarkable = FakeRemarkable()

    result = make_cleanup(remarkable, state).run(
        active_reader_ids=set(), device_names={"Handwritten Journal"}, skip_names=set()
    )

    assert result.considered == 0
    assert remarkable.moved == []


def test_skips_docs_whose_reverse_pass_failed(tmp_path):
    state = SyncState(tmp_path / "state.json")
    state.mark_uploaded("42", "Flaky Doc")
    remarkable = FakeRemarkable()

    result = make_cleanup(remarkable, state).run(
        active_reader_ids=set(), device_names={"Flaky Doc"}, skip_names={"Flaky Doc"}
    )

    assert result.skipped_failed == 1
    assert remarkable.moved == []


def test_already_absent_docs_are_left_to_prune(tmp_path):
    state = SyncState(tmp_path / "state.json")
    state.mark_uploaded("42", "Manually Removed")
    remarkable = FakeRemarkable()

    result = make_cleanup(remarkable, state).run(
        active_reader_ids=set(), device_names=set(), skip_names=set()
    )

    assert result.considered == 0
    assert remarkable.moved == []


def test_dry_run_moves_nothing(tmp_path):
    state = SyncState(tmp_path / "state.json")
    state.mark_uploaded("42", "Old Paper")
    remarkable = FakeRemarkable()

    result = make_cleanup(remarkable, state, dry_run=True).run(
        active_reader_ids=set(), device_names={"Old Paper"}, skip_names=set()
    )

    assert result.archived == 1  # reported, not performed
    assert remarkable.moved == []


def test_move_failure_is_contained(tmp_path):
    state = SyncState(tmp_path / "state.json")
    state.mark_uploaded("1", "Bad")
    state.mark_uploaded("2", "Good")
    remarkable = FakeRemarkable(fail_names={"Bad"})

    result = make_cleanup(remarkable, state).run(
        active_reader_ids=set(), device_names={"Bad", "Good"}, skip_names=set()
    )

    assert result.failed == 1
    assert result.archived == 1
    assert ("Readwise/Good", "Readwise/Archive") in remarkable.moved
