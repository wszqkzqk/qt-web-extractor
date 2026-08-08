import os
import unittest

from PySide6.QtWidgets import QApplication

from qt_web_extractor.extractor import _WebPage


os.environ["QT_LOGGING_RULES"] = "\n".join(
    rule
    for rule in (
        os.environ.get("QT_LOGGING_RULES", ""),
        "qt.qpa.fonts.warning=false",
    )
    if rule
)
_APP = QApplication.instance() or QApplication([])


class ContentConversionTests(unittest.TestCase):
    def test_html_to_markdown_keeps_content_and_removes_unsafe_payloads(self):
        raw_html = """
        <html>
          <head>
            <style>.secret { content: "LEAKED_STYLE_TOKEN"; }</style>
            <script>window.secret = "LEAKED_SCRIPT_TOKEN";</script>
          </head>
          <body>
            <main>
              <h1>Visible heading</h1>
              <p>Read the <a href="https://example.com/docs">documentation</a>.</p>
              <img
                src="data:image/png;base64,LEAKED_DATA_TOKEN"
                alt="Inline diagram"
              >
              <div data-preview="data:text/plain,LEAKED_DATA_TOKEN">
                Visible body text
              </div>
              <p style="background-image: url(data:image/png;base64,LEAKED_DATA_TOKEN)">
                Styled body text
              </p>
            </main>
          </body>
        </html>
        """

        markdown = _WebPage._text_from_html(raw_html)

        self.assertIn("Visible heading", markdown)
        self.assertIn("[documentation](https://example.com/docs)", markdown)
        self.assertIn("Inline diagram", markdown)
        self.assertIn("Visible body text", markdown)
        self.assertIn("Styled body text", markdown)
        self.assertNotIn("LEAKED_SCRIPT_TOKEN", markdown)
        self.assertNotIn("LEAKED_STYLE_TOKEN", markdown)
        self.assertNotIn("LEAKED_DATA_TOKEN", markdown)
        self.assertNotIn("data:", markdown)

    def test_data_uri_img_keeps_image_marker(self):
        markdown = _WebPage._text_from_html(
            '<p>before</p>'
            '<img src="data:image/png;base64,XX" alt="chart alt">'
            '<p>after</p>'
        )
        self.assertIn("![chart alt]()", markdown)
        self.assertNotIn("XX", markdown)
        self.assertNotIn("data:", markdown)

    def test_data_uri_img_with_empty_alt_drops_tag(self):
        # An explicit empty alt must not crash the replacement callback.
        for quoting in ('alt=""', "alt=''"):
            with self.subTest(quoting=quoting):
                markdown = _WebPage._text_from_html(
                    '<p>x</p>'
                    f'<img src="data:image/png;base64,XX" {quoting}>'
                )
                self.assertIn("x", markdown)
                self.assertNotIn("XX", markdown)
                self.assertNotIn("data:", markdown)

    def test_data_uri_img_matched_past_quoted_angle_brackets(self):
        # A ">" inside another attribute must not end the tag match.
        markdown = _WebPage._text_from_html(
            '<p>x</p>'
            '<img title="a>b" src="data:image/png;base64,LEAKED_DATA_TOKEN" alt="pic">'
        )
        self.assertIn("![pic]()", markdown)
        self.assertNotIn("LEAKED_DATA_TOKEN", markdown)
        self.assertNotIn("data:", markdown)

    def test_img_with_real_src_and_data_uri_data_src_keeps_real_url(self):
        markdown = _WebPage._text_from_html(
            '<p>x</p>'
            '<img src="https://cdn.example/real.png"'
            ' data-src="data:image/png;base64,LEAKED_DATA_TOKEN" alt="y">'
        )
        self.assertIn("![y](https://cdn.example/real.png)", markdown)
        self.assertNotIn("LEAKED_DATA_TOKEN", markdown)
        self.assertNotIn("data:", markdown)

    def test_img_with_underscore_data_src_keeps_real_url(self):
        # "src" inside data_src / lazy:src must not trigger the replacement.
        for attr in ("data_src", "lazy:src"):
            with self.subTest(attr=attr):
                markdown = _WebPage._text_from_html(
                    '<p>x</p>'
                    f'<img {attr}="data:image/png;base64,LEAKED_DATA_TOKEN"'
                    ' src="https://cdn.example/real.png" alt="y">'
                )
                self.assertIn("![y](https://cdn.example/real.png)", markdown)
                self.assertNotIn("LEAKED_DATA_TOKEN", markdown)

    def test_data_uri_img_via_data_src_keeps_image_marker(self):
        markdown = _WebPage._text_from_html(
            '<p>x</p>'
            '<img data-src="data:image/png;base64,LEAKED_DATA_TOKEN" alt="w">'
        )
        self.assertIn("![w]()", markdown)
        self.assertNotIn("LEAKED_DATA_TOKEN", markdown)
        self.assertNotIn("data:", markdown)

    def test_data_uri_img_alt_cannot_inject_markup(self):
        markdown = _WebPage._text_from_html(
            '<p>x</p>'
            '<img src="data:image/png;base64,XX" alt=\'a"><b>bold</b>\'>'
        )
        # The alt text stays literal inside the marker; no <b> element forms.
        self.assertNotIn("**bold**", markdown)
        self.assertNotIn("data:", markdown)


if __name__ == "__main__":
    unittest.main()
