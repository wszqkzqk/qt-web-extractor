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
import base64
import html as html_lib
import logging
import mimetypes
import pathlib
import re
import ssl
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
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
    QSize,
    QTemporaryDir,
)
from PySide6.QtGui import (
    QColor,
    QImage,
    QImageIOHandler,
    QImageWriter,
    QPainter,
    QTextDocument,
)
from PySide6.QtWidgets import QApplication
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWebEngineCore import (
    QWebEngineDownloadRequest,
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineScript,
    QWebEngineSettings,
)

if "QT_QPA_PLATFORM" not in os.environ:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--disable-gpu --disable-software-rasterizer",
)

log = logging.getLogger("qt-web-extractor")

# Authoritative extension→MIME map for the image formats we support.
# The platform MIME database is unreliable for newer formats (.avif is
# missing on stock macOS and on Python < 3.13), so only formats outside
# this map fall back to mimetypes.
_IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
}

_IMAGE_URL_SUFFIXES = tuple(_IMAGE_MIME_BY_SUFFIX)


def _as_url(path_or_url: str) -> str:
    """Return *path_or_url* as a URL; bare filesystem paths become file:// URLs.

    Windows drive letters ("C:\\...") parse as one-letter schemes under
    urlsplit; no real scheme is one letter, so a scheme of zero or one
    characters means the input is a filesystem path (consistent with the
    WHATWG URL Standard's Windows drive letter rule).
    """
    try:
        scheme = urllib.parse.urlsplit(path_or_url).scheme
    except ValueError:
        scheme = ""
    if len(scheme) <= 1:
        return pathlib.Path(path_or_url).absolute().as_uri()
    return path_or_url


# Long-edge cap for rendered images (high-res tier of current vision models).
_RENDER_MAX_EDGE = 2576

# Defensive cap on decoded rendered-image bytes.
_RENDER_MAX_BYTES = 16 * 1024 * 1024

# Defensive cap on JPEG bytes retained across one PDF render operation.
_PDF_RENDER_MAX_TOTAL_BYTES = 32 * 1024 * 1024

# PDF page render resolution. 150 DPI is the customary default for
# rasterizing documents (poppler's pdftoppm uses it as its -r default).
_PDF_RENDER_DPI = 150

# JPEG quality for PDF pages. PDFium renders transparent page backgrounds,
# which are composited onto white before encoding.
_PDF_JPEG_QUALITY = 75

# Defensive cap on browser-downloaded PDF bytes (downloads are cancelled
# beyond this; far larger than normal documents).
_PDF_DOWNLOAD_MAX_BYTES = 256 * 1024 * 1024

# PDF file magic bytes.
_PDF_MAGIC = b"%PDF-"

# Isolated JS world: page scripts cannot see or forge window.__qr.
_JS_WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld

# Shared image/PDF interstitial hint; the render profile's cookie store survives each call.
_RETRY_HINT = (
    "The browser session persists across calls, "
    "so a retry may reach the final content."
)

# Canvas-rasterize the loaded image and stash a JSON data URL in window.__qr.
# window.__att is driven by the Qt poll timer (JS timers are clamped on hidden pages).
_RENDER_JS = r"""
window.__qr = null;
window.__att = (function () {
  const EDGE = @EDGE@;
  let attempts = 0;
  let lastErr = 'Image failed to load or decode';
  function finish(v) {
    if (window.__qr) return;
    window.__qr = v;
    window.__att = null;
  }
  function mount(im) {
    try {
      const w = im.naturalWidth, h = im.naturalHeight;
      if (!w || !h) { throw new Error('no intrinsic size'); }
      const scale = Math.min(1, EDGE / Math.max(w, h));
      const cw = Math.max(1, Math.round(w * scale));
      const ch = Math.max(1, Math.round(h * scale));
      const c = document.createElementNS('http://www.w3.org/1999/xhtml', 'canvas');
      c.width = cw; c.height = ch;
      c.getContext('2d').drawImage(im, 0, 0, cw, ch);
      let data = c.toDataURL('image/webp', 0.8);
      if (data.indexOf('data:image/webp') !== 0) data = c.toDataURL('image/jpeg', 0.8);
      finish(JSON.stringify({data: data, w: cw, h: ch}));
    } catch (e) { finish('Render error: ' + e); }
  }
  return function () {
    if (window.__qr) return;
    attempts += 1;
    try {
      const b = document.body;
      if (b && b.className && b.className.indexOf('neterror') !== -1) {
        // Chromium's built-in network error page: a dead end, fail fast.
        finish('Network error: the browser could not load the URL');
        return;
      }
      const ct = document.contentType || '';
      if (ct.indexOf('image/') === 0) {
        const docImg = document.querySelector('img');
        if (docImg && docImg.naturalWidth) { mount(docImg); return; }
        if (attempts >= @ATTEMPTS@) { finish(lastErr); return; }
        const im = new Image();
        im.onload = function () { mount(im); };
        im.onerror = function () { lastErr = 'Image failed to load or decode'; };
        im.src = location.href;
      } else if (attempts >= @ATTEMPTS@) {
        // Non-image page: wait the full budget (scripts may still navigate
        // away), then report title + text so the caller can self-diagnose.
        let msg = 'The URL did not return an image (Content-Type: ' + (ct || 'unknown') + ')';
        try {
          const t = (document.title || '').replace(/\s+/g, ' ').trim().slice(0, 200);
          if (t) { msg += '. Page title: "' + t + '"'; }
          const txt = (b && b.innerText ? b.innerText : '').replace(/\s+/g, ' ').trim().slice(0, 1500);
          if (txt) { msg += '. Page content: ' + txt; }
        } catch (e) {}
        msg += ' (This looks like a temporary intermediate page rather than the final content. Call fetch_image again. ' + @HINT@ + ')';
        finish(msg);
      }
    } catch (e) { lastErr = String(e); }
  };
})();
window.__att();
"""


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


