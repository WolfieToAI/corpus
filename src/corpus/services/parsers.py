"""Turn uploaded bytes into pages of text.

Supported: PDF (with OCR for scanned pages when an OCR engine is available), DOCX, HTML, plain
text and Markdown. DOCX and HTML are parsed with the standard library only (``zipfile``,
``xml.etree``, ``html.parser``): no heavy office-suite dependency in the image.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable
from html.parser import HTMLParser
from pathlib import PurePath
from xml.etree import ElementTree

from corpus.core.chunking import Page

PDF_TYPES = {"application/pdf"}
DOCX_TYPES = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
HTML_TYPES = {"text/html", "application/xhtml+xml"}
TEXT_TYPES = {"text/plain", "text/markdown", "text/x-markdown"}

PDF_SUFFIXES = {".pdf"}
DOCX_SUFFIXES = {".docx"}
HTML_SUFFIXES = {".html", ".htm", ".xhtml"}
TEXT_SUFFIXES = {".txt", ".md", ".markdown"}

SUPPORTED_TYPES = PDF_TYPES | DOCX_TYPES | HTML_TYPES | TEXT_TYPES
SUPPORTED_SUFFIXES = PDF_SUFFIXES | DOCX_SUFFIXES | HTML_SUFFIXES | TEXT_SUFFIXES

MIN_TEXT_CHARS = 20
"""A PDF page with less extractable text than this is treated as scanned (OCR candidate)."""

OcrFn = Callable[[bytes, int], str]
"""(pdf bytes, 0-based page index) -> recognised text."""


class UnsupportedFileTypeError(ValueError):
    pass


class EmptyDocumentError(ValueError):
    pass


# ----------------------------------------------------------------------------- PDF
def _parse_pdf(data: bytes, ocr: OcrFn | None) -> tuple[list[Page], int]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages: list[Page] = []
    ocr_pages = 0
    for index, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        if ocr is not None and len(text.strip()) < MIN_TEXT_CHARS:
            recognised = ocr(data, index)
            if len(recognised.strip()) > len(text.strip()):
                text = recognised
                ocr_pages += 1
        pages.append(Page(index + 1, text))
    return pages, ocr_pages


# ----------------------------------------------------------------------------- DOCX
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _parse_docx(data: bytes) -> list[Page]:
    """Paragraphs and table cells in document order; explicit page breaks start a new page."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise UnsupportedFileTypeError(f"not a valid DOCX file: {exc}") from exc

    pages: list[list[str]] = [[]]
    for paragraph in root.iter(f"{_W}p"):
        parts: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W}tab":
                parts.append("\t")
            elif node.tag == f"{_W}br" and node.get(f"{_W}type") == "page":
                pages[-1].append("".join(parts))
                parts = []
                pages.append([])
        pages[-1].append("".join(parts))
    return [Page(i, "\n".join(lines).strip()) for i, lines in enumerate(pages, start=1)]


# ----------------------------------------------------------------------------- HTML
class _TextExtractor(HTMLParser):
    SKIP = frozenset({"script", "style", "noscript", "template", "svg", "head"})
    BLOCK = frozenset({
        "p", "div", "section", "article", "header", "footer", "main", "aside", "nav", "li",
        "ul", "ol", "table", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote",
        "br", "hr", "dt", "dd", "figcaption", "title",
    })  # fmt: skip

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: str | None = None
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip_depth += 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title = (self.title or "") + data
        elif not self._skip_depth:
            self.parts.append(data)


def _parse_html(data: bytes) -> list[Page]:
    extractor = _TextExtractor()
    extractor.feed(data.decode("utf-8", errors="replace"))
    extractor.close()
    body = re.sub(r"[ \t]+", " ", "".join(extractor.parts))
    body = re.sub(r"\n\s*\n+", "\n\n", body).strip()
    title = (extractor.title or "").strip()
    return [Page(1, f"{title}\n\n{body}" if title else body)]


# ----------------------------------------------------------------------------- text
def _parse_text(data: bytes) -> list[Page]:
    return [Page(1, data.decode("utf-8", errors="replace"))]


def detect_format(filename: str, content_type: str | None) -> str | None:
    suffix = PurePath(filename).suffix.lower()
    for name, types, suffixes in (
        ("pdf", PDF_TYPES, PDF_SUFFIXES),
        ("docx", DOCX_TYPES, DOCX_SUFFIXES),
        ("html", HTML_TYPES, HTML_SUFFIXES),
        ("text", TEXT_TYPES, TEXT_SUFFIXES),
    ):
        if content_type in types or suffix in suffixes:
            return name
    return None


def parse_document(
    data: bytes, filename: str, content_type: str | None, *, ocr: OcrFn | None = None
) -> list[Page]:
    """Dispatch on content type, falling back to the file extension."""
    pages, _ = parse_document_with_stats(data, filename, content_type, ocr=ocr)
    return pages


def parse_document_with_stats(
    data: bytes, filename: str, content_type: str | None, *, ocr: OcrFn | None = None
) -> tuple[list[Page], int]:
    """Like :func:`parse_document`, also returning how many pages needed OCR."""
    fmt = detect_format(filename, content_type)
    ocr_pages = 0
    if fmt == "pdf":
        pages, ocr_pages = _parse_pdf(data, ocr)
    elif fmt == "docx":
        pages = _parse_docx(data)
    elif fmt == "html":
        pages = _parse_html(data)
    elif fmt == "text":
        pages = _parse_text(data)
    else:
        raise UnsupportedFileTypeError(
            f"unsupported file type {content_type!r} ({filename}); "
            "expected PDF, DOCX, HTML, TXT or Markdown"
        )
    if not any(p.text.strip() for p in pages):
        hint = "" if ocr is not None else " (scanned PDF: enable OCR to read it)"
        raise EmptyDocumentError(f"no extractable text{hint}")
    return pages, ocr_pages
