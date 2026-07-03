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


def test_parse_stat_id_go_struct_format():
    from rem_readwise.remarkable.client import _parse_stat_id

    out = (
        "&{ID:4f1a2b3c-9d8e-4a5b-b6c7-0123456789ab Version:2 Message: Success:true "
        "VissibleName:My Paper Type:DocumentType Parent:abc}"
    )
    assert _parse_stat_id(out) == "4f1a2b3c-9d8e-4a5b-b6c7-0123456789ab"


def test_parse_stat_id_json_format():
    from rem_readwise.remarkable.client import _parse_stat_id

    assert (
        _parse_stat_id('{"ID": "4f1a2b3c-9d8e", "VissibleName": "My Paper"}')
        == "4f1a2b3c-9d8e"
    )


def test_parse_stat_id_garbage_returns_none():
    from rem_readwise.remarkable.client import _parse_stat_id

    assert _parse_stat_id("no ids here") is None
    assert _parse_stat_id("") is None
