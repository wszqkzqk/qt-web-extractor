import base64
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from qt_web_extractor.extractor import (
    QtWebExtractor,
    _ExtractionResult,
    _ImageResult,
    _PdfPageImage,
    _PdfRenderResult,
)
from qt_web_extractor.server import _Handler, _validate_pages_arg


def _handler(*, kind: str = "page", max_pages: int = 10) -> _Handler:
    handler = object.__new__(_Handler)
    handler.extractor = SimpleNamespace(
        detect_url_kind=Mock(return_value=kind),
        pdf_max_render_pages=max_pages,
    )
    return handler


class PageArgumentTests(unittest.TestCase):
    def test_page_items_are_validated_and_preserved(self):
        self.assertEqual(_validate_pages_arg([]), ())
        self.assertEqual(_validate_pages_arg([1, "2-4", 9]), (1, "2-4", 9))

    def test_invalid_page_arguments_are_rejected(self):
        invalid = (
            None,
            (),
            [True],
            [0],
            [-1],
            ["0-1"],
            ["2-1"],
            ["1"],
            ["1-a"],
            [1.5],
        )
        for pages in invalid:
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                _validate_pages_arg(pages)

    def test_empty_and_omitted_pages_share_the_default_selection(self):
        handler = _handler(max_pages=3)
        handler._mcp_pdf_image_result = Mock(
            side_effect=lambda _url, pages: QtWebExtractor._select_pdf_pages(
                pages, total=8, limit=3
            )
        )

        omitted = handler._mcp_call_fetch_pdf(
            {"url": "https://example.test/document.pdf", "mode": "image"}
        )
        empty = handler._mcp_call_fetch_pdf(
            {
                "url": "https://example.test/document.pdf",
                "mode": "image",
                "pages": [],
            }
        )

        self.assertEqual(omitted, ([1, 2, 3], True))
        self.assertEqual(empty, omitted)

        fetch_pdf = next(
            tool for tool in handler._mcp_tools() if tool["name"] == "fetch_pdf"
        )
        description = fetch_pdf["inputSchema"]["properties"]["pages"]["description"]
        self.assertIn("omitted or empty", description)


