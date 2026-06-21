from rem_readwise.util import sanitize_name


def test_sanitize_removes_path_separators_and_collapses_space():
    assert sanitize_name("A/B:C*?  title") == "A B C title"


def test_sanitize_is_deterministic():
    title = 'Weird<>"|name'
    assert sanitize_name(title) == sanitize_name(title)


def test_sanitize_truncates_and_handles_empty():
    assert sanitize_name("x" * 500, max_length=10) == "x" * 10
    assert sanitize_name("   ...   ") == "Untitled"
