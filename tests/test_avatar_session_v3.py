import asyncio
import importlib.util
import unittest
from datetime import datetime, timedelta, timezone
from typing import Optional, cast
from unittest.mock import patch

import aiohttp
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from spatius import (
    AgoraTransportConfig,
    AudioFormat,
    AvatarSDKError,
    AvatarSDKErrorCode,
    LiveKitTransportConfig,
    OggOpusEncoderConfig,
    SessionTokenError,
    new_avatar_session,
)
from spatius.proto.generated import message_pb2

_HAS_OPUSLIB = importlib.util.find_spec("opuslib_next") is not None


class _DummyTask:
    def __init__(self):
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def __await__(self):
        if False:  # pragma: no cover
            yield None
        return None


class _FakeWebSocket:
    def __init__(self, recv_messages: Optional[list[bytes]] = None):
        self.sent: list[bytes] = []
        self._recv_q: asyncio.Queue[bytes] = asyncio.Queue()
        for m in recv_messages or []:
            self._recv_q.put_nowait(m)
        self._iter_q: asyncio.Queue[bytes] = asyncio.Queue()
        self._closed = False

    async def send(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    async def recv(self):
        return await self._recv_q.get()

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        # Block until cancelled by the session close().
        return await self._iter_q.get()


class _ClosingWebSocket:
    def __init__(self, exc: Exception):
        self._exc = exc
        self._closed = False

    async def close(self) -> None:
        self._closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise self._exc


class _FakeHTTPResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


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


def _mk_confirm(connection_id: str) -> bytes:
    m = message_pb2.Message()
    m.type = message_pb2.MESSAGE_SERVER_CONFIRM_SESSION
    m.server_confirm_session.connection_id = connection_id
    return m.SerializeToString()


def _mk_server_error(code: int = 123, message: str = "bad") -> bytes:
    m = message_pb2.Message()
    m.type = message_pb2.MESSAGE_SERVER_ERROR
    m.server_error.connection_id = "cid"
    m.server_error.req_id = "rid"
    m.server_error.code = code
    m.server_error.message = message
    return m.SerializeToString()


class TestAvatarSessionV3(unittest.IsolatedAsyncioTestCase):
    def test_new_avatar_session_defaults_endpoints_from_region(self):
        session = new_avatar_session(region="eu-central")

        self.assertEqual(
            session.config.console_endpoint_url,
            "https://console.eu-central.spatius.ai/v1/console",
        )
        self.assertEqual(
            session.config.ingress_endpoint_url,
            "wss://api.eu-central.spatius.ai/v3/driveningress",
        )

    def test_new_avatar_session_defaults_cn_endpoints_from_region(self):
        session = new_avatar_session(region="cn-beijing")

        self.assertEqual(
            session.config.console_endpoint_url,
            "https://console.cn-beijing.spatialwalk.top/v1/console",
        )
        self.assertEqual(
            session.config.ingress_endpoint_url,
            "wss://api.cn-beijing.spatialwalk.top/v3/driveningress",
        )

    def test_new_avatar_session_explicit_endpoints_override_region(self):
        session = new_avatar_session(
            region="eu-central",
            console_endpoint_url="https://console.example.com/v1/console",
            ingress_endpoint_url="wss://api.example.com/v3/driveningress",
        )

        self.assertEqual(
            session.config.console_endpoint_url,
            "https://console.example.com/v1/console",
        )
        self.assertEqual(
            session.config.ingress_endpoint_url,
            "wss://api.example.com/v3/driveningress",
        )

    async def test_init_raises_structured_session_token_error(self):
        session = new_avatar_session(
            console_endpoint_url="https://console.example.com",
            api_key="api",
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        body = (
            '{"errors":[{"id":"INVALID_ARGUMENT","status":400,'
            '"code":"INVALID_ARGUMENT","title":"Invalid Argument",'
            '"detail":"expire_at must be in the future"}]}'
        )

        with patch(
            "spatius.avatar_session.aiohttp.ClientSession",
            new=lambda *a, **k: _FakeClientSession(_FakeHTTPResponse(400, body)),
        ):
            with self.assertRaises(SessionTokenError) as cm:
                await session.init()

        err = cm.exception
        self.assertIsInstance(err, AvatarSDKError)
        self.assertEqual(err.code, AvatarSDKErrorCode.invalidRequest)
        self.assertEqual(err.phase, "session_token")
        self.assertEqual(err.http_status, 400)
        self.assertEqual(err.server_code, "INVALID_ARGUMENT")
        self.assertEqual(err.server_title, "Invalid Argument")
        self.assertEqual(err.server_detail, "expire_at must be in the future")
        self.assertIn("Failed to create session token (HTTP 400)", err.message)

    async def test_init_wraps_transport_errors(self):
        session = new_avatar_session(
            console_endpoint_url="https://console.example.com",
            api_key="api",
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )

        with patch(
            "spatius.avatar_session.aiohttp.ClientSession",
            new=lambda *a, **k: _FakeClientSession(
                error=aiohttp.ClientConnectionError("network down")
            ),
        ):
            with self.assertRaises(SessionTokenError) as cm:
                await session.init()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.connectionFailed)
        self.assertEqual(err.phase, "session_token")
        self.assertIn("Failed to create session token", err.message)

    async def test_start_header_auth_builds_url_and_headers_and_handshakes(self):
        captured: dict = {}

        async def fake_connect(url, additional_headers=None, **_kwargs):
            captured["url"] = url
            captured["headers"] = dict(
                additional_headers or _kwargs.get("extra_headers") or {}
            )
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            # Don't run background loop in unit tests; also avoid "coroutine never awaited".
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            use_query_auth=False,
        )
        session._session_token = "tok-1"  # bypass init()

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            cid = await session.start()

        self.assertEqual(cid, "server-conn")
        self.assertIn("id=avatar-1", captured["url"])
        self.assertNotIn("appId=", captured["url"])
        self.assertEqual(
            captured["headers"],
            {"X-App-ID": "app-1", "X-Session-Key": "tok-1"},
        )

        # Verify first sent message is ClientConfigureSession
        fake_ws: _FakeWebSocket = session._connection  # type: ignore[assignment]
        self.assertIsNotNone(fake_ws)
        self.assertGreaterEqual(len(fake_ws.sent), 1)
        first = message_pb2.Message()
        first.ParseFromString(fake_ws.sent[0])
        self.assertEqual(first.type, message_pb2.MESSAGE_CLIENT_CONFIGURE_SESSION)
        self.assertEqual(first.client_configure_session.audio_sample_rate, 16000)

        await session.close()

    async def test_start_wss_shares_process_ssl_context(self):
        import ssl as ssl_module

        from spatius.net import get_ssl_context

        captured: dict = {}

        async def fake_connect(url, additional_headers=None, **kwargs):
            captured.update(kwargs)
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            await session.start()

        self.assertIsInstance(captured.get("ssl"), ssl_module.SSLContext)
        self.assertIs(captured["ssl"], get_ssl_context())

        await session.close()

    async def test_start_ws_omits_ssl_context(self):
        captured: dict = {}

        async def fake_connect(url, additional_headers=None, **kwargs):
            captured.update(kwargs)
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="http://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            await session.start()

        self.assertNotIn("ssl", captured)

        await session.close()

    async def test_start_query_auth_builds_query_params(self):
        captured: dict = {}

        async def fake_connect(url, additional_headers=None, **_kwargs):
            captured["url"] = url
            captured["headers"] = dict(
                additional_headers or _kwargs.get("extra_headers") or {}
            )
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            use_query_auth=True,
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            await session.start()

        self.assertIn("id=avatar-1", captured["url"])
        self.assertIn("appId=app-1", captured["url"])
        self.assertIn("sessionKey=tok-1", captured["url"])
        self.assertEqual(captured["headers"], {})

        await session.close()

    async def test_start_sends_extra_params(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        extra_params = {
            "request_label": "example",
            "future_option": "enabled",
        }
        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            extra_params=extra_params,
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            await session.start()

        fake_ws: _FakeWebSocket = session._connection  # type: ignore[assignment]
        first = message_pb2.Message()
        first.ParseFromString(fake_ws.sent[0])

        self.assertEqual(
            first.client_configure_session.extra_params,
            extra_params,
        )

        await session.close()

    async def test_start_with_agora_sends_numeric_uid_including_default_zero(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        for identity in ({}, {"uid": 123}, {"uid": 2**32 - 1}):
            with self.subTest(identity=identity):
                session = new_avatar_session(
                    ingress_endpoint_url="https://ingress.example.com",
                    console_endpoint_url="https://console.example.com",
                    api_key="api",
                    avatar_id="avatar-1",
                    app_id="app-1",
                    transport=AgoraTransportConfig(
                        channel_name="room", token="rtc-token", **identity
                    ),
                )
                session._session_token = "tok-1"
                with (
                    patch("spatius.avatar_session.websockets.connect", new=fake_connect),
                    patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
                ):
                    await session.start()

                fake_ws = cast(_FakeWebSocket, session._connection)
                first = message_pb2.Message.FromString(fake_ws.sent[0])
                self.assertEqual(
                    first.client_configure_session.transport.WhichOneof("destination"),
                    "agora",
                )
                config = first.client_configure_session.transport.agora
                self.assertEqual(config.uid, identity.get("uid", 0))
                self.assertEqual(config.channel_name, "room")
                self.assertEqual(config.token, "rtc-token")
                await session.close()

    async def test_start_with_livekit_transport_sends_new_fields(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            transport=LiveKitTransportConfig(
                url="wss://livekit.example.com",
                token="lk-token",
                agent_identity="source-agent",
                extra_attributes={"role": "avatar", "region": "us-west"},
                idle_timeout=120,
            ),
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            await session.start()

        fake_ws: _FakeWebSocket = session._connection  # type: ignore[assignment]
        self.assertIsNotNone(fake_ws)
        self.assertGreaterEqual(len(fake_ws.sent), 1)

        first = message_pb2.Message()
        first.ParseFromString(fake_ws.sent[0])

        self.assertEqual(first.type, message_pb2.MESSAGE_CLIENT_CONFIGURE_SESSION)
        self.assertEqual(
            first.client_configure_session.transport.WhichOneof("destination"),
            "livekit",
        )
        self.assertEqual(
            first.client_configure_session.transport.livekit.extra_attributes,
            {"role": "avatar", "region": "us-west"},
        )
        self.assertEqual(
            first.client_configure_session.transport.livekit.token, "lk-token"
        )
        self.assertEqual(
            first.client_configure_session.transport.livekit.idle_timeout, 120
        )

        await session.close()

    async def test_start_with_ogg_opus_sends_audio_format(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            sample_rate=24000,
            bitrate=32000,
            audio_format=AudioFormat.OGG_OPUS,
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            await session.start()

        fake_ws: _FakeWebSocket = session._connection  # type: ignore[assignment]
        self.assertIsNotNone(fake_ws)
        self.assertGreaterEqual(len(fake_ws.sent), 1)

        first = message_pb2.Message()
        first.ParseFromString(fake_ws.sent[0])

        self.assertEqual(first.type, message_pb2.MESSAGE_CLIENT_CONFIGURE_SESSION)
        self.assertEqual(first.client_configure_session.audio_sample_rate, 24000)
        self.assertEqual(
            first.client_configure_session.audio_format,
            message_pb2.AUDIO_FORMAT_OGG_OPUS,
        )

        await session.close()

    async def test_send_audio_ogg_opus_passthrough_keeps_preencoded_bytes(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            sample_rate=24000,
            audio_format=AudioFormat.OGG_OPUS,
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            await session.start()

        fake_ws: _FakeWebSocket = session._connection  # type: ignore[assignment]
        pre_encoded = b"OggS-pre-encoded"
        req_id = await session.send_audio(pre_encoded, end=True)

        audio_msg = message_pb2.Message()
        audio_msg.ParseFromString(fake_ws.sent[1])

        self.assertEqual(audio_msg.client_audio_input.req_id, req_id)
        self.assertEqual(audio_msg.client_audio_input.audio, pre_encoded)
        self.assertTrue(audio_msg.client_audio_input.end)

        await session.close()

    @unittest.skipUnless(
        _HAS_OPUSLIB, "opuslib-next is required for internal encoder tests"
    )
    async def test_send_audio_internal_encoder_outputs_ogg_opus_and_callback(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        encoded_results: list[tuple[str, bytes]] = []
        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            sample_rate=24000,
            bitrate=32000,
            audio_format=AudioFormat.OGG_OPUS,
            ogg_opus_encoder=OggOpusEncoderConfig(),
            on_encoded_audio=lambda req_id, payload: encoded_results.append(
                (req_id, bytes(payload))
            ),
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            await session.start()

        fake_ws: _FakeWebSocket = session._connection  # type: ignore[assignment]
        pcm_frame = b"\x00\x00" * 480
        req_id = await session.send_audio(pcm_frame, end=True)

        audio_msg = message_pb2.Message()
        audio_msg.ParseFromString(fake_ws.sent[1])

        self.assertEqual(audio_msg.client_audio_input.req_id, req_id)
        self.assertEqual(audio_msg.client_audio_input.audio[:4], b"OggS")
        self.assertTrue(audio_msg.client_audio_input.end)
        self.assertEqual(
            encoded_results, [(req_id, audio_msg.client_audio_input.audio)]
        )

        await session.close()

    @unittest.skipUnless(
        _HAS_OPUSLIB, "opuslib-next is required for internal encoder tests"
    )
    async def test_send_audio_logs_callback_errors(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            sample_rate=24000,
            audio_format=AudioFormat.OGG_OPUS,
            ogg_opus_encoder=OggOpusEncoderConfig(),
            on_encoded_audio=lambda _req_id, _payload: (_ for _ in ()).throw(
                ValueError("callback failed")
            ),
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            await session.start()

        with self.assertLogs("spatius.avatar_session", level="ERROR") as logs:
            await session.send_audio(b"\x00\x00" * 480, end=True)

        self.assertIn("on_encoded_audio callback raised an exception", logs.output[0])

        await session.close()

    @unittest.skipUnless(
        _HAS_OPUSLIB, "opuslib-next is required for internal encoder tests"
    )
    async def test_send_audio_internal_encoder_buffers_until_frame_ready(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(recv_messages=[_mk_confirm("server-conn")])

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            sample_rate=24000,
            audio_format=AudioFormat.OGG_OPUS,
            ogg_opus_encoder=OggOpusEncoderConfig(),
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            await session.start()

        fake_ws: _FakeWebSocket = session._connection  # type: ignore[assignment]

        req_id = await session.send_audio(b"\x00\x00" * 100, end=False, emotion="happy")
        self.assertEqual(len(fake_ws.sent), 1)

        await session.send_audio(b"\x00\x00" * 380, end=True)
        self.assertEqual(len(fake_ws.sent), 2)

        audio_msg = message_pb2.Message()
        audio_msg.ParseFromString(fake_ws.sent[1])

        self.assertEqual(audio_msg.client_audio_input.req_id, req_id)
        self.assertEqual(audio_msg.client_audio_input.emotion, "happy")
        self.assertEqual(audio_msg.client_audio_input.audio[:4], b"OggS")
        self.assertTrue(audio_msg.client_audio_input.end)

        await session.close()

    async def test_motion_callback_preserves_complete_binary_envelope(self):
        got: list[tuple[bytes, bool]] = []
        session = new_avatar_session(
            region="us-west",
            transport_frames=lambda data, last: got.append((bytes(data), bool(last))),
        )
        motion = message_pb2.ServerResponseMotion(
            connection_id="cid",
            req_id="rid",
            avatar_id="avatar-1",
            emotion="happy",
            trace_context=message_pb2.TraceContext(
                traceparent="00-1234567890abcdef1234567890abcdef-1234567890abcdef-01",
                tracestate="vendor=value",
            ),
            animation=message_pb2.MotionAnimation(
                keyframes=[
                    message_pb2.MotionFrame(
                        expr=[0.25, 0.5],
                        body_pose=[0.0, 0.25, -0.5],
                        jaw_pose=[0.25, 0.0, 0.0],
                        eyelid=[0.25, 0.5],
                        leye_pose=[0.0, -0.25, 0.0],
                        reye_pose=[0.0, 0.25, 0.0],
                        emotion_weights=[0.25, 0.75],
                    )
                ]
            ),
        )
        envelope = message_pb2.Message(
            type=message_pb2.MESSAGE_SERVER_RESPONSE_MOTION,
            server_response_motion=motion,
        )
        first_payload = envelope.SerializeToString()
        await session._handle_binary_message(first_payload)
        envelope.server_response_motion.end = True
        final_payload = envelope.SerializeToString()
        await session._handle_binary_message(final_payload)

        self.assertEqual(got, [(first_payload, False), (final_payload, True)])
        decoded = message_pb2.Message.FromString(got[-1][0])
        self.assertEqual(decoded, envelope)
        self.assertEqual(
            list(decoded.server_response_motion.animation.keyframes[0].emotion_weights),
            [0.25, 0.75],
        )

    async def test_motion_type_without_motion_payload_is_protocol_error(self):
        errors = []
        frames = []
        session = new_avatar_session(
            region="us-west",
            on_error=errors.append,
            transport_frames=lambda data, end: frames.append((data, end)),
        )
        await session._handle_binary_message(
            message_pb2.Message(
                type=message_pb2.MESSAGE_SERVER_RESPONSE_MOTION,
                server_error=message_pb2.ServerError(message="wrong payload"),
            ).SerializeToString()
        )
        self.assertEqual(frames, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].code, AvatarSDKErrorCode.protocolError)

    async def test_emotion_is_stable_per_request_and_resets_after_end(self):
        session = new_avatar_session(region="us-west")
        connection = _FakeWebSocket()
        session._connection = connection
        try:
            req_id = await session.send_audio(b"first", emotion="happy")
            self.assertEqual(await session.send_audio(b"second"), req_id)
            self.assertEqual(
                await session.send_audio(b"", end=True, emotion="happy"), req_id
            )
            next_id = await session.send_audio(b"next", end=True)
            self.assertNotEqual(next_id, req_id)
            messages = [
                message_pb2.Message.FromString(data).client_audio_input
                for data in connection.sent
            ]
            self.assertEqual(
                [msg.emotion for msg in messages], ["happy", "happy", "happy", ""]
            )
            self.assertEqual([msg.end for msg in messages], [False, False, True, True])
            self.assertEqual(
                [msg.audio for msg in messages], [b"first", b"second", b"", b"next"]
            )
        finally:
            await session.close()

    async def test_changing_emotion_mid_request_rejects_without_sending(self):
        session = new_avatar_session(region="us-west")
        connection = _FakeWebSocket()
        session._connection = connection
        try:
            req_id = await session.send_audio(b"first", emotion="happy")
            with self.assertRaises(ValueError):
                await session.send_audio(b"discarded", emotion="sad")
            self.assertEqual(len(connection.sent), 1)
            self.assertEqual(await session.send_audio(b"final", end=True), req_id)
            self.assertEqual(
                message_pb2.Message.FromString(
                    connection.sent[-1]
                ).client_audio_input.emotion,
                "happy",
            )
        finally:
            await session.close()

    async def test_server_default_emotion_cannot_change_mid_request(self):
        session = new_avatar_session(region="us-west")
        connection = _FakeWebSocket()
        session._connection = connection
        try:
            req_id = await session.send_audio(b"first")
            with self.assertRaises(ValueError):
                await session.send_audio(b"discarded", emotion="happy")
            self.assertEqual(
                await session.send_audio(b"final", end=True, emotion=""), req_id
            )
            self.assertTrue(
                all(
                    not message_pb2.Message.FromString(data).client_audio_input.emotion
                    for data in connection.sent
                )
            )
        finally:
            await session.close()

    async def test_interrupt_uses_compact_v3_envelope_and_resets_emotion(self):
        session = new_avatar_session(region="us-west")
        connection = _FakeWebSocket()
        session._connection = connection
        try:
            req_id = await session.send_audio(b"first", emotion="happy")
            self.assertEqual(await session.interrupt(), req_id)
            interrupt = message_pb2.Message.FromString(connection.sent[1])
            self.assertEqual(interrupt.type, message_pb2.MESSAGE_CLIENT_INTERRUPT)
            self.assertEqual(interrupt.type, 6)
            self.assertEqual(interrupt.WhichOneof("data"), "client_interrupt")
            self.assertEqual(interrupt.client_interrupt.req_id, req_id)
            self.assertEqual(
                interrupt.DESCRIPTOR.fields_by_name["client_interrupt"].number, 7
            )
            next_id = await session.send_audio(b"next", end=True, emotion="sad")
            self.assertNotEqual(next_id, req_id)
            self.assertEqual(
                message_pb2.Message.FromString(
                    connection.sent[-1]
                ).client_audio_input.emotion,
                "sad",
            )
        finally:
            await session.close()

    async def test_start_raises_on_server_error_during_handshake(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(
                recv_messages=[_mk_server_error(code=400, message="bad params")]
            )

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.serverError)
        self.assertEqual(err.phase, "websocket_handshake")
        self.assertEqual(err.server_code, "400")
        self.assertEqual(err.connection_id, "cid")
        self.assertEqual(err.req_id, "rid")
        self.assertEqual(err.server_detail, "bad params")

    async def test_async_context_manager_runs_lifecycle(self):
        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        calls: list[str] = []

        async def fake_init():
            calls.append("init")

        async def fake_start():
            calls.append("start")
            return "conn-1"

        async def fake_close():
            calls.append("close")

        with (
            patch.object(session, "init", new=fake_init),
            patch.object(session, "start", new=fake_start),
            patch.object(session, "close", new=fake_close),
        ):
            async with session as entered:
                self.assertIs(entered, session)
                self.assertEqual(calls, ["init", "start"])

        self.assertEqual(calls, ["init", "start", "close"])

    async def test_start_times_out_when_server_never_responds(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            # No recv messages queued: recv() blocks forever.
            return _FakeWebSocket()

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
            patch("spatius.avatar_session.HANDSHAKE_TIMEOUT_S", new=0.05),
        ):
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.connectionFailed)
        self.assertEqual(err.phase, "websocket_handshake")
        self.assertIn("timed out", err.message)

    async def test_start_parses_websocket_http_rejection_body(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            raise InvalidStatus(
                Response(
                    status_code=400,
                    reason_phrase="Bad Request",
                    headers=Headers(),
                    body=b'{"message":"Bad Request"}\n',
                )
            )

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with patch("spatius.avatar_session.websockets.connect", new=fake_connect):
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.sessionTokenInvalid)
        self.assertEqual(err.phase, "websocket_connect")
        self.assertEqual(err.http_status, 400)
        self.assertEqual(err.server_detail, "Bad Request")
        self.assertEqual(err.raw_body, '{"message":"Bad Request"}\n')
        self.assertIn(
            "WebSocket connection rejected (HTTP 400): Bad Request", err.message
        )

    async def test_start_maps_avatar_not_found_http_rejection(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            raise InvalidStatus(
                Response(
                    status_code=404,
                    reason_phrase="Not Found",
                    headers=Headers(),
                    body=b'{"message":"Avatar not found: avatar-1"}\n',
                )
            )

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with patch("spatius.avatar_session.websockets.connect", new=fake_connect):
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.avatarNotFound)
        self.assertEqual(err.http_status, 404)
        self.assertEqual(err.server_detail, "Avatar not found: avatar-1")

    async def test_start_maps_billing_required_http_rejection(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            raise InvalidStatus(
                Response(
                    status_code=402,
                    reason_phrase="Payment Required",
                    headers=Headers(),
                    body=b'{"message":"session denied: credits exhausted"}\n',
                )
            )

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with patch("spatius.avatar_session.websockets.connect", new=fake_connect):
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.creditsExhausted)
        self.assertEqual(err.http_status, 402)
        self.assertIsNotNone(err.server_detail)
        self.assertIn("credits exhausted", cast(str, err.server_detail))

    def test_new_avatar_session_defaults_expire_at(self):
        before = datetime.now(timezone.utc)
        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        expire_at = session.config.expire_at
        self.assertIsNotNone(expire_at)
        self.assertIsNotNone(expire_at.tzinfo)
        self.assertGreaterEqual(expire_at, before + timedelta(minutes=59))
        self.assertLessEqual(expire_at, datetime.now(timezone.utc) + timedelta(hours=1))

    def test_new_avatar_session_treats_naive_expire_at_as_utc(self):
        naive = datetime(2030, 1, 1, 12, 0, 0)
        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            expire_at=naive,
        )
        expire_at = session.config.expire_at
        self.assertEqual(expire_at.tzinfo, timezone.utc)
        self.assertEqual(expire_at.replace(tzinfo=None), naive)

    async def test_runtime_server_error_callback_receives_avatar_sdk_error(self):
        got: list[Exception] = []
        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            on_error=got.append,
        )

        await session._handle_binary_message(
            _mk_server_error(code=321, message="server boom")
        )

        self.assertEqual(len(got), 1)
        err = cast(AvatarSDKError, got[0])
        self.assertIsInstance(err, AvatarSDKError)
        self.assertEqual(err.code, AvatarSDKErrorCode.serverError)
        self.assertEqual(err.phase, "websocket_runtime")
        self.assertEqual(err.server_code, "321")
        self.assertEqual(err.connection_id, "cid")
        self.assertEqual(err.req_id, "rid")
        self.assertEqual(err.server_detail, "server boom")

    async def test_runtime_billing_error_code_is_mapped(self):
        got: list[Exception] = []
        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            on_error=got.append,
        )

        await session._handle_binary_message(
            _mk_server_error(code=4001, message="Credits exhausted")
        )

        self.assertEqual(len(got), 1)
        err = cast(AvatarSDKError, got[0])
        self.assertIsInstance(err, AvatarSDKError)
        self.assertEqual(err.code, AvatarSDKErrorCode.creditsExhausted)
        self.assertEqual(err.server_code, "4001")

    async def test_runtime_grpc_unauthenticated_egress_error_is_mapped(self):
        got: list[Exception] = []
        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            on_error=got.append,
        )

        await session._handle_binary_message(
            _mk_server_error(
                code=16,
                message="failed to create connection: failed to connect to room: unauthorized: invalid token",
            )
        )

        self.assertEqual(len(got), 1)
        err = cast(AvatarSDKError, got[0])
        self.assertIsInstance(err, AvatarSDKError)
        self.assertEqual(err.code, AvatarSDKErrorCode.invalidEgressConfig)
        self.assertEqual(err.server_code, "16")

    async def test_handshake_server_error_maps_business_code_from_message(self):
        async def fake_connect(url, additional_headers=None, **_kwargs):
            return _FakeWebSocket(
                recv_messages=[
                    _mk_server_error(
                        code=0,
                        message="unsupported sample rate: 12345",
                    )
                ]
            )

        def fake_create_task(coro):
            coro.close()
            return _DummyTask()

        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
        )
        session._session_token = "tok-1"

        with (
            patch("spatius.avatar_session.websockets.connect", new=fake_connect),
            patch("spatius.avatar_session.asyncio.create_task", new=fake_create_task),
        ):
            with self.assertRaises(AvatarSDKError) as cm:
                await session.start()

        err = cm.exception
        self.assertEqual(err.code, AvatarSDKErrorCode.unsupportedSampleRate)
        self.assertEqual(err.phase, "websocket_handshake")

    async def test_read_loop_reports_unexpected_connection_close(self):
        got: list[Exception] = []
        session = new_avatar_session(
            ingress_endpoint_url="https://ingress.example.com",
            console_endpoint_url="https://console.example.com",
            api_key="api",
            avatar_id="avatar-1",
            app_id="app-1",
            on_error=got.append,
        )
        session._connection = _ClosingWebSocket(
            ConnectionClosedError(Close(1011, "server exploded"), None)
        )

        await session._read_loop()

        self.assertEqual(len(got), 1)
        err = cast(AvatarSDKError, got[0])
        self.assertIsInstance(err, AvatarSDKError)
        self.assertEqual(err.code, AvatarSDKErrorCode.connectionClosed)
        self.assertEqual(err.phase, "websocket_runtime")
        self.assertEqual(err.close_code, 1011)
        self.assertEqual(err.close_reason, "server exploded")
