import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from qt_web_extractor.extractor import QtWebExtractor, _ProxyConfig


class ProxyTests(unittest.TestCase):
    def test_normalize_proxy_url(self):
        normalized = QtWebExtractor._normalize_proxy_url(
            "https://user:pass@proxy.example:8443/path?query=yes#fragment",
            strict=True,
        )

        self.assertEqual(normalized, "https://user:pass@proxy.example:8443")
        with self.assertRaisesRegex(ValueError, "HTTP/HTTPS"):
            QtWebExtractor._normalize_proxy_url(
                "socks5://proxy.example:1080",
                strict=True,
            )

    def test_explicit_proxy_takes_precedence_and_preserves_no_proxy(self):
        environment = {
            "HTTP_PROXY": "http://environment.example:8080",
            "NO_PROXY": " localhost, .example.com, 127.0.0.1 ",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch("urllib.request.getproxies_environment") as getproxies,
        ):
            config = QtWebExtractor._resolve_proxy_config(
                "https://explicit.example:8443/path"
            )

        self.assertEqual(
            config,
            _ProxyConfig(
                proxies={
                    "http": "https://explicit.example:8443",
                    "https": "https://explicit.example:8443",
                },
                no_proxy=("localhost", ".example.com", "127.0.0.1"),
            ),
        )
        getproxies.assert_not_called()

    def test_environment_proxies_and_no_proxy_bypass(self):
        environment_proxies = {
            "http": "http://http-proxy.example:8080/path",
            "all": "https://fallback-proxy.example:4443/path",
            "no": "localhost,.example.com",
        }
        with patch(
            "urllib.request.getproxies_environment",
            return_value=environment_proxies,
        ):
            config = QtWebExtractor._resolve_proxy_config(None)

        self.assertEqual(
            config,
            _ProxyConfig(
                proxies={
                    "http": "http://http-proxy.example:8080",
                    "https": "https://fallback-proxy.example:4443",
                },
                no_proxy=("localhost", ".example.com"),
            ),
        )

        extractor = SimpleNamespace(
            _proxy_config=config,
            _no_proxy_match=QtWebExtractor._no_proxy_match,
        )
        self.assertTrue(
            QtWebExtractor._should_bypass_proxy(
                extractor,
                "https://api.example.com/x",
            )
        )
        self.assertTrue(
            QtWebExtractor._should_bypass_proxy(extractor, "https://localhost/x")
        )
        self.assertFalse(
            QtWebExtractor._should_bypass_proxy(
                extractor,
                "https://notexample.com/x",
            )
        )

    def test_chromium_proxy_flags_are_idempotent(self):
        config = _ProxyConfig(
            proxies={
                "http": "http://proxy.example:8080",
                "https": "http://proxy.example:8080",
            },
            no_proxy=("localhost", ".example.com"),
        )
        key = "QTWEBENGINE_CHROMIUM_FLAGS"

        with patch.dict(os.environ, {key: "--disable-gpu"}, clear=True):
            QtWebExtractor._apply_chromium_proxy(config)
            first = os.environ[key]
            QtWebExtractor._apply_chromium_proxy(config)
            second = os.environ[key]

        self.assertEqual(second, first)
        self.assertEqual(first.count("--proxy-server="), 1)
        self.assertEqual(first.count("--proxy-bypass-list="), 1)
        self.assertIn("--disable-gpu", first)


if __name__ == "__main__":
    unittest.main()
