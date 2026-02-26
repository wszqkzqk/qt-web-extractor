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
import logging
import queue
import signal
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from PySide6.QtCore import QTimer

from qt_web_extractor.extractor import QtWebExtractor, _ExtractionResult

log = logging.getLogger("qt-web-extractor")


class _ExtractRequest:
    __slots__ = ("url", "pdf", "result", "done")

    def __init__(self, url: str, pdf: bool = False):
        self.url = url
        self.pdf = pdf
        self.result: _ExtractionResult | None = None
        self.done = threading.Event()


class _Handler(BaseHTTPRequestHandler):
    extract_queue: "queue.Queue[_ExtractRequest | None]"
    timeout_s: int = 40
    api_key: str = ""

    def log_message(self, fmt, *args):
        log.info(fmt, *args)

    def _check_auth(self) -> bool:
        if not self.api_key:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and auth[7:].strip() == self.api_key:
            return True
        self._send_json({"error": "unauthorized"}, 401)
        return False

    def _send_json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"status": "ok"})
            return
        if not self._check_auth():
            return
        self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._check_auth():
            return
        if self.path != "/extract":
            self._send_json({"error": "not found"}, 404)
            return

        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._send_json({"error": "empty body"}, 400)
            return
        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON"}, 400)
            return

        url = body.get("url", "").strip()
        if not url:
            self._send_json({"error": "url is required"}, 400)
            return

        pdf = body.get("pdf", None)
        if pdf is None:
            pdf = url.lower().split("?")[0].split("#")[0].endswith(".pdf")

        log.info("Extract request: %s (pdf=%s)", url, pdf)
        req = _ExtractRequest(url, pdf=pdf)
        self.extract_queue.put(req)

        if not req.done.wait(timeout=self.timeout_s):
            self._send_json({"error": "extraction timed out"}, 504)
            return

        if req.result is None:
            self._send_json({"error": "extraction failed"}, 500)
            return

        self._send_json(req.result.to_dict())


def serve(
    host: str = "127.0.0.1",
    port: int = 8766,
    timeout_ms: int = 30000,
    user_agent: str | None = None,
    api_key: str = "",
):
    """Start the extraction server. Blocks forever (runs Qt event loop)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    extractor = QtWebExtractor(timeout_ms=timeout_ms, user_agent=user_agent)
    app = extractor._app

    extract_queue: queue.Queue[_ExtractRequest | None] = queue.Queue()

    _Handler.extract_queue = extract_queue
    _Handler.timeout_s = timeout_ms // 1000 + 10
    _Handler.api_key = api_key

    server = HTTPServer((host, port), _Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    log.info("Listening on http://%s:%d", host, port)
    log.info("  timeout: %dms, auth: %s", timeout_ms, "on" if api_key else "off")

    shutting_down = False

    def handle_signal(*_):
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        log.info("Shutting down...")
        extract_queue.put(None)  # poison pill
        server.shutdown()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Qt WebEngine must run on the main thread, so we poll the queue
    # from inside the Qt event loop.
    poll_timer = QTimer()
    poll_timer.setInterval(50)

    def poll_queue():
        try:
            req = extract_queue.get_nowait()
        except queue.Empty:
            return
        if req is None:
            poll_timer.stop()
            app.quit()
            return
        result = extractor.extract_pdf(req.url) if req.pdf else extractor.extract(req.url)
        req.result = result
        req.done.set()

    poll_timer.timeout.connect(poll_queue)
    poll_timer.start()

    app.exec()

    server.server_close()
    server_thread.join(timeout=2)
    del extractor
    log.info("Shutdown complete")
