import json

import httpx
import pytest
import respx

from rem_readwise.models import ReaderDocument
from rem_readwise.readwise.client import ReadwiseClient, ReadwiseDownloadError


@respx.mock
def test_list_documents_follows_pagination():
    respx.get("https://readwise.io/api/v3/list/").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "results": [
                        {"id": "1", "title": "A", "category": "pdf",
                         "source_url": "https://x/a.pdf"}
                    ],
                    "nextPageCursor": "cur2",
                },
            ),
            httpx.Response(
                200,
                json={
                    "results": [{"id": "2", "title": "B", "category": "pdf"}],
                    "nextPageCursor": None,
                },
            ),
        ]
    )
    with ReadwiseClient("tok") as client:
        docs = list(client.list_documents(category="pdf"))
    assert [d.id for d in docs] == ["1", "2"]
    assert docs[0].source_url == "https://x/a.pdf"


@respx.mock
def test_create_highlights_batches_requests():
    captured: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(len(body["highlights"]))
        return httpx.Response(200, json=[])

    respx.post("https://readwise.io/api/v2/highlights/").mock(side_effect=handler)

    with ReadwiseClient("tok") as client:
        payloads = [{"text": f"t{i}", "title": "x"} for i in range(150)]
        client.create_highlights(payloads)

    assert captured == [100, 50]


@respx.mock
def test_download_document_streams_to_disk(tmp_path):
    respx.get("https://example.com/a.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.7 body")
    )
    doc = ReaderDocument(id="1", title="A", source_url="https://example.com/a.pdf")
    dest = tmp_path / "out" / "a.pdf"
    with ReadwiseClient("tok") as client:
        client.download_document(doc, dest)
    assert dest.read_bytes() == b"%PDF-1.7 body"


@respx.mock
def test_download_rejects_non_pdf_for_pdf_category(tmp_path):
    # Simulates an uploaded PDF whose source_url serves an HTML page instead.
    respx.get("https://example.com/not.pdf").mock(
        return_value=httpx.Response(
            200, headers={"Content-Type": "text/html"}, content=b"<html>nope</html>"
        )
    )
    doc = ReaderDocument(
        id="1", title="Bad", category="pdf", source_url="https://example.com/not.pdf"
    )
    dest = tmp_path / "bad.pdf"
    with ReadwiseClient("tok") as client:
        with pytest.raises(ReadwiseDownloadError):
            client.download_document(doc, dest)
    assert not dest.exists()  # junk is cleaned up, never handed to the device


def test_download_without_source_raises(tmp_path):
    doc = ReaderDocument(id="1", title="No Source", category="pdf")
    with ReadwiseClient("tok") as client:
        with pytest.raises(ReadwiseDownloadError):
            client.download_document(doc, tmp_path / "x.pdf")


@respx.mock
def test_download_uses_auth_header_for_readwise_assets(tmp_path):
    route = respx.get("https://readwise.io/asset/a.pdf").mock(
        return_value=httpx.Response(200, content=b"%PDF-1.7")
    )
    doc = ReaderDocument(id="1", title="A", source_url="https://readwise.io/asset/a.pdf")
    with ReadwiseClient("tok") as client:
        client.download_document(doc, tmp_path / "a.pdf")
    assert route.calls.last.request.headers["Authorization"] == "Token tok"
