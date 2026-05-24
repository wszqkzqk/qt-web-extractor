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

import sys
import os
import argparse
import json

from qt_web_extractor.extractor import QtWebExtractor


def _cmd_extract(args):
    extractor = QtWebExtractor(
        timeout_ms=args.timeout,
        user_agent=args.user_agent,
        proxy=args.proxy,
        mineru_api_key=getattr(args, "mineru_api_key", ""),
        mineru_timeout_ms=getattr(args, "mineru_timeout", 300000),
        mineru_base_url=getattr(args, "mineru_base_url", "https://mineru.net"),
    )

    results = []
    for url in args.urls:
        if getattr(args, "pdf", False) or extractor.detect_pdf_url(url):
            results.append(extractor.extract_pdf(url))
        else:
            results.append(extractor.extract(url))

    del extractor

    if args.output_json:
        if len(results) == 1:
            print(results[0].to_json())
        else:
            print(json.dumps([r.to_dict() for r in results], ensure_ascii=False))
    else:
        for result in results:
            if result.error:
                print(f"[ERROR] {result.error}", file=sys.stderr)
            if result.title:
                print(f"=== {result.title} ===")
                print(f"URL: {result.url}\n")
            print(result.html if args.html else result.text)
            if len(results) > 1:
                print("\n" + "=" * 60 + "\n")


def _cmd_serve(args):
    from qt_web_extractor.server import serve

    serve(
        host=args.host,
        port=args.port,
        timeout_ms=args.timeout,
        user_agent=args.user_agent or None,
        api_key=args.api_key,
        proxy=args.proxy,
        mineru_api_key=getattr(args, "mineru_api_key", ""),
        mineru_timeout_ms=getattr(args, "mineru_timeout", 300000),
        mineru_base_url=getattr(args, "mineru_base_url", "https://mineru.net"),
    )


def main():
    # If the first non-flag arg isn't a known subcommand, treat it as
    # a bare URL for backwards-compat (i.e. `qt-web-extractor URL`).
    known_cmds = {"extract", "serve"}
    first_pos = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    if first_pos and first_pos not in known_cmds:
        # Bare URL invocation
        flat = argparse.ArgumentParser(prog="qt-web-extractor")
        flat.add_argument("urls", nargs="+", metavar="URL")
        flat.add_argument("--timeout", type=int, default=30000)
        flat.add_argument("--user-agent", type=str, default=None)
        flat.add_argument(
            "--proxy",
            type=str,
            default=None,
            help="Override outbound HTTP/HTTPS proxy. Defaults to HTTPS_PROXY/HTTP_PROXY/ALL_PROXY.",
        )
        flat.add_argument("--json", action="store_true", dest="output_json")
        flat.add_argument("--html", action="store_true")
        flat.add_argument("--pdf", action="store_true", help="Force PDF extraction")
        flat.add_argument(
            "--mineru-api-key",
            type=str,
            default=os.environ.get("MINERU_API_KEY", ""),
            help="MinerU API Key for PDF extraction",
        )
        flat.add_argument(
            "--mineru-timeout",
            type=int,
            default=int(os.environ.get("MINERU_TIMEOUT_MS", "300000")),
            help="Timeout for MinerU API in ms",
        )
        flat.add_argument(
            "--mineru-base-url",
            type=str,
            default=os.environ.get("MINERU_BASE_URL", "https://mineru.net"),
            help="MinerU API base URL (supports self-hosted MinerU)",
        )
        _cmd_extract(flat.parse_args())
        return

    parser = argparse.ArgumentParser(
        prog="qt-web-extractor",
        description="Extract web page content using Qt WebEngine with full JS support.",
    )
    sub = parser.add_subparsers(dest="command")

    p_extract = sub.add_parser("extract", help="One-shot URL extraction.")
    p_extract.add_argument("urls", nargs="+", metavar="URL")
    p_extract.add_argument("--timeout", type=int, default=30000)
    p_extract.add_argument("--user-agent", type=str, default=None)
    p_extract.add_argument(
        "--proxy",
        type=str,
        default=None,
        help="Override outbound HTTP/HTTPS proxy. Defaults to HTTPS_PROXY/HTTP_PROXY/ALL_PROXY.",
    )
    p_extract.add_argument("--json", action="store_true", dest="output_json")
    p_extract.add_argument("--html", action="store_true")
    p_extract.add_argument("--pdf", action="store_true", help="Force PDF extraction")
    p_extract.add_argument(
        "--mineru-api-key",
        type=str,
        default=os.environ.get("MINERU_API_KEY", ""),
        help="MinerU API Key for PDF extraction",
    )
    p_extract.add_argument(
        "--mineru-timeout",
        type=int,
        default=int(os.environ.get("MINERU_TIMEOUT_MS", "300000")),
        help="Timeout for MinerU API in ms",
    )
    p_extract.add_argument(
        "--mineru-base-url",
        type=str,
        default=os.environ.get("MINERU_BASE_URL", "https://mineru.net"),
        help="MinerU API base URL (supports self-hosted MinerU)",
    )

    p_serve = sub.add_parser("serve", help="Run as HTTP extraction server.")
    p_serve.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    p_serve.add_argument(
        "--port", type=int, default=int(os.environ.get("PORT", "8766"))
    )
    p_serve.add_argument(
        "--timeout", type=int, default=int(os.environ.get("TIMEOUT_MS", "30000"))
    )
    p_serve.add_argument(
        "--user-agent", type=str, default=os.environ.get("USER_AGENT", "")
    )
    p_serve.add_argument("--api-key", type=str, default=os.environ.get("API_KEY", ""))
    p_serve.add_argument(
        "--mineru-api-key",
        type=str,
        default=os.environ.get("MINERU_API_KEY", ""),
        help="MinerU API Key for PDF extraction",
    )
    p_serve.add_argument(
        "--mineru-timeout",
        type=int,
        default=int(os.environ.get("MINERU_TIMEOUT_MS", "300000")),
        help="Timeout for MinerU API in ms",
    )
    p_serve.add_argument(
        "--mineru-base-url",
        type=str,
        default=os.environ.get("MINERU_BASE_URL", "https://mineru.net"),
        help="MinerU API base URL (supports self-hosted MinerU)",
    )
    p_serve.add_argument(
        "--proxy",
        type=str,
        default=None,
        help="Override outbound HTTP/HTTPS proxy. Defaults to HTTPS_PROXY/HTTP_PROXY/ALL_PROXY.",
    )

    args = parser.parse_args()

    if args.command == "serve":
        _cmd_serve(args)
    elif args.command == "extract":
        _cmd_extract(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
