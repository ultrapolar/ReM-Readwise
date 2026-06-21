import datetime as dt

from rem_readwise.models import ReaderDocument, RmHighlight
from rem_readwise.readwise import build_highlight_payloads


def _doc():
    return ReaderDocument(
        id="42",
        title="On Photography",
        author="Susan Sontag",
        source_url="https://example.com/photo.pdf",
    )


def test_payload_carries_page_location_and_anchoring_fields():
    when = dt.datetime(2026, 6, 21, 12, 0, tzinfo=dt.UTC)
    payloads = build_highlight_payloads(
        _doc(),
        [RmHighlight(page_index=4, text="In the beginning", color="yellow")],
        highlighted_at=when,
    )
    assert len(payloads) == 1
    p = payloads[0]
    assert p["text"] == "In the beginning"
    assert p["title"] == "On Photography"
    assert p["author"] == "Susan Sontag"
    assert p["source_url"] == "https://example.com/photo.pdf"
    assert p["category"] == "pdf"
    assert p["location"] == 5  # 1-based page number
    assert p["location_type"] == "page"
    assert p["highlighted_at"] == when.isoformat()
    assert "note" not in p  # default yellow -> no color tag


def test_non_yellow_color_becomes_note_tag():
    payloads = build_highlight_payloads(
        _doc(), [RmHighlight(page_index=0, text="hi", color="green")]
    )
    assert payloads[0]["note"] == ".green"


def test_empty_text_is_dropped_and_long_text_truncated():
    long_text = "x" * 9000
    payloads = build_highlight_payloads(
        _doc(),
        [
            RmHighlight(page_index=0, text="   "),
            RmHighlight(page_index=1, text=long_text),
        ],
    )
    assert len(payloads) == 1
    assert len(payloads[0]["text"]) == 8000


def test_missing_source_url_omits_field():
    doc = ReaderDocument(id="1", title="t")
    payloads = build_highlight_payloads(doc, [RmHighlight(page_index=0, text="hi")])
    assert "source_url" not in payloads[0]
    assert "author" not in payloads[0]
