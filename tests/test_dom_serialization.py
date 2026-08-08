import os
import unittest

import shiboken6
from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWidgets import QApplication

from qt_web_extractor.extractor import _SERIALIZE_DOM_JS, _WebPage


os.environ["QT_LOGGING_RULES"] = "\n".join(
    rule
    for rule in (
        os.environ.get("QT_LOGGING_RULES", ""),
        "qt.qpa.fonts.warning=false",
    )
    if rule
)
_APP = QApplication.instance() or QApplication([])

_BASE_URL = "https://site.example/dir/page.html"


def serialize_dom(body_html: str) -> str:
    """Run the DOM serialization JS on *body_html* and return the HTML."""
    profile = QWebEngineProfile()
    page = QWebEnginePage(profile)
    loop = QEventLoop()
    out = []

    def on_load(_ok):
        page.runJavaScript(
            _SERIALIZE_DOM_JS,
            0,
            lambda value: (out.append(value), loop.quit()),
        )

    page.loadFinished.connect(on_load)
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.setInterval(10_000)
    watchdog.timeout.connect(loop.quit)
    watchdog.start()
    page.setHtml(f"<html><body>{body_html}</body></html>", QUrl(_BASE_URL))
    loop.exec()
    watchdog.stop()
    page.loadFinished.disconnect(on_load)
    shiboken6.delete(page)
    shiboken6.delete(profile)
    _APP.processEvents()
    return out[0] if out else ""


def serialize_markdown(body_html: str) -> str:
    return _WebPage._text_from_html(serialize_dom(body_html))


class DomSerializationTests(unittest.TestCase):
    def test_data_placeholder_src_backfilled_from_data_src(self):
        md = serialize_markdown(
            '<img src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E"'
            ' data-src="https://cdn.example/real.png" alt="chart">'
        )
        self.assertIn("![chart](https://cdn.example/real.png)", md)
        self.assertNotIn("data:", md)

    def test_empty_and_missing_src_backfilled(self):
        md = serialize_markdown(
            '<img src="" data-src="https://cdn.example/a.png" alt="a">'
            '<img data-src="https://cdn.example/b.png" alt="b">'
        )
        self.assertIn("![a](https://cdn.example/a.png)", md)
        self.assertIn("![b](https://cdn.example/b.png)", md)

    def test_relative_urls_resolved_against_page_url(self):
        md = serialize_markdown(
            '<img src="/abs/path.png" alt="a">'
            '<img src="rel.png" alt="b">'
            '<img data-src="lazy/c.png" alt="c">'
        )
        self.assertIn("![a](https://site.example/abs/path.png)", md)
        self.assertIn("![b](https://site.example/dir/rel.png)", md)
        self.assertIn("![c](https://site.example/dir/lazy/c.png)", md)

    def test_real_src_preferred_over_data_src(self):
        md = serialize_markdown(
            '<img src="https://cdn.example/low.png"'
            ' data-src="https://cdn.example/high.png" alt="x">'
        )
        self.assertIn("![x](https://cdn.example/low.png)", md)

    def test_inline_data_uri_without_data_src_leaks_nothing(self):
        md = serialize_markdown(
            '<img src="data:image/png;base64,LEAKED_DATA_TOKEN" alt="E">'
        )
        self.assertNotIn("LEAKED_DATA_TOKEN", md)
        self.assertNotIn("data:", md)

    def test_placeholder_img_without_usable_url_keeps_image_marker(self):
        md = serialize_markdown(
            '<img src="data:image/png;base64,XX" alt="chart alt">'
        )
        self.assertIn("![chart alt]()", md)
        self.assertNotIn("data:", md)

    def test_data_uri_data_src_not_backfilled(self):
        html = serialize_dom(
            '<img src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E"'
            ' data-src="data:image/png;base64,LEAKED_DATA_TOKEN" alt="w">'
        )
        self.assertNotIn("LEAKED_DATA_TOKEN", html)
        self.assertNotIn("data:", html)
        self.assertIn('<img alt="w">', html)

    def test_text_attr_starting_with_data_prefix_survives(self):
        # alt text starting with "data:" is not a payload; keep it.
        md = serialize_markdown(
            '<img src="https://cdn.example/real.png" alt="data: quarterly results">'
        )
        self.assertIn("![data: quarterly results](https://cdn.example/real.png)", md)

    def test_svg_semantic_text_kept_geometry_dropped(self):
        md = serialize_markdown(
            '<svg aria-label="architecture diagram"><path d="M1 1h14"/></svg>'
            '<svg><title>decay curve</title><path d="M0 0"/></svg>'
            '<svg><text x="0">KDA</text><text x="9">MLA</text><rect/></svg>'
            '<svg viewBox="0 0 16 16"><path d="M2 2lSECRET_GEOMETRY 4 4"/></svg>'
        )
        self.assertIn("architecture diagram", md)
        self.assertIn("decay curve", md)
        self.assertIn("KDA", md)
        self.assertIn("MLA", md)
        self.assertNotIn("SECRET_GEOMETRY", md)
        self.assertNotIn("<svg", md)

    def test_svg_text_capped(self):
        md = serialize_markdown(f"<svg><text>{'x' * 500}</text></svg>")
        self.assertLessEqual(len(md.strip()), 200)


if __name__ == "__main__":
    unittest.main()
