import ssl
import unittest
from unittest.mock import patch

from spatius.net import get_ssl_context, host_port_for_url, warm_tls_connection


class TestGetSSLContext(unittest.TestCase):
    def test_returns_shared_default_context(self):
        ctx = get_ssl_context()
        self.assertIsInstance(ctx, ssl.SSLContext)
        self.assertIs(ctx, get_ssl_context())


class TestHostPortForUrl(unittest.TestCase):
    def test_https_url(self):
        self.assertEqual(
            host_port_for_url("https://console.us-west.spatius.ai/v1/console"),
            ("console.us-west.spatius.ai", 443),
        )

    def test_wss_url(self):
        self.assertEqual(
            host_port_for_url("wss://api.us-west.spatius.ai/v2/driveningress"),
            ("api.us-west.spatius.ai", 443),
        )

    def test_explicit_port(self):
        self.assertEqual(
            host_port_for_url("https://example.com:8443/x"), ("example.com", 8443)
        )

    def test_bare_host(self):
        self.assertEqual(host_port_for_url("example.com"), ("example.com", 443))

    def test_invalid_url_raises(self):
        with self.assertRaises(ValueError):
            host_port_for_url("://")


class TestWarmTLSConnection(unittest.IsolatedAsyncioTestCase):
    async def test_failure_returns_false_and_does_not_raise(self):
        async def fail_open(*_args, **_kwargs):
            raise OSError("connection refused")

        with patch("spatius.net.asyncio.open_connection", new=fail_open):
            self.assertFalse(await warm_tls_connection("https://api.example.com"))

    async def test_unparseable_url_returns_false(self):
        self.assertFalse(await warm_tls_connection("://"))

    async def test_success_returns_true(self):
        class _Writer:
            def close(self):
                pass

            async def wait_closed(self):
                pass

        async def fake_open(*_args, **_kwargs):
            return None, _Writer()

        with patch("spatius.net.asyncio.open_connection", new=fake_open):
            self.assertTrue(await warm_tls_connection("https://api.example.com"))


if __name__ == "__main__":
    unittest.main()
