import time
import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import patch

import aiohttp

import spatius.bootstrap as bootstrap
from spatius import new_avatar_session
from spatius.bootstrap import BootstrapError, fetch_bootstrap, resolve_region


class _FakeHTTPResponse:
    def __init__(self, status: int, body):
        self.status = status
        self._body = body

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    async def text(self) -> str:
        return self._body if isinstance(self._body, str) else ""


class _FakeRequestContext:
    def __init__(self, response: Optional[_FakeHTTPResponse] = None, error=None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeClientSession:
    def __init__(self, response: Optional[_FakeHTTPResponse] = None, error=None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, *_args, **_kwargs):
        return _FakeRequestContext(response=self._response, error=self._error)


def _region_body(current: str) -> dict:
    return {"region": {"current": current, "candidates": ["us-west"]}}


class TestFetchBootstrap(unittest.IsolatedAsyncioTestCase):
    async def test_posts_expected_payload_and_returns_body(self):
        captured: dict = {}

        class CapturingSession(_FakeClientSession):
            def post(self, url, **kwargs):
                captured["url"] = url
                captured.update(kwargs)
                return _FakeRequestContext(
                    response=_FakeHTTPResponse(200, _region_body("eu-central"))
                )

        with patch(
            "spatius.bootstrap.aiohttp.ClientSession",
            new=lambda *a, **k: CapturingSession(),
        ):
            body = await fetch_bootstrap(
                app_id="app-1", sdk_version="1.2.3", region="auto"
            )

        self.assertEqual(body["region"]["current"], "eu-central")
        self.assertEqual(captured["url"], bootstrap.BOOTSTRAP_URL)
        self.assertEqual(
            captured["json"],
            {
                "app_id": "app-1",
                "sdk_version": "1.2.3",
                "region": "auto",
                "platform": "python",
            },
        )

    async def test_non_200_raises_bootstrap_error(self):
        with patch(
            "spatius.bootstrap.aiohttp.ClientSession",
            new=lambda *a, **k: _FakeClientSession(
                response=_FakeHTTPResponse(503, "oops")
            ),
        ):
            with self.assertRaises(BootstrapError) as cm:
                await fetch_bootstrap(app_id="app-1")
        self.assertIn("503", str(cm.exception))

    async def test_transport_error_raises_bootstrap_error(self):
        with patch(
            "spatius.bootstrap.aiohttp.ClientSession",
            new=lambda *a, **k: _FakeClientSession(
                error=aiohttp.ClientConnectionError("network down")
            ),
        ):
            with self.assertRaises(BootstrapError):
                await fetch_bootstrap(app_id="app-1")

    async def test_non_object_body_raises_bootstrap_error(self):
        with patch(
            "spatius.bootstrap.aiohttp.ClientSession",
            new=lambda *a, **k: _FakeClientSession(
                response=_FakeHTTPResponse(200, ["not", "a", "dict"])
            ),
        ):
            with self.assertRaises(BootstrapError):
                await fetch_bootstrap(app_id="app-1")


class TestResolveRegion(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bootstrap._cached_region = None
        bootstrap._cached_at = 0.0

    def tearDown(self):
        bootstrap._cached_region = None
        bootstrap._cached_at = 0.0

    async def test_concrete_region_used_directly_without_bootstrap(self):
        async def fail_fetch(**_kwargs):
            raise AssertionError("bootstrap must not be called")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fail_fetch):
            region = await resolve_region(app_id="app-1", requested_region="eu-central")
        self.assertEqual(region, "eu-central")

    async def test_auto_resolves_region_current_and_caches(self):
        async def fake_fetch(**_kwargs):
            return _region_body("ap-southeast")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fake_fetch):
            region = await resolve_region(app_id="app-1", requested_region="auto")
        self.assertEqual(region, "ap-southeast")
        self.assertEqual(bootstrap._cached_region, "ap-southeast")

    async def test_empty_region_treated_as_auto(self):
        async def fake_fetch(**_kwargs):
            return _region_body("eu-central")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fake_fetch):
            region = await resolve_region(app_id="app-1", requested_region="")
        self.assertEqual(region, "eu-central")

    async def test_failure_falls_back_to_default_region(self):
        async def fail_fetch(**_kwargs):
            raise BootstrapError("network down")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fail_fetch):
            with self.assertLogs("spatius.bootstrap", level="WARNING"):
                region = await resolve_region(app_id="app-1", requested_region="auto")
        self.assertEqual(region, "us-west")

    async def test_failure_falls_back_to_cached_region(self):
        bootstrap._cached_region = "eu-central"

        async def fail_fetch(**_kwargs):
            raise BootstrapError("network down")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fail_fetch):
            region = await resolve_region(app_id="app-1", requested_region="auto")
        self.assertEqual(region, "eu-central")

    async def test_missing_region_current_falls_back(self):
        async def fake_fetch(**_kwargs):
            return {"time_sync": {"server_receive_ms": 1, "server_send_ms": 2}}

        with patch("spatius.bootstrap.fetch_bootstrap", new=fake_fetch):
            region = await resolve_region(app_id="app-1", requested_region="auto")
        self.assertEqual(region, "us-west")

    async def test_fresh_cached_region_skips_bootstrap(self):
        async def fake_fetch(**_kwargs):
            return _region_body("ap-southeast")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fake_fetch):
            first = await resolve_region(app_id="app-1", requested_region="auto")
        self.assertEqual(first, "ap-southeast")

        async def fail_fetch(**_kwargs):
            raise AssertionError("bootstrap must not be called within the TTL")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fail_fetch):
            second = await resolve_region(app_id="app-1", requested_region="auto")
        self.assertEqual(second, "ap-southeast")

    async def test_expired_cache_triggers_refetch(self):
        bootstrap._cached_region = "ap-southeast"
        bootstrap._cached_at = time.monotonic() - bootstrap.REGION_CACHE_TTL_S - 1

        async def fake_fetch(**_kwargs):
            return _region_body("eu-central")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fake_fetch):
            region = await resolve_region(app_id="app-1", requested_region="auto")
        self.assertEqual(region, "eu-central")

    async def test_zero_cache_ttl_disables_positive_caching(self):
        calls = 0

        async def fake_fetch(**_kwargs):
            nonlocal calls
            calls += 1
            return _region_body("ap-southeast")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fake_fetch):
            await resolve_region(app_id="app-1", requested_region="auto", cache_ttl=0)
            await resolve_region(app_id="app-1", requested_region="auto", cache_ttl=0)
        self.assertEqual(calls, 2)

    async def test_stale_cache_still_used_as_failure_fallback(self):
        bootstrap._cached_region = "ap-southeast"
        bootstrap._cached_at = time.monotonic() - bootstrap.REGION_CACHE_TTL_S - 1

        async def fail_fetch(**_kwargs):
            raise BootstrapError("network down")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fail_fetch):
            region = await resolve_region(app_id="app-1", requested_region="auto")
        self.assertEqual(region, "ap-southeast")


