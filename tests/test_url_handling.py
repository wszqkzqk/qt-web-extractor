import unittest
from pathlib import Path
from unittest import mock

from qt_web_extractor.extractor import QtWebExtractor, _as_url
from qt_web_extractor.server import _url_access_error


class UrlHandlingTests(unittest.TestCase):
    def detect_kind(self, url: str, content_type: str) -> str:
        head = mock.Mock(return_value=content_type)
        extractor = mock.Mock(_head_content_type=head)
        kind = QtWebExtractor.detect_url_kind(extractor, url, timeout=7)
        head.assert_called_once_with(url, 7)
        return kind

    def test_as_url_preserves_urls_and_converts_paths(self):
        for url in (
            "https://example.com/file.pdf",
            "ftp://example.com/file.pdf",
            "file:///tmp/file.pdf",
        ):
            with self.subTest(url=url):
                self.assertEqual(_as_url(url), url)

        for path in (
            "document.pdf",
            "folder/image with spaces.png",
            r"C:\Documents\report.pdf",
        ):
            with self.subTest(path=path):
                self.assertEqual(_as_url(path), Path(path).absolute().as_uri())

    def test_url_access_policy(self):
        with mock.patch("qt_web_extractor.server.log.warning"):
            for url in (
                "http://example.com",
                "https://example.com",
                "ftp://example.com/file.pdf",
            ):
                with self.subTest(url=url):
                    self.assertIsNone(_url_access_error(url, allow_local_files=False))

            for url in (
                "document.pdf",
                "file:///tmp/document.pdf",
                r"C:\Documents\report.pdf",
            ):
                with self.subTest(url=url, allowed=False):
                    self.assertEqual(
                        _url_access_error(url, allow_local_files=False),
                        "unsupported or invalid URL",
                    )
                with self.subTest(url=url, allowed=True):
                    self.assertIsNone(_url_access_error(url, allow_local_files=True))

            for url, scheme in (
                ("data:text/plain,hello", "data"),
                ("javascript:alert(1)", "javascript"),
                ("chrome://settings", "chrome"),
            ):
                with self.subTest(url=url):
                    self.assertEqual(
                        _url_access_error(url, allow_local_files=True),
                        f"this server does not support the {scheme!r} URL scheme",
                    )

    def test_detect_url_kind_prefers_authoritative_mime(self):
        cases = (
            ("https://example.com/not-really.pdf", "text/html", "page"),
            ("https://example.com/not-really.png", "application/pdf", "pdf"),
            ("https://example.com/not-really.pdf", "image/jpeg", "image"),
            ("https://example.com/download", "image/custom", "image"),
        )
        for url, content_type, expected in cases:
            with self.subTest(url=url, content_type=content_type):
                self.assertEqual(self.detect_kind(url, content_type), expected)

    def test_detect_url_kind_falls_back_for_unknown_mime(self):
        cases = (
            ("https://example.com/report.PDF?download=1", "", "pdf"),
            ("https://example.com/picture.avif", "application/octet-stream", "image"),
            ("https://example.com/download", "", "page"),
            ("https://example.com/report.pdf", "application/octet-stream", "pdf"),
        )
        for url, content_type, expected in cases:
            with self.subTest(url=url, content_type=content_type):
                self.assertEqual(self.detect_kind(url, content_type), expected)

    def test_detect_url_kind_supports_all_image_suffixes(self):
        for suffix in (
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".avif",
            ".svg",
            ".bmp",
            ".ico",
        ):
            url = f"https://example.com/image{suffix.upper()}?raw=1"
            with self.subTest(suffix=suffix):
                self.assertEqual(self.detect_kind(url, ""), "image")


if __name__ == "__main__":
    unittest.main()
