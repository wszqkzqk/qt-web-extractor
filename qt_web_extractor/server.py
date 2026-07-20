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

import base64
import ipaddress
import json
import logging
import queue
import signal
import socket
import threading
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

try:
    from importlib.metadata import version
    _server_version = version("qt-web-extractor")
except Exception:
    _server_version = "0.1.0dev"

from PySide6.QtCore import QTimer

from qt_web_extractor.extractor import QtWebExtractor, _ExtractionResult, _ImageResult, _as_url

log = logging.getLogger("qt-web-extractor")

_MCP_PROTOCOL_VERSION = "2024-11-05"

# Schemes that fetch remote content; the tool's purpose, always allowed.
_REMOTE_SCHEMES = frozenset({"http", "https", "ftp"})
# Schemes that read server-local files; gated behind --allow-local-files.
_LOCAL_SCHEMES = frozenset({"file"})
# Any other scheme (data:, javascript:, chrome:, ...) is rejected outright.


def parse_allow_cidrs(values: list[str]) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    """Parse --allow-cidr values into networks; "all" allows everything."""
    networks = []
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if not token:
                continue
            if token.lower() == "all":
                networks.extend([
                    ipaddress.IPv4Network("0.0.0.0/0"),
                    ipaddress.IPv6Network("::/0"),
                ])
                continue
            try:
                networks.append(ipaddress.ip_network(token))
            except ValueError:
                raise ValueError(f"invalid CIDR: {token!r}") from None
    return networks


def _target_ip_error(url: str, allow_cidrs) -> str | None:
    """Error message if *url*'s host resolves to a disallowed IP, else None.

    Every resolved address must be globally routable or inside an
    --allow-cidr range. Resolution happens again at connect time (in
    Chromium or urllib), so this is best-effort against honest DNS and
    cannot stop DNS rebinding; the authoritative control is network-level
    egress filtering. Redirects are followed inside Chromium and are not
    re-validated.
    """
    hostname = urllib.parse.urlsplit(url).hostname
    if not hostname:
        return "unsupported or invalid URL"
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        ips = {info[4][0] for info in infos}
    except socket.gaierror:
        log.warning("Denied URL with unresolvable host %r: %s", hostname, url)
        return "unsupported or invalid URL"
    if not ips:
        log.warning("Denied URL with unresolvable host %r: %s", hostname, url)
        return "unsupported or invalid URL"
    for raw in ips:
        ip = ipaddress.ip_address(raw)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.is_global or any(ip in net for net in allow_cidrs):
            continue
        log.warning("Denied URL %s: host resolves to non-public IP %s", url, ip)
        return "this server does not allow private or reserved IP addresses"
    return None


def _url_access_error(url: str, allow_local_files: bool, allow_cidrs=()) -> str | None:
    """Error message if the server may not access *url*, else None.

    Local files (file://, bare paths, Windows drive paths) require
    --allow-local-files; remote content (http/https/ftp) must resolve to
    public IPs unless covered by --allow-cidr; any other scheme is
    rejected.
    """
    scheme = urllib.parse.urlsplit(_as_url(url)).scheme.lower()
    if scheme in _REMOTE_SCHEMES:
        return _target_ip_error(url, allow_cidrs)
    if scheme in _LOCAL_SCHEMES:
        if allow_local_files:
            return None
        log.warning("Denied local file URL %s", url)
        return "unsupported or invalid URL"
    log.warning("Denied URL with unsupported scheme %r: %s", scheme, url)
    return f"this server does not support the {scheme!r} URL scheme"


class _ExtractRequest:
    __slots__ = ("url", "pdf", "image", "result", "done")

    def __init__(self, url: str, pdf: bool = False, image: bool = False):
        self.url = url
        self.pdf = pdf
        self.image = image
        self.result = None
        self.done = threading.Event()


