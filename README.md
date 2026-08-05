# Qt Web Extractor

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/wszqkzqk/qt-web-extractor)

A general-purpose **multimodal** web content extraction engine powered by Qt
WebEngine (Chromium). Designed to extract fully-rendered content from modern
web pages that rely on JavaScript, cookies, dynamic content loading, or
client-side rendering — transforming complex, noisy web structures directly
into clean **Markdown** text format, preserving links and readability for LLM
processing or downstream pipelines.

Also supports extracting text from PDF documents via Qt PDF and fetching
images through the same browser engine, so vision-capable agents can *see*
the web, not just read it.

**Key features:**

- **Smart Markdown formatting** — converts fully rendered web pages directly
  into clean Markdown, preserving links and structure without the noise of raw
  HTML. Ideal for feeding text into LLMs.
- **Full JavaScript rendering** — handles SPAs, React/Vue/Angular apps, and
  any page that requires JS to display content.
- **Multimodal MCP support** — serve images to vision-capable agents as
  native MCP image content, fetched through the same full browser engine.
- **PDF extraction** — extract text from PDF documents or render pages as
  images for vision-capable agents, all through the same real browser
  engine.
- **Cookie & session support** — access pages behind login walls or consent
  gates.
- **Multiple interfaces** — use as a CLI tool, Python library, or HTTP
  service with a simple REST API.
- **Headless operation** — runs in Qt offscreen mode, no display or GPU
  required.
- **Lightweight dependencies** — no standalone browser binaries, and no full
  browser process overhead, saving resources.
