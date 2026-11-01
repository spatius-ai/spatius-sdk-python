import asyncio
import json
import traceback
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import parse_qs, urlparse

import aiohttp

from spatius import AvatarDetail, AvatarSDKError, AvatarSDKErrorCode, new_avatar_session
from spatius.avatar_detail import AVATAR_DETAIL_TIMEOUT_S


class _Response:
    def __init__(self, payload=None, *, status=200, error=None, gate=None):
        self.status = status
        self.payload = (
            payload
            if payload is not None
            else {
                "availableEmotions": ["neutral", "喜悦 微弱", "HAPPY"],
                "defaultEmotion": "neutral",
                "assets": [
                    {
                        "assetKey": "identity",
                        "resource": {"remote": "https://cdn.example/avatar"},
                    }
                ],
            }
        )
        self.error = error
        self.gate = gate
        self.entered = asyncio.Event()

    async def json(self, **kwargs):
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _HTTPClient:
    def __init__(self, response):
        self.get = Mock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class TestAvatarDetail(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connector_patch = patch(
            "spatius.avatar_detail.new_connector", return_value=None
        )
        self.connector_patch.start()
        self.addCleanup(self.connector_patch.stop)

    def session(self, **kwargs):
        config = {
            "region": "us-west",
            "app_id": "test-app",
            "api_key": "test-api-key",
            "avatar_id": "test-avatar",
        }
        config.update(kwargs)
        session = new_avatar_session(**config)
        session._session_token = "private-session-token"
        return session

    def http(self, response):
        client = _HTTPClient(response)
        mock = patch("spatius.avatar_detail.aiohttp.ClientSession", return_value=client)
        factory = mock.start()
        self.addCleanup(mock.stop)
        return client, factory

    async def test_fetches_flat_metadata_and_caches_immutable_result(self):
        client, factory = self.http(_Response())
        session = self.session()
        detail = await session.get_avatar_detail()
        self.assertIsInstance(detail, AvatarDetail)
        self.assertEqual(detail.available_emotions, ("neutral", "喜悦 微弱", "HAPPY"))
        self.assertEqual(detail.default_emotion, "neutral")
        with self.assertRaises(FrozenInstanceError):
            detail.default_emotion = "HAPPY"
        self.assertIs(await session.get_avatar_detail(), detail)
        client.get.assert_called_once_with(
            "https://api.us-west.spatius.ai/v3/avatar/test-avatar",
            headers={
                "Accept": "application/json",
                "X-App-ID": "test-app",
                "X-Session-Key": "private-session-token",
            },
            allow_redirects=False,
        )
        self.assertEqual(
            factory.call_args.kwargs["timeout"].total, AVATAR_DETAIL_TIMEOUT_S
        )
        self.assertIsNone(session._connection)

    async def test_query_auth_option_still_uses_headers_for_metadata(self):
        client, _ = self.http(_Response())
        session = self.session(use_query_auth=True)
        await session.get_avatar_detail()
        self.assertEqual(
            client.get.call_args.kwargs["headers"],
            {
                "Accept": "application/json",
                "X-App-ID": "test-app",
                "X-Session-Key": "private-session-token",
            },
        )
        self.assertNotIn("params", client.get.call_args.kwargs)
        self.assertNotIn("sessionKey", client.get.call_args.args[0])

    async def test_resolved_region_is_used_without_automatic_metadata_fetch(self):
        client, _ = self.http(_Response())
        session = new_avatar_session(app_id="app", api_key="key", avatar_id="avatar")
        with (
            patch(
                "spatius.avatar_session.resolve_region_with_cache_info",
                new=AsyncMock(return_value=("cn-beijing", False)),
            ),
            patch(
                "spatius.avatar_session.fetch_session_token",
                new=AsyncMock(return_value="token"),
            ),
        ):
            await session.init()
        client.get.assert_not_called()
        await session.get_avatar_detail()
        self.assertEqual(
            client.get.call_args.args[0],
            "https://api.cn-beijing.spatialwalk.top/v3/avatar/avatar",
        )

    async def test_explicit_endpoint_prefix_port_and_avatar_escaping(self):
        for scheme, expected in [
            ("ws", "http"),
            ("wss", "https"),
            ("http", "http"),
            ("https", "https"),
        ]:
            with self.subTest(scheme=scheme):
                client, _ = self.http(_Response())
                session = self.session(
                    ingress_endpoint_url=f"{scheme}://example.com:9443/proxy/v3/driveningress/?route=test&sessionKey=stale&appId=stale#ignored",
                    avatar_id="avatar /?雪",
                )
                await session.get_avatar_detail()
                endpoint = client.get.call_args.args[0]
                parsed = urlparse(endpoint)
                self.assertEqual(parsed.scheme, expected)
                self.assertEqual(parsed.netloc, "example.com:9443")
                self.assertEqual(
                    parsed.path, "/proxy/v3/avatar/avatar%20%2F%3F%E9%9B%AA"
                )
                self.assertEqual(parse_qs(parsed.query), {"route": ["test"]})
                self.assertEqual(parsed.fragment, "")

    async def test_concurrent_callers_share_one_successful_request(self):
        gate = asyncio.Event()
        response = _Response(gate=gate)
        client, _ = self.http(response)
        session = self.session()
        first = asyncio.create_task(session.get_avatar_detail())
        await response.entered.wait()
        second = asyncio.create_task(session.get_avatar_detail())
        gate.set()
        first_result, second_result = await asyncio.gather(first, second)
        self.assertIs(first_result, second_result)
        client.get.assert_called_once()

    async def test_failed_request_can_be_retried(self):
        response = _Response(status=503)
        client, _ = self.http(response)
        session = self.session()
        with self.assertRaises(AvatarSDKError):
            await session.get_avatar_detail()
        response.status = 200
        detail = await session.get_avatar_detail()
        self.assertEqual(detail.default_emotion, "neutral")
        self.assertEqual(client.get.call_count, 2)

    async def test_cancelled_request_can_be_retried(self):
        gate = asyncio.Event()
        response = _Response(gate=gate)
        client, _ = self.http(response)
        session = self.session()
        task = asyncio.create_task(session.get_avatar_detail())
        await response.entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        gate.set()
        await session.get_avatar_detail()
        self.assertEqual(client.get.call_count, 2)

    async def test_cancelling_waiter_does_not_cancel_active_lookup(self):
        gate = asyncio.Event()
        response = _Response(gate=gate)
        client, _ = self.http(response)
        session = self.session()
        active = asyncio.create_task(session.get_avatar_detail())
        await response.entered.wait()
        waiter = asyncio.create_task(session.get_avatar_detail())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        gate.set()
        detail = await active
        self.assertIs(await session.get_avatar_detail(), detail)
        client.get.assert_called_once()

    async def test_metadata_is_cached_only_for_its_session(self):
        client, _ = self.http(_Response())
        await self.session().get_avatar_detail()
        await self.session(avatar_id="another-avatar").get_avatar_detail()
        self.assertEqual(client.get.call_count, 2)

    async def test_initialization_and_config_required(self):
        client, _ = self.http(_Response())
        session = self.session()
        session._session_token = None
        with self.assertRaisesRegex(ValueError, "Session not initialized"):
            await session.get_avatar_detail()
        for name, message in [
            ("avatar_id", "Missing avatar ID"),
            ("app_id", "Missing app ID"),
            ("ingress_endpoint_url", "Missing ingress endpoint URL"),
        ]:
            with self.subTest(name=name):
                session = self.session()
                setattr(session.config, name, "")
                with self.assertRaisesRegex(ValueError, message):
                    await session.get_avatar_detail()
        client.get.assert_not_called()

    async def test_invalid_endpoint_is_rejected_before_network(self):
        client, _ = self.http(_Response())
        for endpoint in [
            "ftp://example.com/v3/driveningress",
            "wss:///v3/driveningress",
            "wss://user:secret@example.com/v3/driveningress",
            "wss://example.com:bad/v3/driveningress",
            "wss://example.com/custom/path",
        ]:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ValueError):
                    await self.session(
                        ingress_endpoint_url=endpoint
                    ).get_avatar_detail()
        client.get.assert_not_called()

    async def test_rejects_invalid_metadata_and_envelopes(self):
        payloads = [
            [],
            {},
            {"data": {"availableEmotions": ["neutral"], "defaultEmotion": "neutral"}},
            {"errors": [{"detail": "private"}]},
            {"availableEmotions": [], "defaultEmotion": ""},
            {"availableEmotions": "neutral", "defaultEmotion": "neutral"},
            {"availableEmotions": ["neutral", "neutral"], "defaultEmotion": "neutral"},
            {"availableEmotions": ["neutral", ""], "defaultEmotion": "neutral"},
            {"availableEmotions": ["neutral", "  "], "defaultEmotion": "neutral"},
            {"availableEmotions": ["neutral", 1], "defaultEmotion": "neutral"},
            {"availableEmotions": ["neutral", {}], "defaultEmotion": "neutral"},
            {"availableEmotions": ["neutral"], "defaultEmotion": None},
            {"availableEmotions": ["neutral"], "defaultEmotion": "NEUTRAL"},
            {"availableEmotions": ["neutral"], "defaultEmotion": ""},
            {"availableEmotions": ["neutral"]},
            {
                "availableEmotions": ["neutral"],
                "defaultEmotion": "neutral",
                "errors": ["error"],
            },
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                self.http(_Response(payload))
                with self.assertRaises(AvatarSDKError) as raised:
                    await self.session().get_avatar_detail()
                self.assertEqual(
                    raised.exception.code, AvatarSDKErrorCode.protocolError
                )
                self.assertEqual(raised.exception.phase, "avatar_detail")
                self.assertIsNone(raised.exception.raw_body)

    async def test_invalid_json_does_not_leak_response(self):
        self.http(
            _Response(
                error=json.JSONDecodeError(
                    "private-server-body", "private-server-body", 0
                )
            )
        )
        with self.assertRaises(AvatarSDKError) as raised:
            await self.session().get_avatar_detail()
        self.assertEqual(raised.exception.code, AvatarSDKErrorCode.protocolError)
        self.assertNotIn(
            "private-server-body", "".join(traceback.format_exception(raised.exception))
        )

    async def test_http_failures_do_not_read_or_expose_server_body(self):
        for status, code in [
            (302, AvatarSDKErrorCode.invalidRequest),
            (400, AvatarSDKErrorCode.invalidRequest),
            (401, AvatarSDKErrorCode.sessionTokenInvalid),
            (403, AvatarSDKErrorCode.invalidRequest),
            (404, AvatarSDKErrorCode.avatarNotFound),
            (503, AvatarSDKErrorCode.serverError),
        ]:
            with self.subTest(status=status):
                response = _Response(
                    status=status, error=AssertionError("should not read body")
                )
                self.http(response)
                with self.assertRaises(AvatarSDKError) as raised:
                    await self.session().get_avatar_detail()
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.http_status, status)
                self.assertIsNone(raised.exception.raw_body)
                self.assertIsNone(raised.exception.server_detail)
                self.assertFalse(response.entered.is_set())

    async def test_network_failures_are_sanitized_and_retryable(self):
        for error in [
            asyncio.TimeoutError(),
            aiohttp.ClientConnectionError(
                "https://private-token.example?sessionKey=private-session-token"
            ),
        ]:
            with self.subTest(error=type(error)):
                client, _ = self.http(_Response())
                client.get.side_effect = error
                session = self.session(use_query_auth=True)
                with self.assertRaises(AvatarSDKError) as raised:
                    await session.get_avatar_detail()
                self.assertEqual(
                    raised.exception.code, AvatarSDKErrorCode.connectionFailed
                )
                self.assertEqual(raised.exception.phase, "avatar_detail")
                self.assertNotIn(
                    "private-session-token",
                    "".join(traceback.format_exception(raised.exception)),
                )
                client.get.side_effect = None
                self.assertEqual(
                    (await session.get_avatar_detail()).default_emotion, "neutral"
                )
