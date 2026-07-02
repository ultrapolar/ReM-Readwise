import json
import zipfile

from rem_readwise.remarkable import highlights as hl
from tests.conftest import FakeColor, FakeGlyphRange, FakeRect


# ── page-order parsing ─────────────────────────────────────────────────────
def test_parse_modern_cpages_with_redir():
    content = {
        "fileType": "pdf",
        "cPages": {
            "pages": [
                {"id": "p0", "redir": {"value": 0}},
                {"id": "p1", "redir": {"value": 1}},
            ]
        },
    }
    assert hl.parse_content_page_order(content) == [("p0", 0), ("p1", 1)]


def test_parse_cpages_skips_deleted_and_uses_redir_over_position():
    content = {
        "cPages": {
            "pages": [
                {"id": "p0", "redir": {"value": 5}},
                {"id": "p1", "deleted": {"value": 1}},
                {"id": "p2", "redir": {"value": 6}},
            ]
        }
    }
    # Deleted page is dropped; redir values win over list position.
    assert hl.parse_content_page_order(content) == [("p0", 5), ("p2", 6)]


def test_parse_cpages_without_redir_falls_back_to_position():
    content = {"cPages": {"pages": [{"id": "p0"}, {"id": "p1"}]}}
    assert hl.parse_content_page_order(content) == [("p0", 0), ("p1", 1)]


def test_parse_legacy_pages_list():
    content = {"pages": ["a", "b", "c"]}
    assert hl.parse_content_page_order(content) == [("a", 0), ("b", 1), ("c", 2)]


def test_parse_cpages_ignores_malformed_entries():
    content = {
        "cPages": {
            "pages": [
                "not-a-dict",
                {"redir": {"value": 0}},  # no id
                {"id": "p1"},
            ]
        }
    }
    # Skipped entries must not advance the positional fallback index.
    assert hl.parse_content_page_order(content) == [("p1", 0)]


# ── per-page highlight extraction ──────────────────────────────────────────
def test_highlights_for_page_orders_and_maps(monkeypatch):
    glyphs = [
        FakeGlyphRange("second", FakeColor("YELLOW"), [FakeRect(x=10, y=200)]),
        FakeGlyphRange("first", FakeColor("GREEN"), [FakeRect(x=10, y=50)]),
        FakeGlyphRange("   ", FakeColor("YELLOW"), [FakeRect(x=0, y=0)]),  # empty -> skip
    ]
    monkeypatch.setattr(hl, "extract_glyph_ranges", lambda _b: glyphs)

    out = hl.highlights_for_page(b"ignored", pdf_page_index=3)
    assert [h.text for h in out] == ["first", "second"]  # sorted top-to-bottom
    assert out[0].color == "green"
    assert all(h.page_index == 3 for h in out)
    assert [h.order for h in out] == [0, 1]


def test_highlights_for_page_survives_parse_error(monkeypatch):
    def boom(_b):
        raise ValueError("bad rm")

    monkeypatch.setattr(hl, "extract_glyph_ranges", boom)
    assert hl.highlights_for_page(b"x", 0) == []


def test_color_name_falls_back_for_odd_color_values(monkeypatch):
    glyphs = [
        FakeGlyphRange("named", FakeColor("GREEN"), [FakeRect(y=0)]),
        FakeGlyphRange("stringy", "RED", [FakeRect(y=1)]),  # no .name attribute
        FakeGlyphRange("colorless", None, [FakeRect(y=2)]),
    ]
    monkeypatch.setattr(hl, "extract_glyph_ranges", lambda _b: glyphs)

    out = hl.highlights_for_page(b"ignored", 0)
    assert [(h.text, h.color) for h in out] == [
        ("named", "green"),
        ("stringy", "red"),  # str(color).lower()
        ("colorless", "yellow"),  # default
    ]