class _Handler(BaseHTTPRequestHandler):
    extract_queue: "queue.Queue[_ExtractRequest | None]"
    timeout_s: int = 40
    api_key: str = ""
    extractor: QtWebExtractor

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

    def _send_empty(self, status: int = 204):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_mcp_result(self, request_id, result):
        self._send_json({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _send_mcp_error(self, request_id, code: int, message: str, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._send_json({"jsonrpc": "2.0", "id": request_id, "error": error})

    def do_GET(self):
        if self.path == "/health":
            self._send_json({"status": "ok"})
            return
        if not self._check_auth():
            return
        self._send_json({"error": "not found"}, 404)

    def _read_json_body(self) -> dict | None:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._send_json({"error": "empty body"}, 400)
            return None
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_json({"error": "invalid JSON"}, 400)
            return None

    @staticmethod
    def _is_pdf(url: str, extractor: QtWebExtractor) -> bool:
        return extractor.detect_pdf_url(url)

    def _extract_one(self, url: str, pdf: bool = False) -> _ExtractionResult | None:
        req = _ExtractRequest(url, pdf=pdf)
        self.extract_queue.put(req)
        if not req.done.wait(timeout=self.timeout_s):
            return None
        return req.result

    def _image_one(self, url: str) -> _ImageResult | None:
        req = _ExtractRequest(url, image=True)
        self.extract_queue.put(req)
        if not req.done.wait(timeout=self.timeout_s):
            return None
        return req.result

    @staticmethod
    def _mcp_tools() -> list[dict]:
        return [
            {
                "name": "fetch_url",
                "description": (
                    "Extracts content from websites, including dynamic pages, to clean Markdown."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL to extract content from.",
                        }
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "fetch_image",
                "description": (
                    "Fetches an image and returns it as image content you can see. "
                    "You can also use it to view images linked in Markdown returned by fetch_url. "
                    "Resolve site-relative links against the page URL first: e.g. "
                    "after fetching https://xxx.yyy/foo/bar.html, view "
                    "![baz](/baz/img.png) by calling this tool with "
                    "https://xxx.yyy/baz/img.png. Absolute http(s) URLs, or "
                    "file:// URLs if the server allows local files."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL of the image to fetch.",
                        }
                    },
                    "required": ["url"],
                    "additionalProperties": False,
                },
            },
        ]

    def _mcp_call_tool(self, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        if name == "fetch_url":
            return self._mcp_call_fetch_url(arguments)
        if name == "fetch_image":
            return self._mcp_call_fetch_image(arguments)
        raise ValueError("unknown tool name")

    def _mcp_call_fetch_url(self, arguments: dict) -> dict:
        url = arguments.get("url")
        if not isinstance(url, str):
            raise ValueError("arguments.url must be a string")

        url = url.strip()
        if not url:
            raise ValueError("arguments.url is required")

        kind = self.extractor.detect_url_kind(url)
        log.info("MCP fetch_url: %s (kind=%s)", url, kind)

        if kind == "image":
            # The URL actually points to an image; serve it as image content.
            return self._mcp_image_result(url)

        result = self._extract_one(url, pdf=(kind == "pdf"))

        if result is None:
            timeout_error = "extraction timed out"
            return {
                "content": [{"type": "text", "text": f"Error: {timeout_error}"}],
                "structuredContent": {
                    "url": url,
                    "title": "",
                    "markdown": "",
                    "error": timeout_error,
                },
                "isError": True,
            }

        markdown = result.text or ""
        response = {
            "url": result.url or url,
            "title": result.title,
            "markdown": markdown,
            "error": result.error,
        }

        if result.error and not markdown:
            return {
                "content": [{"type": "text", "text": f"Error: {result.error}"}],
                "structuredContent": response,
                "isError": True,
            }

        text = markdown
        if result.error:
            text = f"{markdown}\n\n[warning] {result.error}" if markdown else f"[warning] {result.error}"

        return {
            "content": [{"type": "text", "text": text}],
            "structuredContent": response,
            "isError": False,
        }

    def _mcp_call_fetch_image(self, arguments: dict) -> dict:
        url = arguments.get("url")
        if not isinstance(url, str):
            raise ValueError("arguments.url must be a string")

        url = url.strip()
        if not url:
            raise ValueError("arguments.url is required")

        return self._mcp_image_result(url)

    def _mcp_image_result(self, url: str) -> dict:
        log.info("MCP fetch_image: %s", url)
        result = self._image_one(url)

        if result is None:
            timeout_error = "image fetch timed out"
            return {
                "content": [{"type": "text", "text": f"Error: {timeout_error}"}],
                "structuredContent": _ImageResult(url=url, error=timeout_error).to_dict(),
                "isError": True,
            }

        info = result.to_dict()
        if result.error or not result.data or not result.mime_type:
            error = result.error or "no image data"
            return {
                "content": [{"type": "text", "text": f"Error: {error}"}],
                "structuredContent": info,
                "isError": True,
            }

        dims = (
            f"{result.width}x{result.height}"
            if result.width and result.height
            else "unknown dimensions"
        )
        return {
            "content": [
                {
                    "type": "image",
                    "data": base64.b64encode(result.data).decode("ascii"),
                    "mimeType": result.mime_type,
                },
                {
                    "type": "text",
                    "text": (
                        f"Image fetched: {result.url}\n"
                        f"Format: {result.mime_type}, {dims}, {len(result.data)} bytes"
                    ),
                },
            ],
            "structuredContent": info,
            "isError": False,
        }

    def _handle_mcp(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            self._send_mcp_error(None, -32600, "Invalid Request", {"reason": "empty body"})
            return

        try:
            body = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            self._send_mcp_error(None, -32700, "Parse error")
            return

        if not isinstance(body, dict):
            self._send_mcp_error(None, -32600, "Invalid Request")
            return

        has_id = "id" in body
        request_id = body.get("id")
        method = body.get("method")
        params = body.get("params", {})

        if body.get("jsonrpc") != "2.0" or not isinstance(method, str) or not method:
            self._send_mcp_error(request_id if has_id else None, -32600, "Invalid Request")
            return

        if not isinstance(params, dict):
            if has_id:
                self._send_mcp_error(request_id, -32602, "Invalid params", {"reason": "params must be an object"})
            else:
                self._send_empty()
            return

        # Ignore JSON-RPC notifications unless explicitly needed.
        if not has_id:
            self._send_empty()
            return

        if method == "initialize":
            self._send_mcp_result(
                request_id,
                {
                    "protocolVersion": _MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "qt-web-extractor",
                        "version": _server_version,
                    },
                },
            )
            return

        if method == "ping":
            self._send_mcp_result(request_id, {})
            return

        if method == "notifications/initialized":
            self._send_mcp_result(request_id, {})
            return

        if method == "tools/list":
            self._send_mcp_result(request_id, {"tools": self._mcp_tools()})
            return

        if method == "tools/call":
            try:
                result = self._mcp_call_tool(params)
            except ValueError as e:
                self._send_mcp_error(request_id, -32602, "Invalid params", {"reason": str(e)})
                return
            self._send_mcp_result(request_id, result)
            return

        self._send_mcp_error(request_id, -32601, "Method not found")

    def do_POST(self):
        if not self._check_auth():
            return

        if self.path in ("/mcp", "/mcp/"):
            self._handle_mcp()
            return

        body = self._read_json_body()
        if body is None:
            return

        # Open WebUI external web loader format: POST / with {"urls": [...]}
        if self.path in ("/", "") and "urls" in body:
            urls = body.get("urls", [])
            if not isinstance(urls, list) or not urls:
                self._send_json({"error": "urls must be a non-empty array"}, 400)
                return

            log.info("Batch extract request: %d URLs", len(urls))
            documents = []
            for url in urls:
                url = url.strip()
                if not url:
                    continue
                pdf = self._is_pdf(url, self.extractor)
                log.info("  -> %s (pdf=%s)", url, pdf)
                result = self._extract_one(url, pdf=pdf)
                if result is None:
                    documents.append({
                        "page_content": "",
                        "metadata": {"source": url, "error": "extraction timed out"},
                    })
                else:
                    documents.append({
                        "page_content": result.text,
                        "metadata": {
                            "source": result.url or url,
                            "title": result.title,
                            **({"error": result.error} if result.error else {}),
                        },
                    })
            self._send_json(documents)
            return

        # Legacy single-URL format: POST /extract with {"url": "..."}
        if self.path == "/extract":
            url = body.get("url", "").strip()
            if not url:
                self._send_json({"error": "url is required"}, 400)
                return

            pdf = body.get("pdf", None)
            if pdf is None:
                pdf = self._is_pdf(url, self.extractor)

            log.info("Extract request: %s (pdf=%s)", url, pdf)
            result = self._extract_one(url, pdf=pdf)

            if result is None:
                self._send_json({"error": "extraction timed out"}, 504)
                return

            self._send_json(result.to_dict())
            return

        self._send_json({"error": "not found"}, 404)


def serve(
    host: str = "127.0.0.1",
    port: int = 8766,
    timeout_ms: int = 30000,
    user_agent: str | None = None,
    api_key: str = "",
    proxy: str | None = None,
    allow_local_files: bool = False,
    allow_cidrs: list[str] | None = None,
):
    """Start the extraction server. Blocks forever (runs Qt event loop)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    networks = parse_allow_cidrs(allow_cidrs or [])

    extractor = QtWebExtractor(timeout_ms=timeout_ms, user_agent=user_agent, proxy=proxy)
    app = extractor._app

    extract_queue: queue.Queue[_ExtractRequest | None] = queue.Queue()

    _Handler.extract_queue = extract_queue
    _Handler.timeout_s = timeout_ms // 1000 + 10
    _Handler.api_key = api_key
    _Handler.extractor = extractor

    server = HTTPServer((host, port), _Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    log.info("Listening on http://%s:%d", host, port)
    log.info(
        "  timeout: %dms, auth: %s, proxy: %s, local files: %s, allowed CIDRs: %s",
        timeout_ms,
        "on" if api_key else "off",
        extractor.proxy_summary,
        "on" if allow_local_files else "off",
        ", ".join(str(n) for n in networks) or "(public IPs only)",
    )

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

    # Qt WebEngine must run on the main thread; poll queue from Qt event loop.
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
        try:
            # Single choke point: every request, regardless of endpoint,
            # passes through here and is subject to the URL policy.
            access_error = _url_access_error(req.url, allow_local_files, networks)
            if access_error:
                url = _as_url(req.url)  # match the normalized form of success paths
                if req.image:
                    result = _ImageResult(url=url, error=access_error)
                else:
                    result = _ExtractionResult(url=url, error=access_error)
            elif req.image:
                result = extractor.fetch_image(req.url)
            elif req.pdf:
                result = extractor.extract_pdf(req.url)
            else:
                result = extractor.extract(req.url)
        except Exception as e:
            log.exception("Extraction failed for %s", req.url)
            if req.image:
                result = _ImageResult(url=req.url, error=str(e))
            else:
                result = _ExtractionResult(url=req.url, error=str(e))
        req.result = result
        req.done.set()

    poll_timer.timeout.connect(poll_queue)
    poll_timer.start()

    app.exec()

    server.server_close()
    server_thread.join(timeout=2)
    del extractor
    log.info("Shutdown complete")