class TestInitRegionResolution(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bootstrap._cached_region = None
        bootstrap._cached_at = 0.0

    def tearDown(self):
        bootstrap._cached_region = None
        bootstrap._cached_at = 0.0

    def _mk_session(self, **kwargs):
        kwargs.setdefault("api_key", "api")
        kwargs.setdefault(
            "expire_at", datetime.now(timezone.utc) + timedelta(minutes=5)
        )
        return new_avatar_session(**kwargs)

    def _token_ok_session(self):
        return _FakeClientSession(
            response=_FakeHTTPResponse(200, '{"sessionToken": "tok"}')
        )

    async def test_init_resolves_auto_region_via_bootstrap(self):
        session = self._mk_session(app_id="app-1")
        # auto region: URLs are not composed at construction time.
        self.assertEqual(session.config.region, "auto")
        self.assertEqual(session.config.console_endpoint_url, "")
        self.assertEqual(session.config.ingress_endpoint_url, "")

        async def fake_fetch(**_kwargs):
            return _region_body("eu-central")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fake_fetch):
            with patch(
                "spatius.avatar_session.aiohttp.ClientSession",
                new=lambda *a, **k: self._token_ok_session(),
            ):
                await session.init()

        self.assertEqual(session.config.region, "eu-central")
        self.assertEqual(
            session.config.console_endpoint_url,
            "https://console.eu-central.spatius.ai/v1/console",
        )
        self.assertEqual(
            session.config.ingress_endpoint_url,
            "wss://api.eu-central.spatius.ai/v2/driveningress",
        )

    async def test_init_bootstrap_failure_falls_back_and_still_inits(self):
        session = self._mk_session(app_id="app-1")

        async def fail_fetch(**_kwargs):
            raise BootstrapError("network down")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fail_fetch):
            with patch(
                "spatius.avatar_session.aiohttp.ClientSession",
                new=lambda *a, **k: self._token_ok_session(),
            ):
                await session.init()

        self.assertEqual(session.config.region, "us-west")
        self.assertEqual(
            session.config.console_endpoint_url,
            "https://console.us-west.spatius.ai/v1/console",
        )
        self.assertEqual(
            session.config.ingress_endpoint_url,
            "wss://api.us-west.spatius.ai/v2/driveningress",
        )

    async def test_init_skips_bootstrap_for_concrete_region(self):
        session = self._mk_session(region="eu-central")

        async def fail_fetch(**_kwargs):
            raise AssertionError("bootstrap must not be called")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fail_fetch):
            with patch(
                "spatius.avatar_session.aiohttp.ClientSession",
                new=lambda *a, **k: self._token_ok_session(),
            ):
                await session.init()

        self.assertEqual(session.config.region, "eu-central")
        self.assertEqual(
            session.config.console_endpoint_url,
            "https://console.eu-central.spatius.ai/v1/console",
        )

    async def test_init_skips_bootstrap_for_explicit_urls(self):
        session = self._mk_session(
            console_endpoint_url="https://console.example.com/v1/console",
            ingress_endpoint_url="wss://api.example.com/v2/driveningress",
        )

        async def fail_fetch(**_kwargs):
            raise AssertionError("bootstrap must not be called")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fail_fetch):
            with patch(
                "spatius.avatar_session.aiohttp.ClientSession",
                new=lambda *a, **k: self._token_ok_session(),
            ):
                await session.init()

        self.assertEqual(
            session.config.console_endpoint_url,
            "https://console.example.com/v1/console",
        )
        self.assertEqual(
            session.config.ingress_endpoint_url,
            "wss://api.example.com/v2/driveningress",
        )

    async def test_init_partial_explicit_urls_compose_rest_from_default_region(self):
        session = self._mk_session(
            console_endpoint_url="https://console.example.com/v1/console",
        )

        async def fail_fetch(**_kwargs):
            raise AssertionError("bootstrap must not be called")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fail_fetch):
            with patch(
                "spatius.avatar_session.aiohttp.ClientSession",
                new=lambda *a, **k: self._token_ok_session(),
            ):
                await session.init()

        # Historical behavior: the missing ingress URL falls back to us-west.
        self.assertEqual(
            session.config.ingress_endpoint_url,
            "wss://api.us-west.spatius.ai/v2/driveningress",
        )

    async def test_explicit_region_composes_urls_at_construction(self):
        session = self._mk_session(region="cn-beijing")
        self.assertEqual(
            session.config.console_endpoint_url,
            "https://console.cn-beijing.spatialwalk.top/v1/console",
        )
        self.assertEqual(
            session.config.ingress_endpoint_url,
            "wss://api.cn-beijing.spatialwalk.top/v2/driveningress",
        )


if __name__ == "__main__":
    unittest.main()
