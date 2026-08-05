import unittest
from collections import OrderedDict
from unittest.mock import patch

from qt_web_extractor.extractor import QtWebExtractor


def _extractor(*, budget: int = 10, ttl: int = 60) -> QtWebExtractor:
    extractor = object.__new__(QtWebExtractor)
    extractor._pdf_cache = OrderedDict()
    extractor._pdf_cache_max_bytes = budget
    extractor._pdf_cache_ttl_s = ttl
    extractor._pages = []
    extractor._app = None
    extractor._profile = None
    extractor._render_profile = None
    return extractor


class PdfCacheTests(unittest.TestCase):
    def test_fresh_entry_is_returned_and_expired_entry_is_removed(self):
        extractor = _extractor(ttl=10)
        with patch("qt_web_extractor.extractor.time.monotonic", return_value=100):
            extractor._cache_put_pdf("https://example.test/a.pdf", b"pdf")

        with patch("qt_web_extractor.extractor.time.monotonic", return_value=110):
            self.assertEqual(
                extractor._cache_get_pdf("https://example.test/a.pdf"), b"pdf"
            )
        with patch("qt_web_extractor.extractor.time.monotonic", return_value=111):
            self.assertIsNone(
                extractor._cache_get_pdf("https://example.test/a.pdf")
            )
        self.assertNotIn("https://example.test/a.pdf", extractor._pdf_cache)

    def test_cache_hit_promotes_entry_before_lru_eviction(self):
        extractor = _extractor(budget=6)
        with patch("qt_web_extractor.extractor.time.monotonic", return_value=1):
            extractor._cache_put_pdf("a", b"aaa")
            extractor._cache_put_pdf("b", b"bbb")

        with patch("qt_web_extractor.extractor.time.monotonic", return_value=2):
            self.assertEqual(extractor._cache_get_pdf("a"), b"aaa")
        with patch("qt_web_extractor.extractor.time.monotonic", return_value=3):
            extractor._cache_put_pdf("c", b"ccc")

        self.assertEqual(list(extractor._pdf_cache), ["a", "c"])
        self.assertNotIn("b", extractor._pdf_cache)

    def test_capacity_evicts_oldest_entries_until_within_budget(self):
        extractor = _extractor(budget=7)
        with patch("qt_web_extractor.extractor.time.monotonic", return_value=1):
            extractor._cache_put_pdf("a", b"aaaa")
            extractor._cache_put_pdf("b", b"bb")
            extractor._cache_put_pdf("c", b"ccc")

        self.assertEqual(list(extractor._pdf_cache), ["b", "c"])
        self.assertLessEqual(
            sum(len(data) for data, _timestamp in extractor._pdf_cache.values()),
            extractor._pdf_cache_max_bytes,
        )

    def test_entry_at_limit_is_cached_but_oversized_entry_is_not(self):
        extractor = _extractor(budget=4)
        with patch("qt_web_extractor.extractor.time.monotonic", return_value=1):
            extractor._cache_put_pdf("fits", b"1234")
            extractor._cache_put_pdf("too-large", b"12345")

        self.assertEqual(list(extractor._pdf_cache), ["fits"])
        with patch("qt_web_extractor.extractor.time.monotonic", return_value=2):
            self.assertEqual(extractor._cache_get_pdf("fits"), b"1234")
            self.assertIsNone(extractor._cache_get_pdf("too-large"))


if __name__ == "__main__":
    unittest.main()
