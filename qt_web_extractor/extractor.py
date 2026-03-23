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
import time
import io
import zipfile
import json
import atexit
import html as html_lib
import logging
import re
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
import shiboken6
from PySide6.QtCore import (
    QUrl,
    QTimer,
    QEventLoop,
    Signal,
    QByteArray,
    QBuffer,
    QIODevice,
)
from PySide6.QtGui import QTextDocument
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

log = logging.getLogger("qt-web-extractor")


@dataclass(frozen=True)
class _ProxyConfig:
    proxies: dict[str, str]
    no_proxy: tuple[str, ...] = ()


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

    # Only attempt HTML fallback when rendered text is shorter than this.
    _FALLBACK_TRIGGER = 500

    def __init__(self, profile: QWebEngineProfile, timeout_ms: int = 30000):
        super().__init__(profile)
        self._timeout_ms = timeout_ms
        self._result = _ExtractionResult()
        self._settled = False
        self._load_ok = False

        self.loadFinished.connect(self._on_load_finished)

        # post-load JS settle delay
        self._stability_timer = QTimer(self)
        self._stability_timer.setSingleShot(True)
        self._stability_timer.setInterval(2000)
        self._stability_timer.timeout.connect(self._extract_content)

        self._timeout_timer = QTimer(self)
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
            self._result.error = (
                "Page load reported failure (content may be incomplete)"
            )

    def _on_text_ready(self, text: str):
        if self._settled:
            return
        self._result.text = text
        self.toHtml(self._on_html_ready)

    def _on_html_ready(self, html: str):
        if self._settled:
            return
        self._result.html = html

        # Convert the current HTML to Markdown to preserve links
        doc = QTextDocument()
        doc.setHtml(html)
        md_text = doc.toMarkdown().strip()
        
        # Compare the WebEngine's raw plain text size against our trigger.
        text_len = len(self._result.text.strip())
        if text_len < self._FALLBACK_TRIGGER:
            fallback = self._text_from_html(html)
            if len(fallback) > max(text_len, 1) * 5:
                # If fallback yields significantly more text, use it
                self._result.text = fallback
            else:
                self._result.text = md_text
        else:
            self._result.text = md_text

        self._finish()

    _RE_SCRIPT = re.compile(r"<script[\s>].*?</script>", re.DOTALL | re.IGNORECASE)
    _RE_STYLE = re.compile(r"<style[\s>].*?</style>", re.DOTALL | re.IGNORECASE)

    @classmethod
    def _text_from_html(cls, raw: str) -> str:
        """Best-effort text extraction from raw HTML, preserving links as Markdown.

        Strip script/style first — large inline JS confuses QTextDocument.
        """
        text = cls._RE_SCRIPT.sub("", raw)
        text = cls._RE_STYLE.sub("", text)
        doc = QTextDocument()
        doc.setHtml(text)
        return doc.toMarkdown().strip()

    def _finish(self):
        self._settled = True
        self._timeout_timer.stop()
        self._stability_timer.stop()
        self.extraction_done.emit(self._result)


