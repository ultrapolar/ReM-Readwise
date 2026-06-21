from rem_readwise.models import ReaderDocument, RmHighlight, normalize_text


def test_normalize_text_collapses_whitespace_and_casing():
    assert normalize_text("  Hello   WORLD\n") == "hello world"


def test_page_number_is_one_based():
    assert RmHighlight(page_index=0, text="x").page_number == 1
    assert RmHighlight(page_index=4, text="x").page_number == 5


def test_dedup_key_is_stable_across_whitespace_and_color():
    a = RmHighlight(page_index=2, text="The  quick\nbrown fox", color="yellow")
    b = RmHighlight(page_index=2, text="the quick brown fox", color="green", order=3)
    assert a.dedup_key("doc1") == b.dedup_key("doc1")


def test_dedup_key_differs_by_page_and_doc():
    base = RmHighlight(page_index=2, text="same text")
    other_page = RmHighlight(page_index=3, text="same text")
    assert base.dedup_key("doc1") != other_page.dedup_key("doc1")
    assert base.dedup_key("doc1") != base.dedup_key("doc2")


def test_best_source_prefers_source_url():
    doc = ReaderDocument(id="1", title="t", source_url="s", url="u")
    assert doc.best_source == "s"
    assert ReaderDocument(id="1", title="t", url="u").best_source == "u"
    assert ReaderDocument(id="1", title="t").best_source is None
