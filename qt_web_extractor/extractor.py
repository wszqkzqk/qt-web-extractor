#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Copyright (C) 2026 Zhou Qiankang <wszqkzqk@qq.com>
#
# This file is part of Qt Web Extractor.
#
# Qt Web Extractor is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Qt Web Extractor is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Qt Web Extractor. If not, see <https://www.gnu.org/licenses/>.

import os
import sys
import json
import atexit
import urllib.parse
import urllib.request
import shiboken6
from PySide6.QtCore import QUrl, QTimer, QEventLoop, Signal, QByteArray, QBuffer, QIODevice
from PySide6.QtWidgets import QApplication
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
)

if "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --disable-software-rasterizer",
)


def _detect_pdf(
    url: str,
    user_agent: str | None = None,
    timeout: int = 10,
) -> bool:
    """Check if *url* points to a PDF.

    .pdf suffix is a fast path; otherwise for http(s) URLs a HEAD
    request is sent to check Content-Type.
    """
    parsed = urllib.parse.urlparse(url)
    path = parsed.path.rstrip("/").lower()

    if path.endswith(".pdf"):
        return True

    if parsed.scheme not in ("http", "https"):
        return False

    try:
        req = urllib.request.Request(url, method="HEAD")
        if user_agent:
            req.add_header("User-Agent", user_agent)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ct = resp.headers.get("Content-Type", "")
            return "application/pdf" in ct.lower()
    except Exception:
        return False

class _ExtractionResult:
    __slots__ = ("url", "title", "text", "html", "error")

    def __init__(
        self,
        url: str = "",
        title: str = "",
        text: str = "",
        html: str = "",
        error: str = "",
    ):
        self.url = url
        self.title = title
        self.text = text
        self.html = html
        self.error = error

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "html": self.html,
            "error": self.error,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


class _WebPage(QWebEnginePage):
    extraction_done = Signal(object)

    def __init__(self, profile: QWebEngineProfile, timeout_ms: int = 30000):
        super().__init__(profile)
        self._timeout_ms = timeout_ms
        self._result = _ExtractionResult()
        self._settled = False
        self._load_ok = False

        self.loadFinished.connect(self._on_load_finished)

        # post-load JS settle delay
        self._stability_timer = QTimer()
        self._stability_timer.setSingleShot(True)
        self._stability_timer.setInterval(2000)
        self._stability_timer.timeout.connect(self._extract_content)

        self._timeout_timer = QTimer()
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.setInterval(self._timeout_ms)
        self._timeout_timer.timeout.connect(self._on_timeout)

    def start_loading(self, url: str):
        self._result.url = url
        self._timeout_timer.start()
        self.load(QUrl(url))

    def _on_load_finished(self, ok: bool):
        if self._settled:
            return
        if ok:
            self._load_ok = True
        # ok=False may just be a JS challenge redirect; wait and retry.
        self._stability_timer.start()

    def _on_timeout(self):
        if self._settled:
            return
        self._stability_timer.stop()
        self._extract_content(timed_out=True)

    def _extract_content(self, timed_out: bool = False):
        if self._settled:
            return
        self._result.title = self.title()
        self._result.url = self.url().toString()
        self.toPlainText(self._on_text_ready)
        if timed_out:
            self._result.error = "Timed out (partial content may be available)"
        elif not self._load_ok:
            self._result.error = "Page load reported failure (content may be incomplete)"

    def _on_text_ready(self, text: str):
        if self._settled:
            return
        self._result.text = text
        self.toHtml(self._on_html_ready)

    def _on_html_ready(self, html: str):
        if self._settled:
            return
        self._result.html = html
        self._finish()

    def _finish(self):
        self._settled = True
        self._timeout_timer.stop()
        self._stability_timer.stop()
        self.extraction_done.emit(self._result)