# ── real .rm parsing via rmscene ───────────────────────────────────────────
def _real_rm_bytes(text: str) -> bytes:
    """Write a genuine v6 .rm page containing one GlyphRange highlight."""
    import io

    from rmscene import CrdtId, SceneGlyphItemBlock, write_blocks
    from rmscene import scene_items as si
    from rmscene.crdt_sequence import CrdtSequenceItem

    glyph = si.GlyphRange(
        start=0,
        length=len(text),
        text=text,
        color=si.PenColor.YELLOW,
        rectangles=[si.Rectangle(x=1.0, y=2.0, w=10.0, h=3.0)],
    )
    item = CrdtSequenceItem(
        item_id=CrdtId(1, 2),
        left_id=CrdtId(0, 0),
        right_id=CrdtId(0, 0),
        deleted_length=0,
        value=glyph,
    )
    buf = io.BytesIO()
    write_blocks(buf, [SceneGlyphItemBlock(parent_id=CrdtId(0, 1), item=item)])
    return buf.getvalue()


def test_extract_glyph_ranges_reads_real_rm_bytes():
    ranges = hl.extract_glyph_ranges(_real_rm_bytes("hello device"))
    assert len(ranges) == 1
    assert ranges[0].text == "hello device"


def test_highlights_for_page_end_to_end_on_real_rm_bytes():
    out = hl.highlights_for_page(_real_rm_bytes("hello device"), pdf_page_index=4)
    assert [(h.text, h.color, h.page_index) for h in out] == [("hello device", "yellow", 4)]


# ── full archive extraction ────────────────────────────────────────────────
def _make_archive(tmp_path):
    content = {
        "fileType": "pdf",
        "cPages": {"pages": [{"id": "pageA", "redir": {"value": 0}},
                             {"id": "pageB", "redir": {"value": 1}}]},
    }
    archive = tmp_path / "doc.rmdoc"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("docUUID.content", json.dumps(content))
        zf.writestr("docUUID/pageA.rm", b"A")
        zf.writestr("docUUID/pageB.rm", b"B")
    return archive


def test_extract_highlights_walks_archive(tmp_path, monkeypatch):
    archive = _make_archive(tmp_path)

    def fake_extract(rm_bytes):
        if rm_bytes == b"A":
            return [FakeGlyphRange("alpha", FakeColor("YELLOW"), [FakeRect(y=1)])]
        return [FakeGlyphRange("beta", FakeColor("PINK"), [FakeRect(y=1)])]

    monkeypatch.setattr(hl, "extract_glyph_ranges", fake_extract)

    out = hl.extract_highlights(archive)
    assert [(h.text, h.page_index) for h in out] == [("alpha", 0), ("beta", 1)]


def test_extract_highlights_no_content_returns_empty(tmp_path):
    archive = tmp_path / "empty.rmdoc"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("note.txt", "nothing here")
    assert hl.extract_highlights(archive) == []


def test_extract_highlights_skips_pages_without_rm_files(tmp_path, monkeypatch):
    content = {
        "fileType": "pdf",
        "cPages": {"pages": [{"id": "pageA"}, {"id": "pageB"}]},  # pageB has no .rm
    }
    archive = tmp_path / "doc.rmdoc"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("docUUID.content", json.dumps(content))
        zf.writestr("docUUID/pageA.rm", b"A")

    monkeypatch.setattr(
        hl,
        "extract_glyph_ranges",
        lambda _b: [FakeGlyphRange("alpha", FakeColor("YELLOW"), [FakeRect(y=1)])],
    )

    out = hl.extract_highlights(archive)
    assert [(h.text, h.page_index) for h in out] == [("alpha", 0)]


# ── .rm file resolution ────────────────────────────────────────────────────
def test_resolve_rm_prefers_exact_paths_then_suffix_fallback():
    lookup = {name: name for name in ["docUUID/pageA.rm", "weird/nested/pageB.rm"]}
    assert hl._resolve_rm(lookup, "docUUID", "pageA") == "docUUID/pageA.rm"
    # No exact candidate for pageB; any path ending in its filename matches.
    assert hl._resolve_rm(lookup, "docUUID", "pageB") == "weird/nested/pageB.rm"
    assert hl._resolve_rm(lookup, "docUUID", "pageC") is None