class _ImageResult:
    __slots__ = ("url", "mime_type", "data", "width", "height", "error")

    def __init__(
        self,
        url: str = "",
        mime_type: str = "",
        data: bytes = b"",
        width: int = 0,
        height: int = 0,
        error: str = "",
    ):
        self.url = url
        self.mime_type = mime_type
        self.data = data
        self.width = width
        self.height = height
        self.error = error

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "mime_type": self.mime_type,
            "width": self.width,
            "height": self.height,
            "size_bytes": len(self.data),
            "error": self.error,
        }


class _PdfPageImage:
    __slots__ = ("page", "width", "height", "data", "mime_type")

    def __init__(
        self,
        page: int = 0,
        width: int = 0,
        height: int = 0,
        data: bytes = b"",
        mime_type: str = "",
    ):
        self.page = page
        self.width = width
        self.height = height
        self.data = data
        self.mime_type = mime_type

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "width": self.width,
            "height": self.height,
            "size_bytes": len(self.data),
            "mime_type": self.mime_type,
        }


class _PdfRenderResult:
    __slots__ = ("url", "page_count", "pages", "truncated", "error")

    def __init__(
        self,
        url: str = "",
        page_count: int = 0,
        pages: "list[_PdfPageImage] | None" = None,
        truncated: bool = False,
        error: str = "",
    ):
        self.url = url
        self.page_count = page_count
        self.pages = pages if pages is not None else []
        self.truncated = truncated
        self.error = error

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "page_count": self.page_count,
            "pages": [p.to_dict() for p in self.pages],
            "truncated": self.truncated,
            "error": self.error,
        }


