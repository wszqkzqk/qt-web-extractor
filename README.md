# Qt Web Extractor

Web content extraction engine backed by Qt WebEngine (Chromium). Built for
[Open WebUI](https://github.com/open-webui/open-webui) to replace the default
HTTP fetcher, which can't handle pages that need JavaScript, cookies, or
dynamic content loading.

Also supports extracting text from PDF documents via Qt PDF.

## Why?

Open WebUI's built-in web loader does plain HTTP requests — no JS execution,
no cookie handling, no waiting for async content. That means SPAs, React/Vue
apps, and anything behind a login wall comes back empty or broken.

This tool spins up a headless Chromium (via Qt WebEngine) to render pages
properly, then hands back the text and HTML. Runs in offscreen mode by default,
no display needed.

## Install

### System deps

You need Qt6 WebEngine and Qt6 PDF. On Arch:

```
sudo pacman -S qt6-webengine qt6-pdf pyside6
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
# plain text
python -m qt_web_extractor https://example.com

# JSON output
python -m qt_web_extractor --json https://example.com

# rendered HTML
python -m qt_web_extractor --html https://example.com

# custom timeout (ms)
python -m qt_web_extractor --timeout 60000 https://example.com

# custom User-Agent
python -m qt_web_extractor --user-agent "MyBot/1.0" https://example.com

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
```

### Open WebUI integration

The tool talks to the extraction server over HTTP, so you need the server
running first.

1. Install and start the server:
   ```
   sudo systemctl enable --now qt-web-extractor
   ```

   Or run manually:
   ```
   qt-web-extractor serve
   ```

2. In the Open WebUI admin panel, go to **Workspace → Tools → Create Tool**

3. Paste the contents of `qt_web_extractor/tool.py` into the editor and save

4. Configure the Valve `server_url` to point to your server (default
   `http://127.0.0.1:8766`). Set `api_key` if you configured one.

5. `fetch_page`, `fetch_page_html`, and `fetch_pdf` are now available in conversations.
   `fetch_page` auto-detects PDF URLs by file extension.

### Server mode

Run as a persistent HTTP service for Open WebUI or any other client:

```bash
# start with defaults (127.0.0.1:8766)
qt-web-extractor serve

# custom host/port
qt-web-extractor serve --host 0.0.0.0 --port 9000

# with API key auth
qt-web-extractor serve --api-key mysecretkey
```

API:
- `POST /extract` with `{"url": "https://..."}` → returns JSON with
  `url`, `title`, `text`, `html`, `error`
- `POST /extract` with `{"url": "https://.../file.pdf"}` → auto-detects PDF,
  extracts text (or pass `"pdf": true` to force PDF mode)
- `GET /health` → `{"status": "ok"}`

### systemd

A service file and config are included:

```bash
# edit config
sudo nano /etc/qt-web-extractor.conf

# start
sudo systemctl enable --now qt-web-extractor
```

#### Valve settings (Open WebUI tool)

| Name | Default | Description |
|------|---------|-------------|
| `server_url` | `http://127.0.0.1:8766` | Extraction server URL |
| `api_key` | `""` | Bearer token (must match server's `API_KEY`) |

#### Server config (`/etc/qt-web-extractor.conf`)

| Name | Default | Description |
|------|---------|-------------|
| `HOST` | `127.0.0.1` | Listen address |
| `PORT` | `8766` | Listen port |
| `TIMEOUT_MS` | `30000` | Page load timeout (ms) |
| `USER_AGENT` | `""` | Custom User-Agent |
| `API_KEY` | `""` | Bearer token auth (empty = no auth) |

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
