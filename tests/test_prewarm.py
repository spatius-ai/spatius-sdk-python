import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import spatius.bootstrap as bootstrap
from spatius import new_avatar_session, prewarm
from spatius.bootstrap import resolve_region
from spatius.token_cache import clear_cached_session_tokens, store_session_token


def _region_body(current: str) -> dict:
    return {"region": {"current": current, "candidates": ["us-west"]}}


async def _no_tls_warm(url, *, timeout=5.0):
    return False


class TestPrewarm(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bootstrap._cached_region = None
        bootstrap._cached_at = 0.0
        clear_cached_session_tokens()

    def tearDown(self):
        bootstrap._cached_region = None
        bootstrap._cached_at = 0.0
        clear_cached_session_tokens()

    async def test_resolves_and_caches_auto_region(self):
        async def fake_fetch(**_kwargs):
            return _region_body("eu-central")

        with (
            patch("spatius.bootstrap.fetch_bootstrap", new=fake_fetch),
            patch("spatius.prewarm.warm_tls_connection", new=_no_tls_warm),
        ):
            result = await prewarm(app_id="app-1")

        self.assertEqual(result.region, "eu-central")
        self.assertEqual(
            result.console_endpoint_url,
            "https://console.eu-central.spatius.ai/v1/console",
        )
        self.assertEqual(
            result.ingress_endpoint_url,
            "wss://api.eu-central.spatius.ai/v2/driveningress",
        )
        self.assertFalse(result.session_token_prefetched)

        # the resolved region is cached: a later resolve must not re-fetch
        async def fail_fetch(**_kwargs):
            raise AssertionError("bootstrap must not be called within the TTL")

        with patch("spatius.bootstrap.fetch_bootstrap", new=fail_fetch):
            region = await resolve_region(app_id="app-1", requested_region="auto")
        self.assertEqual(region, "eu-central")

    async def test_concrete_region_skips_bootstrap(self):
        async def fail_fetch(**_kwargs):
            raise AssertionError("bootstrap must not be called")

        with (
            patch("spatius.bootstrap.fetch_bootstrap", new=fail_fetch),
            patch("spatius.prewarm.warm_tls_connection", new=_no_tls_warm),
        ):
            result = await prewarm(app_id="app-1", region="ap-southeast")

        self.assertEqual(result.region, "ap-southeast")

    async def test_tls_warm_reports_warmed_hosts(self):
        warmed = []

        async def fake_warm(url, *, timeout=5.0):
            warmed.append(url)
            return True

        with (
            patch("spatius.prewarm.warm_tls_connection", new=fake_warm),
            patch("spatius.bootstrap.fetch_bootstrap", new=self._eu_fetch),
        ):
            result = await prewarm(app_id="app-1")

        self.assertEqual(
            result.tls_warmed,
            ["console.eu-central.spatius.ai", "api.eu-central.spatius.ai"],
        )
        self.assertEqual(len(warmed), 2)

    async def test_tls_warm_failure_is_best_effort(self):
        with (
            patch("spatius.prewarm.warm_tls_connection", new=_no_tls_warm),
            patch("spatius.bootstrap.fetch_bootstrap", new=self._eu_fetch),
        ):
            result = await prewarm(app_id="app-1")

        self.assertEqual(result.tls_warmed, [])
        self.assertEqual(result.region, "eu-central")

    async def test_region_resolution_failure_never_raises(self):
        async def fail_resolve(*_args, **_kwargs):
            raise RuntimeError("boom")

        with patch("spatius.prewarm.resolve_session_endpoints", new=fail_resolve):
            result = await prewarm(app_id="app-1")

        self.assertIsNone(result.region)
        self.assertEqual(result.tls_warmed, [])

    async def test_prefetch_session_token_consumed_by_init(self):
        async def fake_token(config, *, timeout=10.0):
            return "tok-prefetched"

        with (
            patch("spatius.bootstrap.fetch_bootstrap", new=self._eu_fetch),
            patch("spatius.prewarm.fetch_session_token", new=fake_token),
            patch("spatius.prewarm.warm_tls_connection", new=_no_tls_warm),
        ):
            result = await prewarm(
                app_id="app-1", api_key="api", prefetch_session_token=True
            )
        self.assertTrue(result.session_token_prefetched)

        session = new_avatar_session(
            app_id="app-1",
            api_key="api",
            region="eu-central",
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        async def fail_token(*_args, **_kwargs):
            raise AssertionError("init() must reuse the prefetched token")

        with patch("spatius.avatar_session.fetch_session_token", new=fail_token):
            await session.init()
        self.assertEqual(session._session_token, "tok-prefetched")

    async def test_prefetch_requires_api_key(self):
        with (
            patch("spatius.bootstrap.fetch_bootstrap", new=self._eu_fetch),
            patch("spatius.prewarm.warm_tls_connection", new=_no_tls_warm),
        ):
            with self.assertLogs("spatius.prewarm", level="WARNING"):
                result = await prewarm(app_id="app-1", prefetch_session_token=True)
        self.assertFalse(result.session_token_prefetched)

    async def test_near_expiry_token_is_not_reused(self):
        store_session_token(
            api_key="api",
            app_id="app-1",
            console_endpoint_url="https://console.eu-central.spatius.ai/v1/console",
            token="tok-stale",
            expire_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        )
        session = new_avatar_session(
            app_id="app-1",
            api_key="api",
            region="eu-central",
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        async def fresh_token(config, *, timeout=10.0):
            return "tok-fresh"

        with patch("spatius.avatar_session.fetch_session_token", new=fresh_token):
            await session.init()
        self.assertEqual(session._session_token, "tok-fresh")

    async def test_prefetch_token_failure_is_best_effort(self):
        async def fail_token(config, *, timeout=10.0):
            raise RuntimeError("console unreachable")

        with (
            patch("spatius.bootstrap.fetch_bootstrap", new=self._eu_fetch),
            patch("spatius.prewarm.fetch_session_token", new=fail_token),
            patch("spatius.prewarm.warm_tls_connection", new=_no_tls_warm),
        ):
            result = await prewarm(
                app_id="app-1", api_key="api", prefetch_session_token=True
            )
        self.assertFalse(result.session_token_prefetched)
        self.assertEqual(result.region, "eu-central")

    @staticmethod
    async def _eu_fetch(**_kwargs):
        return _region_body("eu-central")


class TestPrewarmTelemetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bootstrap._cached_region = None
        bootstrap._cached_at = 0.0
        clear_cached_session_tokens()

    def tearDown(self):
        bootstrap._cached_region = None
        bootstrap._cached_at = 0.0
        clear_cached_session_tokens()

    async def test_prewarm_emits_span_and_duration_metric(self):
        span = MagicMock()
        with (
            patch("spatius.prewarm.start_span", return_value=span) as start_span,
            patch("spatius.prewarm.finish_span") as finish_span,
            patch("spatius.prewarm.record_metric") as record_metric,
            patch("spatius.bootstrap.fetch_bootstrap", new=TestPrewarm._eu_fetch),
            patch("spatius.prewarm.warm_tls_connection", new=_no_tls_warm),
        ):
            result = await prewarm(app_id="app-1")

        self.assertEqual(start_span.call_args.args[0], "spatius.prewarm")
        self.assertEqual(start_span.call_args.args[1]["app_id"], "app-1")

        metric = record_metric.call_args
        self.assertEqual(metric.args[0], "spatius.prewarm.duration")
        self.assertGreaterEqual(metric.args[1], 0)
        self.assertEqual(metric.args[2]["success"], True)
        self.assertEqual(metric.args[2]["region"], "eu-central")
        self.assertEqual(metric.args[2]["tls_warmed"], 0)
        self.assertEqual(metric.args[2]["session_token_prefetched"], False)

        finish_span.assert_called_once()
        self.assertIs(finish_span.call_args.args[0], span)
        self.assertEqual(
            finish_span.call_args.kwargs["attributes"]["resolved_region"], "eu-central"
        )
        self.assertTrue(result.region == "eu-central")

    async def test_prewarm_metric_marks_failure(self):
        async def fail_resolve(*_args, **_kwargs):
            raise RuntimeError("boom")

        with (
            patch("spatius.prewarm.resolve_session_endpoints", new=fail_resolve),
            patch("spatius.prewarm.record_metric") as record_metric,
        ):
            result = await prewarm(app_id="app-1")

        # region failure is still a "successful" (best-effort) prewarm, with
        # nothing warmed
        self.assertIsNone(result.region)
        metric = record_metric.call_args
        self.assertEqual(metric.args[2]["success"], True)
        self.assertNotIn("region", metric.args[2])


class TestInitCacheTelemetry(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bootstrap._cached_region = None
        bootstrap._cached_at = 0.0
        clear_cached_session_tokens()

    def tearDown(self):
        bootstrap._cached_region = None
        bootstrap._cached_at = 0.0
        clear_cached_session_tokens()

    async def test_init_metric_reports_token_cache_hit(self):
        store_session_token(
            api_key="api",
            app_id="app-1",
            console_endpoint_url="https://console.eu-central.spatius.ai/v1/console",
            token="tok-cached",
            expire_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        session = new_avatar_session(
            app_id="app-1",
            api_key="api",
            region="eu-central",
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        with patch("spatius.avatar_session.record_metric") as record_metric:
            await session.init()

        metric = record_metric.call_args
        self.assertEqual(metric.args[0], "avatar.session.init.duration")
        attributes = metric.args[2]
        self.assertEqual(attributes["success"], True)
        self.assertEqual(attributes["token_cache_hit"], True)
        # concrete region: no resolution happened, attribute omitted
        self.assertNotIn("region_cache_hit", attributes)

    async def test_init_metric_reports_region_cache_hit_and_token_miss(self):
        bootstrap._cached_region = "eu-central"
        bootstrap._cached_at = bootstrap.time.monotonic()

        session = new_avatar_session(
            app_id="app-1",
            api_key="api",
            region="auto",
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        async def fail_bootstrap(**_kwargs):
            raise AssertionError("bootstrap must not be called within the TTL")

        async def fake_token(config, *, timeout=10.0):
            return "tok-fresh"

        with (
            patch("spatius.bootstrap.fetch_bootstrap", new=fail_bootstrap),
            patch("spatius.avatar_session.fetch_session_token", new=fake_token),
            patch("spatius.avatar_session.record_metric") as record_metric,
        ):
            await session.init()

        attributes = record_metric.call_args.args[2]
        self.assertEqual(attributes["region_cache_hit"], True)
        self.assertEqual(attributes["token_cache_hit"], False)
        self.assertEqual(attributes["region"], "eu-central")


if __name__ == "__main__":
    unittest.main()