class QtWebExtractor:
    """Headless web content extractor using Qt WebEngine (Chromium)."""

    _RE_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
    _SUPPORTED_PROXY_SCHEMES = {"http", "https"}
    _NETERROR_MARKERS = (
        '<body class="neterror"',
        "ERR_CONNECTION_",
        "ERR_SSL_",
        "ERR_HTTP2_",
    )

    def __init__(
        self,
        timeout_ms: int = 30000,
        user_agent: str | None = None,
        persist_cookies: bool = False,
        storage_path: str | None = None,
        proxy: str | None = None,
        mineru_api_key: str = "",
        mineru_timeout_ms: int = 300000,
    ):
        self._timeout_ms = timeout_ms
        self._mineru_api_key = mineru_api_key
        self._mineru_timeout_ms = mineru_timeout_ms
        self._user_agent = user_agent
        self._persist_cookies = persist_cookies
        self._storage_path = storage_path
        self._proxy_config = self._resolve_proxy_config(proxy)
        if self._proxy_config is not None:
            self._apply_chromium_proxy(self._proxy_config)
        self._app = self._ensure_app()
        self._profile = self._create_profile()
        self._http_user_agent: str = self._profile.httpUserAgent()
        self._ssl_context = self._create_ssl_context()
        self._direct_url_opener = self._build_url_opener(proxies={})
        if self._proxy_config is not None:
            self._proxy_url_opener = self._build_url_opener(
                proxies=self._proxy_config.proxies,
            )
        else:
            self._proxy_url_opener = self._direct_url_opener
        self._pages: list[_WebPage] = []

        atexit.register(self._cleanup)

    def __del__(self):
        self._cleanup()

    @staticmethod
    def _parse_no_proxy(value: str | None) -> tuple[str, ...]:
        if not value:
            return ()
        return tuple(part.strip() for part in value.split(",") if part.strip())

    @classmethod
    def _normalize_proxy_url(cls, proxy: str, *, strict: bool) -> str | None:
        parsed = urllib.parse.urlsplit(proxy)
        if parsed.scheme not in cls._SUPPORTED_PROXY_SCHEMES:
            if strict:
                raise ValueError("Only HTTP/HTTPS forward proxies are supported")
            log.warning("Ignoring unsupported proxy URL: %s", proxy)
            return None
        if not parsed.netloc:
            if strict:
                raise ValueError(f"Invalid proxy URL: {proxy}")
            log.warning("Ignoring invalid proxy URL: %s", proxy)
            return None
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @classmethod
    def _resolve_proxy_config(cls, proxy: str | None) -> _ProxyConfig | None:
        if proxy:
            normalized = cls._normalize_proxy_url(proxy, strict=True)
            assert normalized is not None
            return _ProxyConfig(
                proxies={"http": normalized, "https": normalized},
                no_proxy=cls._parse_no_proxy(
                    os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
                ),
            )

        env_proxies = urllib.request.getproxies_environment()
        proxies: dict[str, str] = {}
        for scheme in ("http", "https"):
            proxy_url = env_proxies.get(scheme) or env_proxies.get("all")
            if not proxy_url:
                continue
            normalized = cls._normalize_proxy_url(proxy_url, strict=False)
            if normalized is not None:
                proxies[scheme] = normalized

        if not proxies:
            return None

        return _ProxyConfig(
            proxies=proxies,
            no_proxy=cls._parse_no_proxy(env_proxies.get("no")),
        )

    @staticmethod
    def _format_chromium_proxy_server(proxies: dict[str, str]) -> str:
        return ";".join(f"{scheme}={url}" for scheme, url in proxies.items())

    @classmethod
    def _apply_chromium_proxy(cls, config: _ProxyConfig):
        """Inject proxy flags into the Chromium flags env var before Qt starts."""
        key = "QTWEBENGINE_CHROMIUM_FLAGS"
        existing = os.environ.get(key, "").strip()
        additions: list[str] = []

        if "--proxy-server" not in existing:
            additions.append(
                f"--proxy-server={cls._format_chromium_proxy_server(config.proxies)}"
            )
        if config.no_proxy and "--proxy-bypass-list" not in existing:
            additions.append(f"--proxy-bypass-list={';'.join(config.no_proxy)}")

        if additions:
            os.environ[key] = " ".join(part for part in (existing, *additions) if part)

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

    @staticmethod
    def _create_ssl_context() -> ssl.SSLContext:
        return ssl.create_default_context()

    def _build_url_opener(
        self, proxies: dict[str, str]
    ) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_context),
            urllib.request.ProxyHandler(proxies),
        )

    def _should_bypass_proxy(self, url: str) -> bool:
        if self._proxy_config is None or not self._proxy_config.no_proxy:
            return False
        hostname = urllib.parse.urlsplit(url).hostname
        if not hostname:
            return False
        return urllib.request.proxy_bypass(
            hostname,
            {"no": ",".join(self._proxy_config.no_proxy)},
        )

    @property
    def proxy_summary(self) -> str:
        if self._proxy_config is None:
            return "off"
        parts = [
            f"{scheme}={self._proxy_config.proxies[scheme]}"
            for scheme in ("http", "https")
            if scheme in self._proxy_config.proxies
        ]
        if self._proxy_config.no_proxy:
            parts.append(f"no_proxy={','.join(self._proxy_config.no_proxy)}")
        return ", ".join(parts)

    def _urlopen(self, request: urllib.request.Request, timeout: int | float):
        opener = (
            self._direct_url_opener
            if self._should_bypass_proxy(request.full_url)
            else self._proxy_url_opener
        )
        return opener.open(request, timeout=timeout)

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

    def detect_pdf_url(self, url: str, timeout: int = 10) -> bool:
        """Check if *url* points to a PDF.

        ``.pdf`` suffix is a fast path; otherwise for http(s) URLs a HEAD
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
            req.add_header("User-Agent", self._http_user_agent)
            with self._urlopen(req, timeout=timeout) as resp:
                ct = resp.headers.get("Content-Type", "")
                return "application/pdf" in ct.lower()
        except Exception:
            return False

    @classmethod
    def _looks_like_neterror_page(cls, result: _ExtractionResult) -> bool:
        if not result.html:
            return False
        return any(marker in result.html for marker in cls._NETERROR_MARKERS)

    def _extract_via_http_fallback(
        self, url: str
    ) -> tuple[_ExtractionResult | None, str | None]:
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", self._http_user_agent)
            req.add_header(
                "Accept",
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            )
            req.add_header("Accept-Language", "en-US,en;q=0.9")
            with self._urlopen(req, timeout=self._timeout_ms // 1000) as resp:
                charset = resp.headers.get_content_charset() or "utf-8"
                raw_html = resp.read().decode(charset, errors="replace")
                text = _WebPage._text_from_html(raw_html)
                title_match = self._RE_TITLE.search(raw_html)
                title = (
                    html_lib.unescape(title_match.group(1)).strip()
                    if title_match is not None
                    else ""
                )
                return (
                    _ExtractionResult(
                        url=resp.geturl(),
                        title=title,
                        text=text,
                        html=raw_html,
                        error="",
                    ),
                    None,
                )
        except Exception as e:
            log.warning("HTTP fallback failed for %s: %s", url, e)
            return None, str(e)

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

        result = (
            result_holder[0]
            if result_holder
            else _ExtractionResult(
                url=url, error="Extraction failed: no result received"
            )
        )

        if result.error and self._looks_like_neterror_page(result):
            fallback_result, fallback_error = self._extract_via_http_fallback(
                result.url or url
            )
            if fallback_result is not None and fallback_result.text.strip():
                return fallback_result
            if fallback_error:
                result.error = f"{result.error}; HTTP fallback failed: {fallback_error}"

        return result

    def _sleep_qt(self, ms: int):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def _extract_mineru(self, url_or_path: str) -> str:
        if not self._mineru_api_key:
            raise ValueError("No Mineru API key provided")

        start_time = time.time()
        timeout_s = self._mineru_timeout_ms / 1000.0
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._mineru_api_key}",
        }

        parsed = urllib.parse.urlparse(url_or_path)
        is_remote = parsed.scheme in ("http", "https", "ftp")

        # The URL for extraction depends on remote vs local
        if is_remote:
            data = {"url": url_or_path, "model_version": "vlm"}
            req = urllib.request.Request(
                "https://mineru.net/api/v4/extract/task",
                data=json.dumps(data).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with self._urlopen(req, timeout=self._timeout_ms // 1000) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                if resp_data.get("code") != 0:
                    raise Exception(f"Mineru API error: {resp_data.get('msg')}")
                task_id = resp_data["data"]["task_id"]

            while True:
                if time.time() - start_time > timeout_s:
                    raise TimeoutError("Mineru API polling timed out")

                req_poll = urllib.request.Request(
                    f"https://mineru.net/api/v4/extract/task/{task_id}",
                    headers={"Authorization": f"Bearer {self._mineru_api_key}"},
                )
                with self._urlopen(req_poll, timeout=self._timeout_ms // 1000) as resp:
                    poll_data = json.loads(resp.read().decode("utf-8"))
                    state = poll_data["data"]["state"]

                    if state == "done":
                        full_zip_url = poll_data["data"]["full_zip_url"]
                        break
                    elif state == "failed":
                        raise Exception(
                            f"Mineru extraction failed: {poll_data['data'].get('err_msg')}"
                        )

                self._sleep_qt(3000)

        else:
            local_path = parsed.path if parsed.scheme == "file" else url_or_path
            file_name = os.path.basename(local_path)
            data = {"files": [{"name": file_name}], "model_version": "vlm"}
            req = urllib.request.Request(
                "https://mineru.net/api/v4/file-urls/batch",
                data=json.dumps(data).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with self._urlopen(req, timeout=self._timeout_ms // 1000) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                if resp_data.get("code") != 0:
                    raise Exception(f"Mineru batch API error: {resp_data.get('msg')}")
                batch_id = resp_data["data"]["batch_id"]
                upload_url = resp_data["data"]["file_urls"][0]

            with open(local_path, "rb") as f:
                file_data = f.read()

            req_up = urllib.request.Request(upload_url, data=file_data, method="PUT")
            with self._urlopen(req_up, timeout=self._timeout_ms // 1000) as resp:
                if resp.status not in (200, 201):
                    raise Exception(
                        f"Failed to upload local file to Mineru: HTTP {resp.status}"
                    )

            while True:
                if time.time() - start_time > timeout_s:
                    raise TimeoutError("Mineru API polling timed out")

                req_poll = urllib.request.Request(
                    f"https://mineru.net/api/v4/extract-results/batch/{batch_id}",
                    headers={"Authorization": f"Bearer {self._mineru_api_key}"},
                )
                with self._urlopen(req_poll, timeout=self._timeout_ms // 1000) as resp:
                    poll_data = json.loads(resp.read().decode("utf-8"))
                    if poll_data.get("code") != 0:
                        raise Exception(
                            f"Mineru batch poll error: {poll_data.get('msg')}"
                        )

                    res_list = poll_data["data"].get("extract_result", [])
                    if not res_list:
                        raise Exception("Mineru batch poll returned empty result list")

                    target_res = res_list[0]
                    state = target_res["state"]

                    if state == "done":
                        full_zip_url = target_res.get("full_zip_url")
                        if not full_zip_url:
                            raise Exception(
                                "done state reached but full_zip_url missing"
                            )
                        break
                    elif state == "failed":
                        raise Exception(
                            f"Mineru batch extraction failed: {target_res.get('err_msg')}"
                        )

                self._sleep_qt(3000)

        if not full_zip_url:
            raise Exception("Missing full_zip_url in Mineru response")

        req_zip = urllib.request.Request(full_zip_url)
        with self._urlopen(req_zip, timeout=self._timeout_ms // 1000) as resp:
            zip_data = resp.read()

        with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
            names = z.namelist()
            matched_md = next((n for n in names if n.endswith("full.md")), None)
            if not matched_md:
                raise Exception("full.md not found in returned Zip file")

            with z.open(matched_md) as fmd:
                md_text = fmd.read().decode("utf-8")

        return md_text

    def extract_pdf(self, url_or_path: str) -> _ExtractionResult:
        """Extract text from a PDF file or URL using Mineru API (if available) or Qt PDF."""
        result = _ExtractionResult(url=url_or_path)

        if getattr(self, "_mineru_api_key", ""):
            try:
                md_text = self._extract_mineru(url_or_path)
                result.text = md_text
                result.title = os.path.basename(url_or_path)
                return result
            except Exception as e:
                log.warning("Mineru extraction failed (%s), falling back to QtPdf.", e)

        # prevent GC while doc is in use
        _buffer: QBuffer | None = None
        _byte_array: QByteArray | None = None

        try:
            doc = QPdfDocument()
            parsed = urllib.parse.urlparse(url_or_path)

            if parsed.scheme in ("http", "https", "ftp"):
                req = urllib.request.Request(url_or_path)
                req.add_header("User-Agent", self._http_user_agent)
                with self._urlopen(req, timeout=self._timeout_ms // 1000) as resp:
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
