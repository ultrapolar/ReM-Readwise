from rem_readwise.remarkable.client import _abs, _parse_ls


def test_abs_normalizes_paths():
    assert _abs("Readwise") == "/Readwise"
    assert _abs("/Readwise") == "/Readwise"
    assert _abs("  Readwise/Sub ") == "/Readwise/Sub"
    assert _abs("") == "/"
    assert _abs("/") == "/"


def test_parse_ls_handles_prefixed_entries():
    out = "[d] Books\n[f] My Paper\n[f] Another Doc\n"
    entries = _parse_ls(out)
    assert [(e.name, e.is_dir) for e in entries] == [
        ("Books", True),
        ("My Paper", False),
        ("Another Doc", False),
    ]


def test_parse_ls_handles_trailing_slash_dirs_and_blanks():
    out = "Notebooks/\nplain file\n\n"
    entries = _parse_ls(out)
    assert ("Notebooks", True) in [(e.name, e.is_dir) for e in entries]
    assert ("plain file", False) in [(e.name, e.is_dir) for e in entries]
