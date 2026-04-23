# Qt Web Extractor

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/wszqkzqk/qt-web-extractor)

A general-purpose web content extraction engine powered by Qt WebEngine (Chromium). Designed to extract fully-rendered content from modern web pages that rely on JavaScript, cookies, dynamic content loading, or client-side rendering — transforming complex, noisy web structures directly into clean **Markdown** text format, preserving links and readability for LLM processing or downstream pipelines.

Also supports extracting text from PDF documents via Qt PDF.

**Key features:**

- **Smart Markdown formatting** — converts fully rendered web pages directly into clean Markdown, preserving links and structure without the noise of raw HTML. Ideal for feeding text into LLMs.
- **Full JavaScript rendering** — handles SPAs, React/Vue/Angular apps, and
  any page that requires JS to display content.
- **Cookie & session support** — access pages behind login walls or consent
  gates.
- **PDF text extraction** — extract text from PDF documents via Qt PDF with
  auto-detection.
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
  needs rendered web content: AI agents, crawlers, monitoring tools,
  automation scripts, and more.

## Why?

Traditional HTTP fetchers (like `requests` or `urllib`) do plain HTTP
requests — no JS execution, no cookie handling, no waiting for async content.
That means SPAs, React/Vue apps, and anything behind a login wall comes back
empty or broken.

Qt Web Extractor spins up a headless Chromium (via Qt WebEngine) to render
pages properly, then hands back the text and HTML. Runs in offscreen mode by
default, no display needed.

### Why not Playwright / Puppeteer / Selenium?

While tools like Playwright are incredibly powerful for browser automation, they can be overkill for simple content extraction:

- **Simpler Deployment:** No need to download and manage separate, standalone Chromium binaries (which Playwright/Puppeteer do by default).
- **Package Manager Integration:** It uses the system's native Qt WebEngine. On Linux distributions, this means it integrates perfectly with your system's package manager, receiving security updates automatically without bloating your application directory.
- **Lightweight:** It focuses purely on rendering and extracting content, making it more lightweight and straightforward to set up as a simple background service.

## Install

### Arch Linux (AUR)

You can install the package directly from the Arch User Repository (AUR) using your favorite AUR helper (e.g., `yay` or `paru`):

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
python -m qt_web_extractor --user-agent "MyBot/1.0" https://example.com
# note: UA auto-switch is still enabled by default (`on_block`)

# UA auto-switch strategy: off | on_block | rotate
python -m qt_web_extractor --ua-mode on_block https://example.com

# custom fallback UA pool (comma-separated)
python -m qt_web_extractor --ua-pool "UA1,UA2,UA3" https://example.com

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

# UA strategy with custom fallback pool
extractor = QtWebExtractor(
    ua_mode="on_block",
    user_agents=[
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    ],
)
```

### Open WebUI integration

Qt Web Extractor integrates with Open WebUI as an **external web page loader**.
The server exposes an API compatible with Open WebUI's built-in web loader engine.

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
WebUI, etc.), web crawlers, automation scripts, monitoring tools, or your
own projects:

```bash
# start with defaults (127.0.0.1:8766)
qt-web-extractor serve

# custom host/port
qt-web-extractor serve --host 0.0.0.0 --port 9000

# with API key auth
qt-web-extractor serve --api-key mysecretkey

# configure UA auto-switch mode
qt-web-extractor serve --ua-mode on_block

# custom fallback UA pool
qt-web-extractor serve --ua-pool "UA1,UA2,UA3"

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

The explicit `--proxy` flag overrides `HTTPS_PROXY` / `HTTP_PROXY` / `ALL_PROXY` for outbound requests, while `NO_PROXY` is still honored. Only HTTP/HTTPS forward proxies are supported end-to-end.

API endpoints:
- `POST /` with `{"urls": ["https://...", ...]}` → Open WebUI external loader
  format, returns `[{"page_content": "...", "metadata": {"source": "...", "title": "..."}}]`
- `POST /extract` with `{"url": "https://..."}` → single-URL format, returns
  JSON with `url`, `title`, `text`, `html`, `error`
- `POST /mcp` with JSON-RPC 2.0 payload → MCP endpoint for AI agents
  (supports `initialize`, `tools/list`, `tools/call`)
- `GET /health` → `{"status": "ok"}`

PDF URLs (ending in `.pdf`) are auto-detected in both endpoints. For
`POST /extract`, pass `"pdf": true` to force PDF mode.

### MCP integration (Claude Code / OpenCode)

The built-in MCP endpoint (`/mcp`) reuses the same running server process.
No extra wrapper process is required.
MCP uses the same Bearer authentication as `/extract`.

Available MCP tool:
- `fetch_url` with input `{ "url": "https://..." }`
- Returns rendered Markdown text (with PDF auto-detection)

Claude Code example:

```bash
# no auth
claude mcp add --transport http web-extractor http://127.0.0.1:8766/mcp

# if server uses --api-key
claude mcp add --transport http web-extractor http://127.0.0.1:8766/mcp \
  --header "Authorization: Bearer mysecretkey"
```

Optional Claude Code config:

These files are user-managed and are not auto-created by package installation.

- **Project-scoped**: create `.mcp.json` in your project root.
- **User-scoped (global)**: configure `~/.claude.json` under `mcpServers`, or run:
  ```bash
  claude mcp add --transport http --scope user web-extractor http://127.0.0.1:8766/mcp
  ```

Example `.mcp.json` (project-scoped):

```json
{
  "mcpServers": {
    "web-extractor": {
      "type": "http",
      "url": "${QT_WEB_EXTRACTOR_MCP_URL:-http://127.0.0.1:8766/mcp}",
      "headers": {
        "Authorization": "Bearer ${QT_WEB_EXTRACTOR_API_KEY:-}"
      }
    }
  }
}
```

OpenCode config (`opencode.json` in project root, or `~/.config/opencode/opencode.json` for global user config):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "web_extractor": {
      "type": "remote",
      "url": "http://127.0.0.1:8766/mcp",
      "enabled": true,
      "oauth": false,
      "headers": {
        "Authorization": "Bearer {env:QT_WEB_EXTRACTOR_API_KEY}"
      }
    }
  }
}
```

Hardcoded values are also valid in both configs, for example: `"Authorization": "Bearer mysecretkey"`.

When auth is disabled, the `Authorization` header can be omitted.

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
| `TIMEOUT_MS` | `30000` | Page load timeout (ms) |
| `USER_AGENT` | `""` | Custom User-Agent |
| `UA_MODE` | `on_block` | UA strategy: `off`, `on_block`, or `rotate` |
| `UA_POOL` | unset | Comma-separated fallback UA list (defaults to built-in mobile UA pool) |
| `API_KEY` | `""` | Bearer token auth (empty = no auth) |
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

Sites behind Cloudflare's aggressive bot challenge may still fail — this is a
known limitation of all headless browsers.

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

This project is licensed under the GNU General Public License v3.0 or later (GPL-3.0-or-later). See the [COPYING](COPYING) file for details.

This program is distributed in the hope that it will be useful, but **WITHOUT ANY WARRANTY**; without even the implied warranty of **MERCHANTABILITY** or **FITNESS FOR A PARTICULAR PURPOSE**. See the [GNU General Public License](https://www.gnu.org/licenses/gpl-3.0.html) for more details.