class _WebPage(QWebEnginePage):
    extraction_done = Signal(object)

    def __init__(self, profile: QWebEngineProfile, timeout_ms: int = 30000):
        super().__init__(profile)
        self._timeout_ms = timeout_ms
        self._result = _ExtractionResult()
        self._settled = False
        self._load_ok = False

        self.loadFinished.connect(self._on_load_finished)
        self.loadStarted.connect(self._on_load_started)

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
        self.load(QUrl(_as_url(url)))

    def _on_load_started(self):
        self._stability_timer.stop()

    def _on_load_finished(self, ok: bool):
        if self._settled:
            return
        if ok:
            self._load_ok = True
        # ok=False may just be an intermediate redirect; wait and retry.
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

        if timed_out:
            self._result.error = "Timed out (partial content may be available)"
        elif not self._load_ok:
            self._result.error = "Page load reported failure (content may be incomplete)"

        # Inject JS to serialize the Composed Tree (Shadow DOM + Slots) safely and efficiently
        js = """(function() {
            const VOID = new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
            const SKIP = new Set(['script','style','svg','noscript','template','meta','link','base','title']);
            
            function escapeHTML(str) {
                return (str || '')
                    .replace(/&/g, '&amp;')
                    .replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;')
                    .replace(/"/g, '&quot;')
                    .replace(/'/g, '&#039;');
            }

            function walk(node) {
                if (node.nodeType === Node.TEXT_NODE) return escapeHTML(node.nodeValue);
                if (node.nodeType !== Node.ELEMENT_NODE) return '';
                
                let t = node.tagName.toLowerCase();
                if (SKIP.has(t)) return '';
                
                if (t === 'slot') return [...node.assignedNodes({flatten:true})].map(walk).join('');
                
                // Map web component tags (containing '-') to <div> for QTextDocument compatibility
                let outTag = t.includes('-') ? 'div' : t;
                let h = '<' + outTag;
                
                for (let a of node.attributes) {
                    h += ' ' + a.name + '="' + escapeHTML(a.value) + '"';
                }
                
                if (VOID.has(outTag)) {
                    h += '>';
                } else {
                    h += '>' + [...(node.shadowRoot || node).childNodes].map(walk).join('') + '</' + outTag + '>';
                }
                return h;
            }
            return walk(document.documentElement);
        })();"""
        self.runJavaScript(js, 0, self._on_flattened_html_ready)

    def _on_flattened_html_ready(self, shadow_html: str):
        if self._settled:
            return
        if not shadow_html or not shadow_html.strip():
            self.toHtml(self._on_html_ready)
            return
        # Intermediate pages may self-redirect after scripts run; skip and wait.
        if not self._result.error and ('id="anubis_challenge"' in shadow_html or self.title() == "Making sure you're not a bot!"):
            return
        self._on_html_ready(shadow_html)

    def _on_html_ready(self, html: str):
        if self._settled:
            return
        self._result.html = html

        # Always remove script/style before Markdown conversion.
        self._result.text = self._text_from_html(html)

        self._finish()

    _RE_SCRIPT = re.compile(r"<script[\s>].*?</script>", re.DOTALL | re.IGNORECASE)
    _RE_STYLE = re.compile(r"<style[\s>].*?</style>", re.DOTALL | re.IGNORECASE)
    _RE_BODY = re.compile(r"<body[^>]*>(.*?)</body>", re.DOTALL | re.IGNORECASE)
    _RE_CONTENT_START = re.compile(r"<(main|article|h1|h2|section|p)\b", re.IGNORECASE)
    # Replace <img> tags with data URI src by their alt text (or nothing).
    _RE_DATA_URI_IMG = re.compile(
        r'<img\b[^>]*\bsrc=["\']data:[^"\']*["\'][^>]*>',
        re.IGNORECASE | re.DOTALL,
    )
    # Strip data URI attributes from other tags (source srcset, video poster, etc.).
    _RE_DATA_URI_ATTR = re.compile(
        r'\s+(?:srcset|poster|data-[-a-zA-Z0-9]*)=["\']data:[^"\']*["\']',
        re.IGNORECASE,
    )
    _RE_DATA_URI_CSS = re.compile(
        r'background-image\s*:\s*url\(["\']?data:[^"\')]+["\']?\)',
        re.IGNORECASE,
    )

    @staticmethod
    def _replace_data_img(match: re.Match) -> str:
        m = re.search(r'(?:^|\s)alt="([^"]*)"|(?:^|\s)alt=\'([^\']*)\'', match.group(0), re.IGNORECASE)
        return (m.group(1) or m.group(2)) if m else ""

    @staticmethod
    def _qt_html_to_markdown(raw: str) -> str:
        doc = QTextDocument()
        doc.setHtml(raw)
        return doc.toMarkdown().strip()

    @classmethod
    def _text_from_html(cls, raw: str) -> str:
        """Best-effort text extraction from raw HTML, preserving links as Markdown."""
        text = cls._RE_SCRIPT.sub("", raw)
        text = cls._RE_STYLE.sub("", text)
        text = cls._RE_DATA_URI_IMG.sub(cls._replace_data_img, text)
        text = cls._RE_DATA_URI_ATTR.sub("", text)
        text = cls._RE_DATA_URI_CSS.sub("", text)

        md = cls._qt_html_to_markdown(text)
        if md:
            return md

        body_match = cls._RE_BODY.search(text)
        body = body_match.group(1) if body_match is not None else text
        start_match = cls._RE_CONTENT_START.search(body)
        if start_match is not None and start_match.start() > 0:
            body = body[start_match.start():]

        narrowed = f"<html><body>{body}</body></html>"
        md = cls._qt_html_to_markdown(narrowed)
        if md:
            return md

        doc = QTextDocument()
        doc.setHtml(narrowed)
        return doc.toPlainText().strip()

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
        pdf_cache_max_bytes: int = 64 * 1024 * 1024,
        pdf_max_render_pages: int = 10,
        pdf_cache_ttl_s: int = 600,
    ):
        self._timeout_ms = timeout_ms
        self._user_agent = user_agent
        self._persist_cookies = persist_cookies
        self._storage_path = storage_path
        self._pdf_cache_max_bytes = pdf_cache_max_bytes
        self._pdf_max_render_pages = pdf_max_render_pages
        self._pdf_cache_ttl_s = pdf_cache_ttl_s
        self._pdf_cache: "OrderedDict[str, tuple[bytes, float]]" = OrderedDict()
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
        self._pages: list[QWebEnginePage] = []
        self._render_profile = QWebEngineProfile()
        if self._user_agent:
            self._render_profile.setHttpUserAgent(self._user_agent)
        # No PDF viewer: navigating a PDF URL triggers a download instead,
        # which is how PDFs are fetched through the real browser engine.
        self._render_profile.settings().setAttribute(
            QWebEngineSettings.WebAttribute.PdfViewerEnabled, False
        )
        # fetch_image shares cookies set during extraction.
        self._profile.cookieStore().cookieAdded.connect(
            self._render_profile.cookieStore().setCookie
        )

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
                no_proxy=cls._parse_no_proxy(os.environ.get("NO_PROXY") or os.environ.get("no_proxy")),
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

    def _build_url_opener(self, proxies: dict[str, str]) -> urllib.request.OpenerDirector:
        return urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=self._ssl_context),
            urllib.request.ProxyHandler(proxies),
        )

    @staticmethod
    def _no_proxy_match(hostname: str, no_proxy: tuple[str, ...]) -> bool:
        """Match *hostname* against no_proxy entries ("*" or DNS suffixes);
        urllib.request.proxy_bypass() has a platform-dependent signature."""
        for entry in no_proxy:
            name = entry.lstrip(".").lower()
            if not name:
                continue
            if name == "*" or hostname == name or hostname.endswith("." + name):
                return True
        return False

    def _should_bypass_proxy(self, url: str) -> bool:
        if self._proxy_config is None or not self._proxy_config.no_proxy:
            return False
        hostname = urllib.parse.urlsplit(url).hostname
        if not hostname:
            return False
        return self._no_proxy_match(hostname, self._proxy_config.no_proxy)

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

    @property
    def pdf_max_render_pages(self) -> int:
        return self._pdf_max_render_pages

    def _urlopen(self, request: urllib.request.Request, timeout: int | float):
        opener = self._direct_url_opener if self._should_bypass_proxy(request.full_url) else self._proxy_url_opener
        return opener.open(request, timeout=timeout)

    def _cleanup(self):
        self._pdf_cache.clear()
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
        if self._render_profile is not None:
            try:
                if shiboken6.isValid(self._render_profile):
                    shiboken6.delete(self._render_profile)
            except RuntimeError:
                pass
            self._render_profile = None

    def _head_content_type(self, url: str, timeout: int = 10) -> str:
        """Return the lowercase Content-Type of *url* via a HEAD request, or ""."""
        if urllib.parse.urlparse(url).scheme not in ("http", "https"):
            return ""
        try:
            req = urllib.request.Request(url, method="HEAD")
            req.add_header("User-Agent", self._http_user_agent)
            with self._urlopen(req, timeout=timeout) as resp:
                if "Content-Type" not in resp.headers:
                    return ""
                return resp.headers.get_content_type().lower()
        except Exception:
            return ""

    def detect_url_kind(self, url: str, timeout: int = 10) -> str:
        """Classify *url* as "pdf", "image", or "page".

        A HEAD Content-Type is authoritative when available (a suffix can lie:
        dynamic routes may serve HTML from URLs ending in .png or .pdf); the
        suffix is only a fallback for when HEAD fails or the scheme is not
        http(s).
        """
        ct = self._head_content_type(url, timeout)
        if "application/pdf" in ct:
            return "pdf"
        if ct.startswith("image/"):
            return "image"
        # octet-stream means "no idea", let the suffix decide below.
        if ct and ct != "application/octet-stream":
            return "page"

        path = urllib.parse.urlparse(url).path.rstrip("/").lower()
        if path.endswith(".pdf"):
            return "pdf"
        if path.endswith(_IMAGE_URL_SUFFIXES):
            return "image"
        return "page"

    def detect_pdf_url(self, url: str, timeout: int = 10) -> bool:
        """Check if *url* points to a PDF."""
        return self.detect_url_kind(url, timeout) == "pdf"

    @classmethod
    def _looks_like_neterror_page(cls, result: _ExtractionResult) -> bool:
        if not result.html:
            return False
        return any(marker in result.html for marker in cls._NETERROR_MARKERS)

    def _extract_via_http_fallback(self, url: str) -> tuple[_ExtractionResult | None, str | None]:
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", self._http_user_agent)
            req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
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
                return _ExtractionResult(
                    url=resp.geturl(),
                    title=title,
                    text=text,
                    html=raw_html,
                    error="",
                ), None
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

        result = result_holder[0] if result_holder else _ExtractionResult(
            url=url, error="Extraction failed: no result received"
        )

        if result.error and self._looks_like_neterror_page(result):
            fallback_result, fallback_error = self._extract_via_http_fallback(result.url or url)
            if fallback_result is not None and fallback_result.text.strip():
                return fallback_result
            if fallback_error:
                result.error = f"{result.error}; HTTP fallback failed: {fallback_error}"

        return result

    def _cache_get_pdf(self, url: str) -> bytes | None:
        entry = self._pdf_cache.get(url)
        if entry is None:
            return None
        data, ts = entry
        if time.monotonic() - ts > self._pdf_cache_ttl_s:
            del self._pdf_cache[url]
            return None
        self._pdf_cache.move_to_end(url)
        return data

    def _cache_put_pdf(self, url: str, data: bytes):
        if len(data) > self._pdf_cache_max_bytes:
            return
        self._pdf_cache[url] = (data, time.monotonic())
        self._pdf_cache.move_to_end(url)
        total = sum(len(d) for d, _ in self._pdf_cache.values())
        while total > self._pdf_cache_max_bytes and len(self._pdf_cache) > 1:
            _, (old_data, _) = self._pdf_cache.popitem(last=False)
            total -= len(old_data)

    @staticmethod
    def _pdf_probe_forces_download(value) -> bool:
        """Decide from the JSON-stringified [contentType, bodyPrefix] page
        probe whether to force the PDF download. Returns False only for an
        HTML page without the PDF signature (an interstitial or error page
        to wait out); anything else forces the download.
        """
        try:
            probe = json.loads(value) if isinstance(value, str) else None
        except ValueError:
            probe = None
        if isinstance(probe, list) and len(probe) == 2:
            mime, prefix = probe
            if mime in ("text/html", "application/xhtml+xml") and not prefix.startswith("%PDF-"):
                return False
        return True

    def _download_pdf_via_browser(
        self,
        url: str,
        *,
        deadline: float | None = None,
    ) -> tuple[bytes | None, str | None]:
        """Download *url* through the real browser engine; return (pdf_bytes, error).

        Uses the same Chromium network stack and cookie store as page/image
        fetching, so scripted interstitials complete on their own.
        PdfViewerEnabled is off on the render profile, so navigating a PDF
        normally triggers a download instead of opening the viewer. If a
        server labels PDF bytes as renderable text, the loaded page is probed
        for the PDF signature and explicitly downloaded through the same page.
        """
        if deadline is None:
            deadline = time.monotonic() + self._timeout_ms / 1000.0
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            return None, "PDF download timed out"

        tmpdir = QTemporaryDir()
        if not tmpdir.isValid():
            return None, "PDF download failed: could not create a temporary directory"

        state: dict = {
            "data": None,
            "error": "PDF download failed",
            "request": None,
            "done": False,
            "forced": False,
            "probe": None,
        }
        # close() runs only after extraction has stopped.
        assert self._render_profile is not None
        page = QWebEnginePage(self._render_profile)
        self._pages.append(page)
        loop = QEventLoop()
        timer = QTimer(page)
        timer.setSingleShot(True)
        timer.setInterval(max(1, remaining_ms))

        def request_finished(req: QWebEngineDownloadRequest) -> bool:
            try:
                return not shiboken6.isValid(req) or req.isFinished()
            except RuntimeError:
                return True

        def finish(error: str | None = None, *, cancel_download: bool = False):
            if state["done"]:
                return
            state["done"] = True
            if error:
                state["error"] = error
            req = state["request"]
            if cancel_download and req is not None and not request_finished(req):
                try:
                    req.cancel()
                except RuntimeError:
                    pass
            loop.quit()

        def on_timeout():
            detail = ""
            probe = state["probe"]
            if isinstance(probe, str):
                try:
                    probe = json.loads(probe)
                except ValueError:
                    probe = None
                if isinstance(probe, list) and len(probe) == 2:
                    ctype, prefix = probe
                    detail = (
                        f'; last seen: Content-Type "{ctype}", '
                        f'page starts: "{prefix}"'
                    )
            finish(
                "PDF download timed out (the URL may not be a PDF, "
                "or an intermediate page did not finish loading in time"
                f"{detail}). {_RETRY_HINT}",
                cancel_download=True,
            )

        def on_state(download_state):
            req = state["request"]
            if state["done"] or req is None:
                return
            if download_state == QWebEngineDownloadRequest.DownloadState.DownloadCompleted:
                path = os.path.join(req.downloadDirectory(), req.downloadFileName())
                try:
                    with open(path, "rb") as file:
                        data = file.read(_PDF_DOWNLOAD_MAX_BYTES + 1)
                except OSError as e:
                    finish(f"PDF download failed: {e}")
                    return
                if len(data) > _PDF_DOWNLOAD_MAX_BYTES:
                    finish(
                        f"PDF exceeds {_PDF_DOWNLOAD_MAX_BYTES // (1024 * 1024)} MB"
                    )
                elif not data.startswith(_PDF_MAGIC):
                    finish(
                        "The URL did not return a PDF document "
                        "(it may be an error page)"
                    )
                else:
                    state["data"] = data
                    finish()
            elif download_state in (
                QWebEngineDownloadRequest.DownloadState.DownloadCancelled,
                QWebEngineDownloadRequest.DownloadState.DownloadInterrupted,
            ):
                detail = (
                    req.interruptReasonString()
                    if download_state
                    == QWebEngineDownloadRequest.DownloadState.DownloadInterrupted
                    else ""
                )
                finish(
                    "PDF download was interrupted" + (f": {detail}" if detail else "")
                )

        def on_size_guard():
            req = state["request"]
            if req is not None and req.receivedBytes() > _PDF_DOWNLOAD_MAX_BYTES:
                finish(
                    f"PDF exceeds {_PDF_DOWNLOAD_MAX_BYTES // (1024 * 1024)} MB",
                    cancel_download=True,
                )

        def on_download(req: QWebEngineDownloadRequest):
            if state["done"] or req.page() != page:
                return
            active = state["request"]
            if active is not None and not request_finished(active):
                req.cancel()
                return
            req.setDownloadDirectory(tmpdir.path())
            req.setDownloadFileName("document.pdf")
            state["request"] = req
            req.stateChanged.connect(on_state)
            req.receivedBytesChanged.connect(on_size_guard)
            req.accept()

        def on_load_started():
            state["forced"] = False

        def on_load_finished(ok: bool):
            req = state["request"]
            if state["done"] or (req is not None and not request_finished(req)):
                return
            if not ok:
                state["forced"] = True
                page.download(page.url())
                return

            def on_probe(value):
                if state["done"] or state["forced"] or page.isLoading():
                    return
                state["probe"] = value
                if not self._pdf_probe_forces_download(value):
                    return
                state["forced"] = True
                page.download(page.url())

            page.runJavaScript(
                # runJavaScript results cannot carry JS arrays; stringify in-page.
                "JSON.stringify([document.contentType || '', "
                "(document.body && document.body.innerText || '').slice(0, 32)])",
                _JS_WORLD,
                on_probe,
            )

        try:
            self._render_profile.downloadRequested.connect(on_download)
            page.loadStarted.connect(on_load_started)
            page.loadFinished.connect(on_load_finished)
            timer.timeout.connect(on_timeout)
            timer.start()
            page.load(QUrl(url))
            loop.exec()
        finally:
            state["done"] = True
            timer.stop()
            req = state["request"]
            if req is not None and shiboken6.isValid(req):
                if not request_finished(req):
                    req.cancel()
                for signal, slot in (
                    (req.stateChanged, on_state),
                    (req.receivedBytesChanged, on_size_guard),
                ):
                    try:
                        signal.disconnect(slot)
                    except (RuntimeError, TypeError):
                        pass
            try:
                self._render_profile.downloadRequested.disconnect(on_download)
            except (RuntimeError, TypeError):
                pass
            if shiboken6.isValid(page):
                shiboken6.delete(page)
            try:
                self._pages.remove(page)
            except ValueError:
                pass

        return state["data"], (None if state["data"] is not None else state["error"])

    def _load_pdf_document(
        self,
        url_or_path: str,
        *,
        deadline: float | None = None,
    ) -> tuple[QPdfDocument | None, tuple | None, str]:
        """Load a PDF into a QPdfDocument; return (doc, keepalive, error).

        http(s) PDFs are downloaded through the real browser engine (see
        _download_pdf_via_browser) and cached in memory for
        self._pdf_cache_ttl_s seconds so pagination does not re-download;
        ftp falls back to urllib.
        The returned keepalive holds the byte buffer the document reads
        from; keep it referenced for the document's lifetime.
        """
        url_or_path = _as_url(url_or_path)
        parsed = urllib.parse.urlparse(url_or_path)
        doc = QPdfDocument()
        keepalive = None

        try:
            if deadline is not None and time.monotonic() >= deadline:
                return None, None, "PDF operation timed out"
            if parsed.scheme in ("http", "https"):
                data = self._cache_get_pdf(url_or_path)
                if data is None:
                    data, error = self._download_pdf_via_browser(
                        url_or_path,
                        deadline=deadline,
                    )
                    if data is None:
                        return None, None, error or "PDF download failed"
                    self._cache_put_pdf(url_or_path, data)
                byte_array = QByteArray(data)
                buffer = QBuffer(byte_array)
                buffer.open(QIODevice.OpenModeFlag.ReadOnly)
                doc.load(buffer)
                keepalive = (byte_array, buffer)
            elif parsed.scheme == "ftp":
                req = urllib.request.Request(url_or_path)
                req.add_header("User-Agent", self._http_user_agent)
                timeout_s = self._timeout_ms / 1000.0
                if deadline is not None:
                    timeout_s = min(timeout_s, deadline - time.monotonic())
                if timeout_s <= 0:
                    return None, None, "PDF operation timed out"
                with self._urlopen(req, timeout=timeout_s) as resp:
                    data = resp.read(_PDF_DOWNLOAD_MAX_BYTES + 1)
                    if len(data) > _PDF_DOWNLOAD_MAX_BYTES:
                        return None, None, (
                            f"PDF exceeds "
                            f"{_PDF_DOWNLOAD_MAX_BYTES // (1024 * 1024)} MB"
                        )
                    byte_array = QByteArray(data)
                    buffer = QBuffer(byte_array)
                    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
                    doc.load(buffer)
                    keepalive = (byte_array, buffer)
            else:
                local_path = (
                    urllib.request.url2pathname(parsed.path)
                    if parsed.scheme == "file"
                    else url_or_path
                )
                doc.load(local_path)
        except Exception as e:
            return None, None, str(e)

        if doc.status() != QPdfDocument.Status.Ready:
            return None, None, f"Failed to load PDF: {doc.status().name}"
        return doc, keepalive, ""

    def extract_pdf(self, url_or_path: str) -> _ExtractionResult:
        """Extract text from a PDF file or URL using Qt PDF."""
        url_or_path = _as_url(url_or_path)
        result = _ExtractionResult(url=url_or_path)

        try:
            # _keepalive holds the buffer the document reads from
            doc, _keepalive, error = self._load_pdf_document(url_or_path)
            if doc is None:
                result.error = error
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

    def fetch_image(self, url: str) -> _ImageResult:
        """Load an image URL and return it as bytes.

        Remote http(s) images are rendered in the browser and rasterized as
        WebP; local files (file:// URLs and bare paths) are read directly.

        Must run on the Qt main thread.
        """
        url = _as_url(url)
        result = _ImageResult(url=url)
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme == "file":
            path = urllib.request.url2pathname(urllib.parse.urlsplit(url).path)
            ext = os.path.splitext(path)[1].lower()
            mime = _IMAGE_MIME_BY_SUFFIX.get(ext) or mimetypes.guess_type(path)[0] or ""
            if not mime.startswith("image/"):
                result.error = "not an image file"
                return result
            try:
                with open(path, "rb") as f:
                    result.data = f.read()
            except OSError as e:
                result.error = str(e)
                return result
            result.mime_type = mime
            return result
        if scheme not in ("http", "https"):
            result.error = "Only http(s) or local image URLs are supported"
            return result
        return self._rasterize_url(url)

    def _rasterize_url(self, url: str) -> _ImageResult:
        """Load *url* in the render profile and rasterize it via canvas.

        Internal helper with no URL policy of its own; callers enforce
        scheme rules. Must run on the Qt main thread.
        """
        result = _ImageResult(url=url)

        page = QWebEnginePage(self._render_profile)
        self._pages.append(page)

        loop = QEventLoop()
        state: dict = {"out": None, "final_url": "", "error": "Image render failed"}

        timeout_timer = QTimer(page)
        timeout_timer.setSingleShot(True)
        timeout_timer.setInterval(self._timeout_ms)

        poll_timer = QTimer(page)
        poll_timer.setInterval(500)

        def on_probe(value):
            if not value:  # None/empty before the injected script sets __qr
                return
            state["out"] = value
            state["final_url"] = page.url().toString()
            poll_timer.stop()
            timeout_timer.stop()
            loop.quit()

        def on_timeout():
            state["error"] = "Image render timed out"
            poll_timer.stop()
            loop.quit()

        def on_load_finished(_ok):
            # Re-inject on every navigation: pages may redirect.
            # JS budget ends 5s early so its error wins over the Qt timeout.
            js = (
                _RENDER_JS.replace("@EDGE@", str(_RENDER_MAX_EDGE))
                .replace("@ATTEMPTS@", str(max(2, (self._timeout_ms - 5000) // 500)))
                .replace("@HINT@", json.dumps(_RETRY_HINT))
            )
            page.runJavaScript(js, _JS_WORLD)

        try:
            page.loadFinished.connect(on_load_finished)
            # Qt timer drives window.__att (JS timers are clamped on hidden pages).
            poll_timer.timeout.connect(
                lambda: page.runJavaScript(
                    "window.__qr || (window.__att && window.__att(), window.__qr)",
                    _JS_WORLD,
                    on_probe,
                )
            )
            timeout_timer.timeout.connect(on_timeout)
            timeout_timer.start()
            poll_timer.start()
            page.load(QUrl(url))
            loop.exec()
        finally:
            if shiboken6.isValid(page):
                shiboken6.delete(page)
            try:
                self._pages.remove(page)
            except ValueError:
                pass

        out = state["out"]
        if not isinstance(out, str) or not out.startswith("{"):
            result.error = out if isinstance(out, str) else state["error"]
            return result

        try:
            payload = json.loads(out)
            header, b64 = payload["data"].split(",", 1)
            if not header.startswith("data:image/"):
                raise ValueError(header)
            if len(b64) > (_RENDER_MAX_BYTES * 4) // 3 + 8:
                result.error = (
                    f"Rendered image exceeds "
                    f"{_RENDER_MAX_BYTES // (1024 * 1024)} MB"
                )
                return result
            result.data = base64.b64decode(b64)
            result.mime_type = header[len("data:"):].split(";", 1)[0]
        except Exception:
            result.error = "Image render failed: bad canvas data"
            return result

        result.url = state["final_url"] or result.url
        result.width = int(payload.get("w") or 0)
        result.height = int(payload.get("h") or 0)
        return result

    @staticmethod
    def _select_pdf_pages(
        pages: list | None,
        total: int,
        limit: int,
    ) -> tuple[list[int], bool]:
        """Select sorted unique pages with memory bounded by input size + limit."""
        if pages is None or len(pages) == 0:
            count = min(total, limit)
            return list(range(1, count + 1)), total > limit

        intervals: list[tuple[int, int]] = []
        for item in pages:
            if isinstance(item, int) and not isinstance(item, bool):
                if 1 <= item <= total:
                    intervals.append((item, item))
            elif isinstance(item, str):
                parts = item.strip().split("-", 1)
                if len(parts) == 2 and all(part.isdigit() for part in parts):
                    start, end = max(1, int(parts[0])), min(total, int(parts[1]))
                    if start <= end:
                        intervals.append((start, end))
        if not intervals:
            return [], False

        intervals.sort()
        merged: list[list[int]] = []
        for start, end in intervals:
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])

        available = sum(end - start + 1 for start, end in merged)
        selected: list[int] = []
        for start, end in merged:
            remaining = limit - len(selected)
            if remaining <= 0:
                break
            selected.extend(range(start, min(end, start + remaining - 1) + 1))
        return selected, available > limit

    @staticmethod
    def _encode_pdf_page_jpeg(image: QImage) -> tuple[bytes | None, str | None]:
        """Composite a rendered PDF page on white and encode it as JPEG."""
        if image.isNull():
            return None, "PDF page render returned an empty image"

        # PDFium renders the page background transparently. JPEG has no alpha,
        # so fill behind the existing pixels in place to avoid a second
        # full-resolution image allocation and a black page background.
        if image.hasAlphaChannel():
            painter = QPainter(image)
            if not painter.isActive():
                return None, "Failed to initialize PDF page compositor"
            try:
                painter.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_DestinationOver
                )
                painter.fillRect(image.rect(), QColor("white"))
            finally:
                painter.end()

        encoded = QByteArray()
        buffer = QBuffer(encoded)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            return None, "Failed to open JPEG output buffer"

        writer = QImageWriter(buffer, b"jpeg")
        writer.setQuality(_PDF_JPEG_QUALITY)
        if writer.supportsOption(QImageIOHandler.ImageOption.OptimizedWrite):
            writer.setOptimizedWrite(True)
        if not writer.canWrite():
            return None, f"JPEG encoder unavailable: {writer.errorString()}"
        if not writer.write(image):
            return None, f"Failed to encode PDF page as JPEG: {writer.errorString()}"
        buffer.close()

        if encoded.size() > _RENDER_MAX_BYTES:
            return None, (
                f"Rendered image exceeds "
                f"{_RENDER_MAX_BYTES // (1024 * 1024)} MB"
            )
        return bytes(encoded.data()), None

    def render_pdf_pages(
        self,
        url_or_path: str,
        pages: list | None = None,
    ) -> _PdfRenderResult:
        """Render selected 1-based PDF pages directly to JPEG images.

        *pages* items may be positive integers or "a-b" range strings.
        An omitted or empty list selects the first configured number of pages.
        The load and all selected pages share one operation-wide timeout.
        Pages are rasterized by Qt PDF at _PDF_RENDER_DPI, with the long edge
        capped at _RENDER_MAX_EDGE, then encoded once as JPEG.
        Must run on the Qt main thread.
        """
        url_or_path = _as_url(url_or_path)
        result = _PdfRenderResult(url=url_or_path)
        deadline = time.monotonic() + self._timeout_ms / 1000.0

        def stop_error() -> str:
            if time.monotonic() >= deadline:
                return "PDF render timed out"
            return ""

        try:
            # _keepalive holds the buffer the document reads from
            doc, _keepalive, error = self._load_pdf_document(
                url_or_path,
                deadline=deadline,
            )
            if doc is None:
                result.error = error
                return result
            if error := stop_error():
                result.error = error
                return result

            total = doc.pageCount()
            result.page_count = total
            if total <= 0:
                result.error = "PDF document has no pages"
                return result
            if self._pdf_max_render_pages < 1:
                result.error = "pdf_max_render_pages must be at least 1"
                return result

            wanted, result.truncated = self._select_pdf_pages(
                pages,
                total,
                self._pdf_max_render_pages,
            )
            if not wanted:
                result.error = (
                    f"Requested pages are out of range "
                    f"(document has {total} pages)"
                )
                return result

            rendered_bytes = 0
            for page_no in wanted:
                if error := stop_error():
                    result.error = error
                    return result
                idx = page_no - 1
                pt = doc.pagePointSize(idx)
                scale = min(
                    _PDF_RENDER_DPI / 72.0,
                    _RENDER_MAX_EDGE / max(pt.width(), pt.height(), 1),
                )
                size = QSize(
                    min(_RENDER_MAX_EDGE, max(1, round(pt.width() * scale))),
                    min(_RENDER_MAX_EDGE, max(1, round(pt.height() * scale))),
                )
                image = doc.render(idx, size)
                if image.isNull():
                    result.error = f"Failed to render page {page_no}"
                    return result
                if error := stop_error():
                    result.error = error
                    return result

                data, encode_error = self._encode_pdf_page_jpeg(image)
                if data is None:
                    result.error = (
                        f"Failed to encode page {page_no}: "
                        f"{encode_error or 'unknown JPEG error'}"
                    )
                    return result
                if error := stop_error():
                    result.error = error
                    return result
                new_total = rendered_bytes + len(data)
                if new_total > _PDF_RENDER_MAX_TOTAL_BYTES:
                    result.truncated = True
                    break
                rendered_bytes = new_total
                result.pages.append(
                    _PdfPageImage(
                        page=page_no,
                        width=image.width(),
                        height=image.height(),
                        data=data,
                        mime_type="image/jpeg",
                    )
                )
        except Exception as e:
            result.error = str(e)

        return result

    def extract_multiple(self, urls: list[str]) -> list[_ExtractionResult]:
        return [self.extract(url) for url in urls]
