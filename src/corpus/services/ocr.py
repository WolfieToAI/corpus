"""OCR for scanned PDF pages: render with PDFium, recognise with Tesseract.

Only pages without a usable text layer are OCR-ed (see ``parsers.MIN_TEXT_CHARS``), so born-
digital PDFs pay nothing. ``OCR=auto`` (default) enables it when the Python packages (extra
``ocr``) and the ``tesseract`` binary are installed - the Docker image ships both, with English,
French and German models.
"""

from __future__ import annotations

import io
import logging
import shutil
from typing import TYPE_CHECKING

from corpus.services.parsers import OcrFn, parse_document_with_stats

if TYPE_CHECKING:
    from corpus.config import Settings

logger = logging.getLogger(__name__)


class TesseractOcr:
    def __init__(self, languages: str = "eng+fra+deu", dpi: int = 300) -> None:
        self.languages = languages
        self.dpi = dpi

    def __call__(self, pdf: bytes, page_index: int) -> str:
        import pypdfium2 as pdfium
        import pytesseract

        document = pdfium.PdfDocument(pdf)
        try:
            image = document[page_index].render(scale=self.dpi / 72).to_pil()
        finally:
            document.close()
        text: str = pytesseract.image_to_string(image, lang=self.languages)
        return text


def ocr_available() -> bool:
    try:
        import pypdfium2  # noqa: F401
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return shutil.which("tesseract") is not None


def build_ocr(settings: Settings) -> OcrFn | None:
    if settings.ocr == "off":
        return None
    if not ocr_available():
        if settings.ocr == "tesseract":
            raise RuntimeError("OCR=tesseract but pytesseract/pypdfium2 or tesseract is missing")
        logger.info("OCR unavailable (install the 'ocr' extra and tesseract); scans are rejected")
        return None
    return TesseractOcr(settings.ocr_languages)


def selftest() -> None:
    """Render text to an image-only PDF and read it back. Run in CI inside the Docker image."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1700, 300), "white")
    font = ImageFont.load_default(size=72)
    ImageDraw.Draw(image).text((60, 100), "Warranty claim 4217", fill="black", font=font)
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=150)

    ocr = TesseractOcr()
    pages, ocr_pages = parse_document_with_stats(
        buffer.getvalue(), "scan.pdf", "application/pdf", ocr=ocr
    )
    text = pages[0].text
    assert ocr_pages == 1, "the image-only page should have been OCR-ed"
    assert "4217" in text and "warranty" in text.lower(), f"unexpected OCR output: {text!r}"
    print(f"OCR self-test passed: {text.strip()!r}")


if __name__ == "__main__":
    selftest()
