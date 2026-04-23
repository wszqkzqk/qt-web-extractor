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
import html as html_lib
import logging
import re
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass
import shiboken6
from PySide6.QtCore import QUrl, QTimer, QEventLoop, Signal, QByteArray, QBuffer, QIODevice
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
        self.load(QUrl(url))

    def _on_load_started(self):
        self._stability_timer.stop()

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
    _UA_MODE_VALUES = {"off", "on_block", "rotate"}
    _DEFAULT_MOBILE_USER_AGENTS = (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Mobile Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
        "Mozilla/5.0 (Linux; Android 13; SM-S9180) AppleWebKit/537.36 "
        "(KHTML, like Gecko) SamsungBrowser/24.0 Chrome/120.0.0.0 Mobile Safari/537.36",
    )
    _WECHAT_HOST = "mp.weixin.qq.com"
    _WECHAT_CAPTCHA_PATH = "/mp/wappoc_appmsgcaptcha"
    _WECHAT_CAPTCHA_MARKERS = ("环境异常", "完成验证后即可继续访问", "去验证")
    _GENERIC_CHALLENGE_URL_MARKERS = ("wappoc_appmsgcaptcha", "/captcha", "/challenge", "cf_chl_")
    _GENERIC_CHALLENGE_TEXT_MARKERS = (
        "verify you are human",
        "security check",
        "access denied",
        "complete verification",
        "enable javascript and cookies to continue",
        "robot check",
    )

    def __init__(
        self,
        timeout_ms: int = 30000,
        user_agent: str | None = None,
        ua_mode: str = "on_block",
        user_agents: list[str] | tuple[str, ...] | None = None,
        persist_cookies: bool = False,
        storage_path: str | None = None,
        proxy: str | None = None,
    ):
        self._timeout_ms = timeout_ms
        self._user_agent = user_agent
        self._ua_mode = self._normalize_ua_mode(ua_mode)
        self._custom_user_agents = self._normalize_user_agent_list(user_agents)
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

    @classmethod
    def _normalize_ua_mode(cls, value: str | None) -> str:
        mode = (value or "on_block").strip().lower()
        if mode not in cls._UA_MODE_VALUES:
            allowed = ", ".join(sorted(cls._UA_MODE_VALUES))
            raise ValueError(f"Invalid ua_mode '{value}'. Expected one of: {allowed}")
        return mode

    @staticmethod
    def _normalize_user_agent_list(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
        if not values:
            return ()
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            ua = value.strip()
            if not ua or ua in seen:
                continue
            seen.add(ua)
            normalized.append(ua)
        return tuple(normalized)

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
        opener = self._direct_url_opener if self._should_bypass_proxy(request.full_url) else self._proxy_url_opener
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

    @classmethod
    def _is_wechat_url(cls, url: str) -> bool:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        return host == cls._WECHAT_HOST or host.endswith(f".{cls._WECHAT_HOST}")

    @classmethod
    def _looks_like_wechat_captcha(cls, result: _ExtractionResult) -> bool:
        current_url = result.url or ""
        if cls._WECHAT_CAPTCHA_PATH in current_url:
            return True
        text = result.text or ""
        return all(marker in text for marker in cls._WECHAT_CAPTCHA_MARKERS)

    @classmethod
    def _looks_like_generic_challenge(cls, result: _ExtractionResult) -> bool:
        current_url = (result.url or "").lower()
        text = (result.text or "").lower()
        html = (result.html or "").lower()

        if any(marker in current_url for marker in cls._GENERIC_CHALLENGE_URL_MARKERS):
            return True

        # Keep text-only detection conservative to avoid false positives on normal pages.
        if len(text) > 1000:
            return False
        return any(marker in text or marker in html for marker in cls._GENERIC_CHALLENGE_TEXT_MARKERS)

    def _looks_blocked(self, requested_url: str, result: _ExtractionResult) -> bool:
        if self._is_wechat_url(requested_url) and self._looks_like_wechat_captcha(result):
            return True
        return self._looks_like_generic_challenge(result)

    def _default_accept_language(self, url: str) -> str:
        if self._is_wechat_url(url):
            return "zh-CN,zh;q=0.9,en;q=0.8"
        return "en-US,en;q=0.9"

    def _candidate_user_agents(self, current_ua: str) -> list[str]:
        source = self._custom_user_agents or self._DEFAULT_MOBILE_USER_AGENTS
        candidates: list[str] = []
        for ua in source:
            if ua and ua != current_ua and ua not in candidates:
                candidates.append(ua)
        return candidates

    def _extract_via_http_fallback(
        self,
        url: str,
        *,
        user_agent: str | None = None,
        accept_language: str | None = None,
    ) -> tuple[_ExtractionResult | None, str | None]:
        try:
            req = urllib.request.Request(url)
            req.add_header("User-Agent", user_agent or self._http_user_agent)
            req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
            req.add_header("Accept-Language", accept_language or self._default_accept_language(url))
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

    def _extract_with_current_profile(self, url: str) -> _ExtractionResult:
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

    def _extract_with_user_agent(self, url: str, user_agent: str) -> _ExtractionResult:
        assert self._profile is not None, "Profile has been cleaned up"
        original_ua = self._profile.httpUserAgent()
        switched = bool(user_agent and user_agent != original_ua)
        if switched:
            self._profile.setHttpUserAgent(user_agent)
            self._http_user_agent = user_agent

        try:
            return self._extract_with_current_profile(url)
        finally:
            if switched and self._profile is not None:
                self._profile.setHttpUserAgent(original_ua)
                self._http_user_agent = original_ua

    def _post_process_result(self, requested_url: str, result: _ExtractionResult, user_agent: str) -> _ExtractionResult:
        if result.error and self._looks_like_neterror_page(result):
            fallback_result, fallback_error = self._extract_via_http_fallback(
                result.url or requested_url,
                user_agent=user_agent,
            )
            if fallback_result is not None and fallback_result.text.strip():
                return fallback_result
            if fallback_error:
                result.error = f"{result.error}; HTTP fallback failed: {fallback_error}"

        if self._looks_blocked(requested_url, result):
            fallback_result, fallback_error = self._extract_via_http_fallback(
                requested_url,
                user_agent=user_agent,
            )
            if (
                fallback_result is not None
                and fallback_result.text.strip()
                and not self._looks_blocked(requested_url, fallback_result)
            ):
                return fallback_result
            if fallback_error:
                msg = f"HTTP fallback failed during anti-bot recovery: {fallback_error}"
                result.error = f"{result.error}; {msg}" if result.error else msg

        return result

    def extract(self, url: str) -> _ExtractionResult:
        assert self._profile is not None, "Profile has been cleaned up"
        primary_ua = self._profile.httpUserAgent()
        fallback_uas = self._candidate_user_agents(primary_ua)

        if self._ua_mode == "rotate":
            sequence = [primary_ua, *fallback_uas]
        else:
            sequence = [primary_ua]

        last_result: _ExtractionResult | None = None
        for ua in sequence:
            if ua != primary_ua:
                log.info("Trying alternate UA for %s", url)
            result = self._extract_with_user_agent(url, ua)
            result = self._post_process_result(url, result, ua)
            last_result = result
            if not self._looks_blocked(url, result):
                return result

        if self._ua_mode == "on_block" and self._looks_blocked(url, last_result or _ExtractionResult()):
            for ua in fallback_uas:
                log.info("Blocked page detected for %s, retrying with alternate UA", url)
                result = self._extract_with_user_agent(url, ua)
                result = self._post_process_result(url, result, ua)
                last_result = result
                if not self._looks_blocked(url, result):
                    return result

        return last_result if last_result is not None else _ExtractionResult(
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