- **systemd integration** — ships with a service unit for easy deployment.
- **Open WebUI compatible** — works as an external web page loader for
  [Open WebUI](https://github.com/open-webui/open-webui), and can also be
  used as a custom tool plugin.
- **Universal HTTP API** — the REST server can serve any application that
  needs rendered web content: AI agents, monitoring tools, automation
  scripts, and more.

## Why?

Traditional HTTP fetchers (like `requests` or `urllib`) do plain HTTP
requests — no JS execution, no cookie handling, no waiting for async content.
That means SPAs, React/Vue apps, and anything behind a login wall comes back
empty or broken.

Qt Web Extractor spins up a headless Chromium (via Qt WebEngine) to render
pages properly, then hands back the text and HTML. Runs in offscreen mode by
default, no display needed.

### Why not Playwright / Puppeteer / Selenium?

While tools like Playwright are incredibly powerful for browser automation,
they can be overkill for simple content extraction:

- **Simpler Deployment:** No need to download and manage separate, standalone
  Chromium binaries (which Playwright/Puppeteer do by default).
- **Package Manager Integration:** It uses the system's native Qt WebEngine.
  On Linux distributions, this means it integrates perfectly with your
  system's package manager, receiving security updates automatically without
  bloating your application directory.
- **Lightweight:** It focuses purely on rendering and extracting content,
  making it more lightweight and straightforward to set up as a simple
  background service.

## Install

### Arch Linux (AUR)

You can install the package directly from the Arch User Repository (AUR) using
your favorite AUR helper (e.g., `yay` or `paru`):

```bash
yay -S qt-web-extractor
```

### System deps

You need Qt6 WebEngine (which includes Qt6 PDF). On Arch:

```
sudo pacman -S qt6-webengine pyside6
```

### Install the package

```
pip install .
```

Or in dev mode:

```
pip install -e .
```

## Usage

### CLI

```bash
# Clean Markdown text (preserves links and structure)
python -m qt_web_extractor https://example.com

# JSON output
python -m qt_web_extractor --json https://example.com

# rendered HTML
python -m qt_web_extractor --html https://example.com

# custom timeout (ms)
python -m qt_web_extractor --timeout 60000 https://example.com

# custom User-Agent
python -m qt_web_extractor --user-agent "MyApp/1.0" https://example.com

# override proxy for this command
python -m qt_web_extractor --proxy http://127.0.0.1:7890 https://example.com

# multiple URLs
python -m qt_web_extractor https://example.com https://example.org

# extract text from a PDF (auto-detected by .pdf extension)
python -m qt_web_extractor https://example.com/document.pdf

# force PDF extraction mode
python -m qt_web_extractor --pdf https://example.com/file
```

### Python API

```python
from qt_web_extractor import QtWebExtractor

extractor = QtWebExtractor(timeout_ms=30000)
result = extractor.extract("https://example.com")

print(result.title)
print(result.text)   # plain text
print(result.html)   # rendered HTML
print(result.error)  # empty string if all went well

# extract from PDF
result = extractor.extract_pdf("https://example.com/document.pdf")
print(result.text)

# override proxy explicitly (otherwise standard proxy env vars are used)
extractor = QtWebExtractor(proxy="http://127.0.0.1:7890")
```

### Open WebUI integration

Qt Web Extractor integrates with Open WebUI as an **external web page loader**.
The server exposes an API compatible with Open WebUI's built-in web loader
engine.

1. Install and start the server:
   ```
   sudo systemctl enable --now qt-web-extractor
   ```

   Or run manually:
   ```
   qt-web-extractor serve
   ```

2. In the Open WebUI admin panel, go to
   **Settings → Web Search → Web Page Loader**

3. Set **Web Loader Engine** to `external`

4. Set **External Web Loader URL** to `http://127.0.0.1:8766`
   (or wherever the server is running)

5. Set **External Web Loader API Key** to the server's `API_KEY` if
   you configured one, or any non-empty string if didn't.

That's it — Open WebUI will now use Qt Web Extractor to load all web pages
with full JavaScript rendering support. PDF URLs are auto-detected and handled
via Qt PDF.

#### Alternative: custom tool

You can also use `qt_web_extractor/tool.py` as a custom Open WebUI tool for
more explicit control (see the file for setup instructions). This provides
`fetch_page`, `fetch_page_html`, and `fetch_pdf` as conversation tools.

### Server mode

Run as a persistent HTTP service for any application — AI platforms (Open
WebUI, etc.), automation scripts, monitoring tools, or your own projects:

```bash
# start with defaults (127.0.0.1:8766)
qt-web-extractor serve

# custom host/port
qt-web-extractor serve --host 0.0.0.0 --port 9000

# with API key auth
qt-web-extractor serve --api-key mysecretkey

# let clients read server-local files (file:// URLs, local paths)
qt-web-extractor serve --allow-local-files

# override proxy for the service process
qt-web-extractor serve --proxy http://127.0.0.1:7890
```

Proxy handling follows standard environment variables by default:

```bash
export HTTPS_PROXY=http://127.0.0.1:7890
export HTTP_PROXY=http://127.0.0.1:7890
export ALL_PROXY=http://127.0.0.1:7890
export NO_PROXY=127.0.0.1,localhost,.internal.example
```

The explicit `--proxy` flag overrides `HTTPS_PROXY` / `HTTP_PROXY` /
`ALL_PROXY` for outbound requests, while `NO_PROXY` is still honored. Only
HTTP/HTTPS forward proxies are supported end-to-end.

API endpoints:
- `POST /` with `{"urls": ["https://...", ...]}` → Open WebUI external loader
  format, returns
  `[{"page_content": "...", "metadata": {"source": "...", "title": "..."}}]`
- `POST /extract` with `{"url": "https://..."}` → single-URL format, returns
  JSON with `url`, `title`, `text`, `html`, `error`
- `POST /mcp` with JSON-RPC 2.0 payload → MCP endpoint for AI agents
  (supports `initialize`, `tools/list`, `tools/call`)
- `GET /health` → `{"status": "ok"}`

PDF URLs (ending in `.pdf`) are auto-detected in both endpoints. For
`POST /extract`, pass `"pdf": true` to force PDF mode.

### MCP integration

The built-in MCP endpoint (`/mcp`) reuses the same running server process.
No extra wrapper process is required. It speaks standard MCP over HTTP and
uses the same Bearer authentication as `/extract`.

Canonical configuration — works in most MCP clients:

```json
{
  "mcpServers": {
    "web-extractor": {
      "url": "http://127.0.0.1:8766/mcp",
      "headers": {
        "Authorization": "Bearer mysecretkey"
      }
    }
  }
}
```

Omit `headers` when the server runs without `--api-key`. If you don't want
to store the token in plain text, use your client's environment variable
expansion if it has one. Clients differ only in where this JSON lives and
in minor key naming — consult your client's own MCP documentation (see the
[official client list](https://modelcontextprotocol.io/clients)).

Claude Code, for example, takes a one-liner:

```bash
# no auth
claude mcp add --transport http web-extractor http://127.0.0.1:8766/mcp

# if server uses --api-key
claude mcp add --transport http web-extractor http://127.0.0.1:8766/mcp \
  --header "Authorization: Bearer mysecretkey"
```

To verify the setup, ask the agent to fetch a page, e.g. "use web-extractor
to fetch https://example.com and summarize it".

Available MCP tools:
- `fetch_url` with input `{ "url": "https://..." }` — returns rendered
  Markdown text (with PDF auto-detection). If the URL actually points to an
  image, it is returned directly as image content, same as calling
  `fetch_image`.
- `fetch_pdf` with input `{ "url": "https://.../doc.pdf" }` — fetches a PDF
  document. Default mode extracts its text; `"mode": "image"` renders pages
  as JPEG images for multimodal models (figures, charts, scanned pages).
  Optional `"pages"` accepts 1-based integers and/or `"a-b"` ranges.
- `fetch_image` with input `{ "url": "https://.../img.png" }` — loads an
  image through the same full browser engine and returns it as
  WebP image content that multimodal models can view directly, capped at
  2576 px on the long edge (the high-resolution tier of current vision
  models). Any browser-renderable format works — PNG, JPEG, GIF, WebP,
  SVG, AVIF, ICO, ... Only absolute http(s) URLs (plus local files when the
  server runs with `--allow-local-files`).

  Models without vision capability can simply ignore this tool — every
  image result also carries a text summary (URL, format, dimensions, size),
  so nothing breaks for text-only agents.

  Site-relative image links in fetched Markdown must be resolved against
  the page URL first: after fetching `https://xxx.yyy/foo/bar.html`, an
  image like `![baz](/baz/img.png)` is viewed by calling `fetch_image` with
  `https://xxx.yyy/baz/img.png`.

### systemd

A service file and config are included:

```bash
# edit config
sudo nano /etc/qt-web-extractor.conf

# start
sudo systemctl enable --now qt-web-extractor
```

#### Open WebUI settings

| Setting | Value | Description |
|---------|-------|-------------|
| `WEB_LOADER_ENGINE` | `external` | Use external web loader |
| `EXTERNAL_WEB_LOADER_URL` | `http://127.0.0.1:8766` | Server URL |
| `EXTERNAL_WEB_LOADER_API_KEY` | `""` | Bearer token (must match server's `API_KEY`) |

#### Server config (`/etc/qt-web-extractor.conf`)

| Name | Default | Description |
|------|---------|-------------|
| `HOST` | `127.0.0.1` | Listen address |
| `PORT` | `8766` | Listen port |
| `TIMEOUT_MS` | `30000` | Page load timeout (ms); operation-wide budget for loading and rendering all selected pages in a `fetch_pdf` image-mode request |
| `USER_AGENT` | `""` | Custom User-Agent |
| `API_KEY` | `""` | Bearer token auth (empty = no auth) |
| `ALLOW_LOCAL_FILES` | `""` | Allow reading local files (`file://` URLs, local paths) for clients |
| `PDF_CACHE_MAX_BYTES` | `67108864` | Total memory budget (bytes) for cached PDF downloads (64 MiB) |
| `PDF_MAX_RENDER_PAGES` | `10` | Max PDF pages rendered per `fetch_pdf` image-mode call |
| `PDF_CACHE_TTL_S` | `600` | Seconds a cached PDF download stays reusable |
| `HTTPS_PROXY` | unset | HTTPS outbound proxy |
| `HTTP_PROXY` | unset | HTTP outbound proxy |
| `ALL_PROXY` | unset | Fallback outbound proxy |
| `NO_PROXY` | unset | Hosts that bypass proxy |

## How it works

The server runs Qt WebEngine on the main thread (Qt requirement) and an HTTP
server in a background thread. Incoming requests are queued and processed one
at a time by the Qt event loop. Each page gets 2 seconds after `loadFinished`
for JS to settle, then `toPlainText()` and `toHtml()` are extracted from the
rendered DOM. A hard timeout prevents hanging on unresponsive pages.

Pages that require human verification may still fail — this is a known
limitation of all headless browsers.

## Project layout

```
qt-web-extractor/
├── pyproject.toml
├── PKGBUILD
├── LICENSE
├── README.md
├── qt-web-extractor.service         # systemd unit
├── qt-web-extractor.conf.example    # default config
└── qt_web_extractor/
    ├── __init__.py
    ├── __main__.py      # CLI (extract + serve subcommands)
    ├── extractor.py     # core engine (QWebEnginePage)
    ├── server.py        # HTTP server wrapper
    └── tool.py          # Open WebUI tool interface
```

## License

This project is licensed under the GNU General Public License v3.0 or later
(GPL-3.0-or-later). See the [COPYING](COPYING) file for details.

This program is distributed in the hope that it will be useful, but
**WITHOUT ANY WARRANTY**; without even the implied warranty of
**MERCHANTABILITY** or **FITNESS FOR A PARTICULAR PURPOSE**. See the
[GNU General Public License](https://www.gnu.org/licenses/gpl-3.0.html) for more
details.
