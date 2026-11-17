import io
import zipfile

import pytest
from pypdf import PdfWriter

from corpus.services.parsers import (
    EmptyDocumentError,
    UnsupportedFileTypeError,
    detect_format,
    parse_document,
    parse_document_with_stats,
)

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _docx(body: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>"
        )
    return buffer.getvalue()


def _blank_pdf(pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=595, height=842)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_docx_paragraphs_tables_and_page_breaks() -> None:
    data = _docx(
        "<w:p><w:r><w:t>Warranty</w:t></w:r>"
        "<w:r><w:t xml:space='preserve'> policy</w:t></w:r></w:p>"
        "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Cell A</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
        "<w:p><w:r><w:t>Before</w:t><w:br w:type='page'/><w:t>After</w:t></w:r></w:p>"
    )
    pages = parse_document(data, "policy.docx", DOCX)
    assert [p.number for p in pages] == [1, 2]
    assert pages[0].text == "Warranty policy\nCell A\nBefore"
    assert pages[1].text == "After"


def test_invalid_docx_is_rejected() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        parse_document(b"not a zip", "x.docx", DOCX)


def test_html_keeps_text_and_structure_drops_scripts() -> None:
    html = b"""<html><head><title>Help &amp; FAQ</title><style>p{}</style></head>
      <body><nav>Home</nav><h1>Warranty</h1><p>Two   years.</p><script>alert(1)</script>
      <ul><li>Body</li><li>Battery</li></ul></body></html>"""
    [page] = parse_document(html, "faq.html", "text/html")
    assert page.text.startswith("Help & FAQ\n\n")
    assert "Two years." in page.text and "Battery" in page.text
    assert "alert" not in page.text and "p{}" not in page.text


def test_scanned_pdf_pages_go_through_ocr() -> None:
    calls: list[int] = []

    def fake_ocr(pdf: bytes, index: int) -> str:
        calls.append(index)
        return f"Recognised text of scanned page {index + 1}."

    pages, ocr_pages = parse_document_with_stats(_blank_pdf(2), "scan.pdf", None, ocr=fake_ocr)
    assert calls == [0, 1] and ocr_pages == 2
    assert pages[1].text == "Recognised text of scanned page 2."


def test_scanned_pdf_without_ocr_explains_why_it_fails() -> None:
    with pytest.raises(EmptyDocumentError, match="enable OCR"):
        parse_document(_blank_pdf(), "scan.pdf", "application/pdf")


def test_ocr_output_that_is_not_better_is_ignored() -> None:
    with pytest.raises(EmptyDocumentError):
        parse_document(_blank_pdf(), "scan.pdf", None, ocr=lambda pdf, i: "   ")


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        ("a.pdf", None, "pdf"),
        ("a.bin", "application/pdf", "pdf"),
        ("a.docx", None, "docx"),
        ("a.HTM", None, "html"),
        ("a.md", None, "text"),
        ("a.png", "image/png", None),
    ],
)
def test_format_detection(filename: str, content_type: str | None, expected: str | None) -> None:
    assert detect_format(filename, content_type) == expected