class McpContractTests(unittest.TestCase):
    @staticmethod
    def _handle_request(
        handler: _Handler,
        body: dict,
        headers: dict | None = None,
    ) -> None:
        encoded = json.dumps(body).encode("utf-8")
        handler.headers = {
            "Content-Length": str(len(encoded)),
            **(headers or {}),
        }
        handler.rfile = io.BytesIO(encoded)
        handler._handle_mcp()

    def test_initialize_advertises_structured_content_protocol(self):
        handler = _handler()
        handler._send_mcp_result = Mock()

        self._handle_request(
            handler,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            {"MCP-Protocol-Version": "2024-11-05"},
        )

        request_id, result = handler._send_mcp_result.call_args.args
        self.assertEqual(request_id, 1)
        self.assertEqual(result["protocolVersion"], "2025-06-18")

    def test_notification_returns_transport_status(self):
        for params, status in (({}, 202), ([], 400)):
            with self.subTest(params=params):
                handler = _handler()
                handler._send_empty = Mock()
                self._handle_request(
                    handler,
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/initialized",
                        "params": params,
                    },
                    {"MCP-Protocol-Version": "2025-06-18"},
                )
                handler._send_empty.assert_called_once_with(status)

    def test_non_initialize_rejects_explicit_wrong_protocol_version(self):
        handler = _handler()
        handler._send_json = Mock()
        handler._send_mcp_result = Mock()

        self._handle_request(
            handler,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
            },
            {"MCP-Protocol-Version": "2024-11-05"},
        )

        handler._send_json.assert_called_once_with(
            {"error": "unsupported MCP protocol version"},
            400,
        )
        handler._send_mcp_result.assert_not_called()

    def test_get_mcp_returns_method_not_allowed(self):
        handler = _handler()
        handler.path = "/mcp"
        handler.headers = {}
        handler._check_auth = Mock(return_value=True)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        handler.do_GET()

        handler.send_response.assert_called_once_with(405)
        handler.send_header.assert_any_call("Allow", "POST")
        handler.send_header.assert_any_call("Content-Length", "0")
        handler.end_headers.assert_called_once_with()

    def test_get_mcp_rejects_explicit_wrong_protocol_version(self):
        handler = _handler()
        handler.path = "/mcp"
        handler.headers = {"MCP-Protocol-Version": "2024-11-05"}
        handler._check_auth = Mock(return_value=True)
        handler._send_json = Mock()
        handler.send_response = Mock()

        handler.do_GET()

        handler._send_json.assert_called_once_with(
            {"error": "unsupported MCP protocol version"},
            400,
        )
        handler.send_response.assert_not_called()

    def test_mcp_get_and_post_reject_origin(self):
        for method in ("do_GET", "do_POST"):
            with self.subTest(method=method):
                handler = _handler()
                handler.path = "/mcp"
                handler.headers = {"Origin": "https://browser.example"}
                handler._send_json = Mock()
                handler._check_auth = Mock(return_value=True)

                getattr(handler, method)()

                handler._send_json.assert_called_once_with(
                    {"error": "Origin is not allowed for MCP requests"},
                    403,
                )
                handler._check_auth.assert_not_called()

    def test_tool_dispatch_rejects_bad_arguments_and_unknown_tools(self):
        handler = _handler()
        with self.assertRaisesRegex(ValueError, "arguments must be an object"):
            handler._mcp_call_tool({"name": "fetch_url", "arguments": []})
        with self.assertRaisesRegex(ValueError, "unknown tool name"):
            handler._mcp_call_tool({"name": "missing", "arguments": {}})

    def test_all_tools_reject_unknown_arguments(self):
        valid_arguments = {
            "fetch_url": {"url": "https://example.test/"},
            "fetch_image": {"url": "https://example.test/image.png"},
            "fetch_pdf": {
                "url": "https://example.test/document.pdf",
                "mode": "image",
                "pages": [1],
            },
        }
        for name, arguments in valid_arguments.items():
            with self.subTest(tool=name), self.assertRaisesRegex(
                ValueError, "unknown tool argument"
            ):
                _handler()._mcp_call_tool(
                    {
                        "name": name,
                        "arguments": {**arguments, "extra": True},
                    }
                )

    def test_tool_call_allows_top_level_meta(self):
        handler = _handler()
        handler._mcp_call_fetch_url = Mock(return_value={"ok": True})

        result = handler._mcp_call_tool(
            {
                "name": "fetch_url",
                "arguments": {"url": "https://example.test/"},
                "_meta": {"progressToken": "token"},
            }
        )

        self.assertEqual(result, {"ok": True})
        handler._mcp_call_fetch_url.assert_called_once_with(
            {"url": "https://example.test/"}
        )

    def test_unknown_tool_argument_returns_invalid_params(self):
        handler = _handler()
        handler._send_mcp_error = Mock()
        handler._send_mcp_result = Mock()

        self._handle_request(
            handler,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "fetch_url",
                    "arguments": {
                        "url": "https://example.test/",
                        "extra": True,
                    },
                },
            },
        )

        handler._send_mcp_error.assert_called_once_with(
            7,
            -32602,
            "Invalid params",
            {"reason": "unknown tool argument(s): 'extra'"},
        )
        handler._send_mcp_result.assert_not_called()

    def test_public_tools_reject_missing_or_blank_urls(self):
        for name in ("fetch_url", "fetch_image", "fetch_pdf"):
            with self.subTest(tool=name, value=None), self.assertRaisesRegex(
                ValueError, "arguments.url must be a string"
            ):
                _handler()._mcp_call_tool({"name": name, "arguments": {}})
            with self.subTest(tool=name, value="blank"), self.assertRaisesRegex(
                ValueError, "arguments.url is required"
            ):
                _handler()._mcp_call_tool(
                    {"name": name, "arguments": {"url": "   "}}
                )

    def test_fetch_pdf_rejects_invalid_mode_and_pages(self):
        handler = _handler()
        with self.assertRaisesRegex(ValueError, 'mode must be "text" or "image"'):
            handler._mcp_call_fetch_pdf(
                {"url": "https://example.test/a.pdf", "mode": "binary"}
            )
        with self.assertRaisesRegex(ValueError, "pages items"):
            handler._mcp_call_fetch_pdf(
                {
                    "url": "https://example.test/a.pdf",
                    "mode": "image",
                    "pages": [False],
                }
            )

    def test_text_mode_rejects_pages(self):
        handler = _handler()
        for arguments in (
            {"url": "https://example.test/a.pdf", "pages": [1]},
            {"url": "https://example.test/a.pdf", "mode": "text", "pages": []},
        ):
            with self.subTest(arguments=arguments):
                with self.assertRaisesRegex(ValueError, 'only applies to "image" mode'):
                    handler._mcp_call_fetch_pdf(arguments)

    def test_fetch_url_routes_page_pdf_and_image_types(self):
        page_result = _ExtractionResult(url="https://example.test/", text="page")
        page_handler = _handler(kind="page")
        page_handler._extract_one = Mock(return_value=page_result)
        page_handler._mcp_call_fetch_url({"url": " https://example.test/ "})
        page_handler._extract_one.assert_called_once_with(
            "https://example.test/", pdf=""
        )

        pdf_result = _ExtractionResult(url="https://example.test/a.pdf", text="pdf")
        pdf_handler = _handler(kind="pdf")
        pdf_handler._extract_one = Mock(return_value=pdf_result)
        pdf_handler._mcp_call_fetch_url({"url": "https://example.test/a.pdf"})
        pdf_handler._extract_one.assert_called_once_with(
            "https://example.test/a.pdf", pdf="text"
        )

        image_handler = _handler(kind="image")
        image_handler._mcp_image_result = Mock(return_value={"route": "image"})
        response = image_handler._mcp_call_fetch_url(
            {"url": "https://example.test/a.png"}
        )
        self.assertEqual(response, {"route": "image"})
        image_handler._mcp_image_result.assert_called_once_with(
            "https://example.test/a.png"
        )

    def test_fetch_url_timeout_has_stable_error_envelope(self):
        handler = _handler(kind="page")
        handler._extract_one = Mock(return_value=None)

        response = handler._mcp_call_fetch_url({"url": "https://example.test/slow"})

        self.assertTrue(response["isError"])
        self.assertEqual(
            response["content"],
            [{"type": "text", "text": "Error: extraction timed out"}],
        )
        self.assertEqual(
            response["structuredContent"],
            {
                "url": "https://example.test/slow",
                "title": "",
                "markdown": "",
                "error": "extraction timed out",
            },
        )

    def test_text_error_is_hard_without_content_and_warning_with_content(self):
        handler = _handler(kind="page")
        handler._extract_one = Mock(
            return_value=_ExtractionResult(error="load failed")
        )
        hard_error = handler._mcp_call_fetch_url(
            {"url": "https://example.test/failure"}
        )
        self.assertTrue(hard_error["isError"])
        self.assertEqual(hard_error["structuredContent"]["error"], "load failed")

        handler._extract_one.return_value = _ExtractionResult(
            url="https://example.test/partial",
            title="Partial",
            text="usable text",
            error="load timed out",
        )
        warning = handler._mcp_call_fetch_url(
            {"url": "https://example.test/partial"}
        )
        self.assertFalse(warning["isError"])
        self.assertEqual(
            warning["content"],
            [{"type": "text", "text": "usable text\n\n[warning] load timed out"}],
        )
        self.assertEqual(warning["structuredContent"]["markdown"], "usable text")
        self.assertEqual(warning["structuredContent"]["error"], "load timed out")

    def test_image_timeout_and_error_have_structured_metadata(self):
        handler = _handler()
        handler._image_one = Mock(return_value=None)
        timeout = handler._mcp_image_result("https://example.test/slow.png")
        self.assertTrue(timeout["isError"])
        self.assertEqual(timeout["structuredContent"]["error"], "image fetch timed out")
        self.assertEqual(timeout["structuredContent"]["size_bytes"], 0)

        handler._image_one.return_value = _ImageResult(
            url="https://example.test/bad.png", error="decode failed"
        )
        failure = handler._mcp_image_result("https://example.test/bad.png")
        self.assertTrue(failure["isError"])
        self.assertEqual(failure["content"][0]["text"], "Error: decode failed")
        self.assertEqual(failure["structuredContent"]["mime_type"], "")

    def test_pdf_image_timeout_and_error_have_structured_metadata(self):
        handler = _handler()
        handler._pdf_one = Mock(return_value=None)
        timeout = handler._mcp_pdf_image_result(
            "https://example.test/slow.pdf", pages=None
        )
        self.assertTrue(timeout["isError"])
        self.assertEqual(timeout["structuredContent"]["error"], "PDF render timed out")
        self.assertEqual(timeout["structuredContent"]["pages"], [])

        handler._pdf_one.return_value = _PdfRenderResult(
            url="https://example.test/bad.pdf",
            page_count=4,
            error="render failed",
        )
        failure = handler._mcp_pdf_image_result(
            "https://example.test/bad.pdf", pages=(1,)
        )
        self.assertTrue(failure["isError"])
        self.assertEqual(failure["content"][0]["text"], "Error: render failed")
        self.assertEqual(failure["structuredContent"]["page_count"], 4)

    def test_binary_success_responses_encode_payloads_only_in_content(self):
        handler = _handler(max_pages=1)
        handler._image_one = Mock(
            return_value=_ImageResult(
                url="https://example.test/image.png",
                mime_type="image/webp",
                data=b"image bytes",
                width=20,
                height=10,
            )
        )
        image_response = handler._mcp_image_result(
            "https://example.test/image.png"
        )
        self.assertFalse(image_response["isError"])
        self.assertEqual(
            image_response["content"][0]["data"],
            base64.b64encode(b"image bytes").decode("ascii"),
        )
        self.assertNotIn("data", image_response["structuredContent"])

        handler._pdf_one = Mock(
            return_value=_PdfRenderResult(
                url="https://example.test/document.pdf",
                page_count=2,
                pages=[
                    _PdfPageImage(
                        page=1,
                        width=30,
                        height=40,
                        data=b"pdf page",
                        mime_type="image/jpeg",
                    )
                ],
                truncated=True,
            )
        )
        pdf_response = handler._mcp_pdf_image_result(
            "https://example.test/document.pdf", pages=None
        )
        self.assertFalse(pdf_response["isError"])
        self.assertEqual(
            pdf_response["content"][0]["data"],
            base64.b64encode(b"pdf page").decode("ascii"),
        )
        self.assertNotIn("data", pdf_response["structuredContent"]["pages"][0])
        self.assertIn(
            "page-count or response-size limit",
            pdf_response["content"][-1]["text"],
        )


if __name__ == "__main__":
    unittest.main()
