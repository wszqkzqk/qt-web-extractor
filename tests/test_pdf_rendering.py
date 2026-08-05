import tempfile
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QSizeF
from PySide6.QtGui import QColor, QImage, QPainter, QPdfWriter

from qt_web_extractor.extractor import QtWebExtractor


class PdfRenderingTests(unittest.TestCase):
    def test_page_selection_is_bounded_and_deduplicated(self):
        select = QtWebExtractor._select_pdf_pages
        self.assertEqual(select(None, 100, 10), (list(range(1, 11)), True))
        self.assertEqual(select([], 100, 10), (list(range(1, 11)), True))
        self.assertEqual(select([5, "1-3", "3-6"], 100, 10), (list(range(1, 7)), False))
        self.assertEqual(
            select(["1-1000000"], 1_000_000, 10),
            (list(range(1, 11)), True),
        )

    def test_jpeg_encoding_composites_transparency_on_white(self):
        image = QImage(64, 64, QImage.Format.Format_ARGB32)
        image.fill(QColor(0, 0, 0, 0))
        painter = QPainter(image)
        painter.fillRect(0, 0, 32, 32, QColor(255, 0, 0, 128))
        painter.end()

        data, error = QtWebExtractor._encode_pdf_page_jpeg(image)

        self.assertIsNone(error)
        self.assertTrue(data.startswith(b"\xff\xd8"))
        decoded = QImage.fromData(data)
        self.assertEqual(decoded.size(), image.size())
        self.assertGreater(decoded.pixelColor(48, 48).red(), 245)
        self.assertGreater(decoded.pixelColor(16, 16).red(), 240)
        self.assertLess(decoded.pixelColor(16, 16).green(), 145)

    def test_total_jpeg_limit_keeps_completed_prefix(self):
        image = QImage(8, 8, QImage.Format.Format_RGB32)
        image.fill(QColor("white"))
        document = Mock()
        document.pageCount.return_value = 4
        document.pagePointSize.return_value = QSizeF(72, 72)
        document.render.return_value = image
        encode = Mock(return_value=(b"abc", None))
        extractor = SimpleNamespace(
            _timeout_ms=5_000,
            _pdf_max_render_pages=10,
            _load_pdf_document=Mock(return_value=(document, None, "")),
            _select_pdf_pages=QtWebExtractor._select_pdf_pages,
            _encode_pdf_page_jpeg=encode,
        )

        with patch(
            "qt_web_extractor.extractor._PDF_RENDER_MAX_TOTAL_BYTES",
            6,
        ):
            result = QtWebExtractor.render_pdf_pages(extractor, "sample.pdf")

        self.assertEqual(result.error, "")
        self.assertTrue(result.truncated)
        self.assertEqual([page.page for page in result.pages], [1, 2])
        self.assertEqual(sum(len(page.data) for page in result.pages), 6)
        self.assertEqual(document.render.call_count, 3)
        self.assertEqual(encode.call_count, 3)

    def test_local_pdf_pages_are_returned_directly_as_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "sample.pdf")
            writer = QPdfWriter(str(path))
            painter = QPainter(writer)
            painter.fillRect(100, 100, 500, 500, QColor("red"))
            painter.end()
            del writer

            extractor = object.__new__(QtWebExtractor)
            extractor._timeout_ms = 5_000
            extractor._pdf_max_render_pages = 10
            extractor._pdf_cache = OrderedDict()
            extractor._pages = []
            extractor._app = None
            extractor._profile = None
            extractor._render_profile = None

            result = extractor.render_pdf_pages(str(path), [])

        self.assertEqual(result.error, "")
        self.assertEqual(result.page_count, 1)
        self.assertEqual(len(result.pages), 1)
        self.assertEqual(result.pages[0].mime_type, "image/jpeg")
        self.assertTrue(result.pages[0].data.startswith(b"\xff\xd8"))


class PdfDownloadProbeTests(unittest.TestCase):
    def test_html_interstitial_does_not_force_download(self):
        forces = QtWebExtractor._pdf_probe_forces_download
        # Interstitial or error page: wait it out, do not download.
        self.assertFalse(forces('["text/html", "Verifying your browser..."]'))
        self.assertFalse(forces('["application/xhtml+xml", "Just a moment"]'))
        # PDF bytes mislabeled as renderable text: force the download.
        self.assertTrue(forces('["text/html", "%PDF-1.4"]'))
        self.assertTrue(forces('["text/plain", "%PDF-1.7"]'))
        # Anything unparsable conservatively forces the download.
        self.assertTrue(forces(""))
        self.assertTrue(forces("not json"))
        self.assertTrue(forces('["text/html"]'))
        self.assertTrue(forces(None))


if __name__ == "__main__":
    unittest.main()
