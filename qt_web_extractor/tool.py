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

import json
import urllib.request
import urllib.error
from typing import Callable, Awaitable


class Tools:
    class Valves:
        def __init__(self):
            self.server_url: str = "http://127.0.0.1:8766"
            self.api_key: str = ""

    def __init__(self):
        self.valves = self.Valves()

    def _post(self, url: str, pdf: bool | None = None) -> dict:
        payload: dict = {"url": url}
        if pdf is not None:
            payload["pdf"] = pdf
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.valves.server_url.rstrip('/')}/extract",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        if self.valves.api_key:
            req.add_header("Authorization", f"Bearer {self.valves.api_key}")
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())

    async def _emit(self, emitter, description: str, done: bool):
        if emitter:
            await emitter({"type": "status", "data": {"description": description, "done": done}})

    async def fetch_page(
        self,
        url: str,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
    ) -> str:
        """
        Fetch and render a web page with full JavaScript support.
        Uses Qt WebEngine to load and render pages, handling JavaScript,
        cookies, and dynamic content that simple HTTP requests cannot process.
        PDF URLs are detected and handled automatically.

        :param url: The URL of the web page to fetch and render.
        :return: The extracted plain text content of the rendered page.
        """
        await self._emit(__event_emitter__, f"Loading: {url}", False)
        try:
            result = self._post(url)  # let server auto-detect PDF
            error = result.get("error", "")
            title = result.get("title", "")
            text = result.get("text", "")
            if error:
                await self._emit(__event_emitter__, f"Done (warning: {error})", True)
            else:
                await self._emit(__event_emitter__, f"Loaded: {title or url}", True)
            return f"# {title}\n\n{text}" if title else text
        except urllib.error.URLError as e:
            await self._emit(__event_emitter__, f"Error: {e}", True)
            return f"Error fetching {url}: {e}"
        except Exception as e:
            await self._emit(__event_emitter__, f"Error: {e}", True)
            return f"Error fetching {url}: {e}"

    async def fetch_page_html(
        self,
        url: str,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
    ) -> str:
        """
        Fetch a web page and return its rendered HTML (after JavaScript execution).
        Useful when you need the full DOM structure of a JavaScript-rendered page.

        :param url: The URL of the web page to fetch.
        :return: The full rendered HTML of the page.
        """
        await self._emit(__event_emitter__, f"Loading HTML: {url}", False)
        try:
            result = self._post(url)
            await self._emit(__event_emitter__, f"Loaded: {result.get('title', url)}", True)
            return result.get("html", "")
        except urllib.error.URLError as e:
            await self._emit(__event_emitter__, f"Error: {e}", True)
            return f"Error fetching {url}: {e}"
        except Exception as e:
            await self._emit(__event_emitter__, f"Error: {e}", True)
            return f"Error fetching {url}: {e}"

    async def fetch_pdf(
        self,
        url: str,
        __event_emitter__: Callable[[dict], Awaitable[None]] | None = None,
    ) -> str:
        """
        Fetch a PDF document and extract its text content.
        Uses Qt PDF to parse the document and return all readable text.

        :param url: The URL (or file path) of the PDF document.
        :return: The extracted plain text content of the PDF.
        """
        await self._emit(__event_emitter__, f"Loading PDF: {url}", False)
        try:
            result = self._post(url, pdf=True)
            error = result.get("error", "")
            title = result.get("title", "")
            text = result.get("text", "")
            if error:
                await self._emit(__event_emitter__, f"Done (warning: {error})", True)
            else:
                await self._emit(__event_emitter__, f"Loaded PDF: {title or url}", True)
            return f"# {title}\n\n{text}" if title else text
        except urllib.error.URLError as e:
            await self._emit(__event_emitter__, f"Error: {e}", True)
            return f"Error fetching PDF {url}: {e}"
        except Exception as e:
            await self._emit(__event_emitter__, f"Error: {e}", True)
            return f"Error fetching PDF {url}: {e}"
