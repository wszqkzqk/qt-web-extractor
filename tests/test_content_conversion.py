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


if __name__ == "__main__":
    unittest.main()