class QtWebExtractor:
    """Headless web content extractor using Qt WebEngine (Chromium)."""

    def __init__(
        self,
        timeout_ms: int = 30000,
        user_agent: str | None = None,
        persist_cookies: bool = False,
        storage_path: str | None = None,
    ):
        self._timeout_ms = timeout_ms
        self._user_agent = user_agent
        self._persist_cookies = persist_cookies
        self._storage_path = storage_path
        self._app = self._ensure_app()
        self._profile = self._create_profile()
        self._pages: list[_WebPage] = []

        atexit.register(self._cleanup)

    def __del__(self):
        self._cleanup()

    @staticmethod
    def _ensure_app() -> QApplication:
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            app = QApplication(sys.argv)
        return app

    def _create_profile(self) -> QWebEngineProfile:
        if self._persist_cookies and self._storage_path:
            profile = QWebEngineProfile(self._storage_path)
            profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
            )
        else:
            profile = QWebEngineProfile()
            profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
            )

        if self._user_agent:
            profile.setHttpUserAgent(self._user_agent)

        s = profile.settings()
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        s.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.AutoLoadImages, False)
        s.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, False)

        return profile

    def _cleanup(self):
        for page in self._pages:
            try:
                if shiboken6.isValid(page):
                    shiboken6.delete(page)
            except RuntimeError:
                pass
        self._pages.clear()
        if self._app:
            self._app.processEvents()
        if self._profile is not None:
            try:
                if shiboken6.isValid(self._profile):
                    shiboken6.delete(self._profile)
            except RuntimeError:
                pass
            self._profile = None

    def extract(self, url: str) -> _ExtractionResult:
        loop = QEventLoop()
        result_holder: list[_ExtractionResult] = []

        assert self._profile is not None, "Profile has been cleaned up"
        page = _WebPage(self._profile, self._timeout_ms)
        self._pages.append(page)

        def on_done(result: _ExtractionResult):
            result_holder.append(result)
            loop.quit()

        page.extraction_done.connect(on_done)
        page.start_loading(url)
        loop.exec()

        page.extraction_done.disconnect(on_done)
        if shiboken6.isValid(page):
            shiboken6.delete(page)
        try:
            self._pages.remove(page)
        except ValueError:
            pass

        return result_holder[0] if result_holder else _ExtractionResult(
            url=url, error="Extraction failed: no result received"
        )

    def extract_pdf(self, url_or_path: str) -> _ExtractionResult:
        """Extract text from a PDF file or URL using Qt PDF."""
        result = _ExtractionResult(url=url_or_path)

        # prevent GC while doc is in use
        _buffer: QBuffer | None = None
        _byte_array: QByteArray | None = None

        try:
            doc = QPdfDocument()
            parsed = urllib.parse.urlparse(url_or_path)

            if parsed.scheme in ("http", "https", "ftp"):
                req = urllib.request.Request(url_or_path)
                if self._user_agent:
                    req.add_header("User-Agent", self._user_agent)
                with urllib.request.urlopen(
                    req, timeout=self._timeout_ms // 1000
                ) as resp:
                    data = resp.read()
                    _byte_array = QByteArray(data)
                    _buffer = QBuffer(_byte_array)
                    _buffer.open(QIODevice.OpenModeFlag.ReadOnly)
                    doc.load(_buffer)
            else:
                local_path = parsed.path if parsed.scheme == "file" else url_or_path
                doc.load(local_path)

            if doc.status() != QPdfDocument.Status.Ready:
                result.error = f"Failed to load PDF: {doc.status().name}"
                return result

            pages = doc.pageCount()
            text_parts: list[str] = []
            for i in range(pages):
                text_parts.append(doc.getAllText(i).text())

            result.text = "\n\n".join(text_parts)
            result.title = os.path.basename(url_or_path)
        except Exception as e:
            result.error = str(e)

        return result

    def extract_multiple(self, urls: list[str]) -> list[_ExtractionResult]:
        return [self.extract(url) for url in urls]
