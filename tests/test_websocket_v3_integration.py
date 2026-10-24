import asyncio
import unittest

from aiohttp import WSMsgType, web

from spatius import new_avatar_session
from spatius.proto.generated import message_pb2


class TestLocalV3WebSocket(unittest.IsolatedAsyncioTestCase):
    async def test_token_handshake_audio_motion_and_interrupt(self):
        received = []
        connection_headers = {}
        token_requests = []
        detail_requests = []
        frames = []
        errors = []
        completed = asyncio.Event()
        interrupted = asyncio.get_running_loop().create_future()

        async def create_token(request):
            token_requests.append(
                (request.headers.get("X-Api-Key"), await request.json())
            )
            return web.json_response({"sessionToken": "local-session-token"})

        async def avatar_detail(request):
            detail_requests.append(
                (
                    request.path,
                    request.headers.get("X-App-ID"),
                    request.headers.get("X-Session-Key"),
                )
            )
            return web.json_response(
                {
                    "availableEmotions": ["neutral", "happy", "sad"],
                    "defaultEmotion": "neutral",
                }
            )

        async def websocket(request):
            connection_headers.update(
                path=request.path,
                avatar=request.query.get("id"),
                app=request.headers.get("X-App-ID"),
                token=request.headers.get("X-Session-Key"),
            )
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for packet in ws:
                if packet.type != WSMsgType.BINARY:
                    continue
                envelope = message_pb2.Message.FromString(packet.data)
                received.append(envelope)
                if envelope.type == message_pb2.MESSAGE_CLIENT_CONFIGURE_SESSION:
                    await ws.send_bytes(
                        message_pb2.Message(
                            type=message_pb2.MESSAGE_SERVER_CONFIRM_SESSION,
                            server_confirm_session=message_pb2.ServerConfirmSession(
                                connection_id="local-connection",
                            ),
                        ).SerializeToString()
                    )
                elif (
                    envelope.type == message_pb2.MESSAGE_CLIENT_AUDIO_INPUT
                    and envelope.client_audio_input.end
                ):
                    audio = envelope.client_audio_input
                    for end in (False, True):
                        await ws.send_bytes(
                            message_pb2.Message(
                                type=message_pb2.MESSAGE_SERVER_RESPONSE_MOTION,
                                server_response_motion=message_pb2.ServerResponseMotion(
                                    connection_id="local-connection",
                                    req_id=audio.req_id,
                                    avatar_id="local-avatar",
                                    emotion=audio.emotion,
                                    end=end,
                                    animation=message_pb2.MotionAnimation(
                                        keyframes=[
                                            message_pb2.MotionFrame(
                                                expr=[0.25, 0.5],
                                                body_pose=[0.0, -0.25, 0.0],
                                                jaw_pose=[0.25, 0.0, 0.0],
                                                eyelid=[0.25, 0.5],
                                                leye_pose=[0.0, 0.25, 0.0],
                                                reye_pose=[0.0, -0.25, 0.0],
                                                emotion_weights=[0.25, 0.75],
                                            )
                                        ],
                                    ),
                                ),
                            ).SerializeToString()
                        )
                elif envelope.type == message_pb2.MESSAGE_CLIENT_INTERRUPT:
                    if not interrupted.done():
                        interrupted.set_result(envelope)
            return ws

        def on_frame(payload, end):
            frames.append((message_pb2.Message.FromString(payload), end))
            if end:
                completed.set()

        app = web.Application()
        app.router.add_post("/v1/console/session-tokens", create_token)
        app.router.add_get("/v3/driveningress/websocket", websocket)
        app.router.add_get("/v3/avatar/{avatar_id}", avatar_detail)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        session = new_avatar_session(
            api_key="local-api-key",
            app_id="local-app",
            avatar_id="local-avatar",
            console_endpoint_url=f"{origin}/v1/console",
            ingress_endpoint_url=f"{origin}/v3/driveningress",
            sample_rate=16000,
            transport_frames=on_frame,
            on_error=errors.append,
        )
        try:
            await session.init()
            self.assertEqual(detail_requests, [])
            detail = await session.get_avatar_detail()
            self.assertEqual(detail.available_emotions, ("neutral", "happy", "sad"))
            self.assertEqual(detail.default_emotion, "neutral")
            self.assertIs(await session.get_avatar_detail(), detail)
            self.assertEqual(await session.start(), "local-connection")
            req_id = await session.send_audio(
                b"\x01\x00" * 160, emotion="happy", strength=0.5
            )
            self.assertEqual(await session.send_audio(b"\x02\x00" * 160), req_id)
            self.assertEqual(await session.send_audio(b"", end=True), req_id)
            await asyncio.wait_for(completed.wait(), timeout=5)
            next_id = await session.send_audio(
                b"\x03\x00" * 160, emotion="sad", strength=0.0
            )
            self.assertNotEqual(next_id, req_id)
            self.assertEqual(await session.interrupt(), next_id)
            interrupt = await asyncio.wait_for(interrupted, timeout=5)
            completed.clear()
            default_id = await session.send_audio(b"\x04\x00" * 160, end=True)
            self.assertNotEqual(default_id, next_id)
            await asyncio.wait_for(completed.wait(), timeout=5)
        finally:
            await session.close()
            await runner.cleanup()

        self.assertEqual(
            connection_headers,
            {
                "path": "/v3/driveningress/websocket",
                "avatar": "local-avatar",
                "app": "local-app",
                "token": "local-session-token",
            },
        )
        self.assertEqual(
            detail_requests,
            [
                ("/v3/avatar/local-avatar", "local-app", "local-session-token"),
            ],
        )
        self.assertEqual(token_requests[0][0], "local-api-key")
        self.assertIsInstance(token_requests[0][1]["expireAt"], int)
        config = received[0].client_configure_session
        self.assertEqual(config.audio_sample_rate, 16000)
        self.assertEqual(config.audio_format, message_pb2.AUDIO_FORMAT_PCM_S16LE)
        self.assertIn(config.transport.WhichOneof("destination"), (None, "websocket"))
        audio = [
            packet.client_audio_input
            for packet in received
            if packet.type == message_pb2.MESSAGE_CLIENT_AUDIO_INPUT
        ]
        self.assertEqual([packet.req_id for packet in audio[:3]], [req_id] * 3)
        self.assertEqual(
            [packet.emotion for packet in audio], ["happy"] * 3 + ["sad", ""]
        )
        self.assertEqual(
            [packet.end for packet in audio], [False, False, True, False, True]
        )
        self.assertEqual(
            [packet.HasField("strength") for packet in audio],
            [True, True, True, True, False],
        )
        self.assertEqual(
            [packet.strength for packet in audio[:4]], [0.5, 0.5, 0.5, 0.0]
        )
        self.assertEqual(
            b"".join(packet.audio for packet in audio[:3]),
            b"\x01\x00" * 160 + b"\x02\x00" * 160,
        )
        self.assertEqual([end for _, end in frames], [False, True, False, True])
        for index, (envelope, end) in enumerate(frames):
            motion = envelope.server_response_motion
            self.assertEqual(envelope.type, message_pb2.MESSAGE_SERVER_RESPONSE_MOTION)
            self.assertEqual(motion.req_id, req_id if index < 2 else default_id)
            self.assertEqual(motion.connection_id, "local-connection")
            self.assertEqual(motion.avatar_id, "local-avatar")
            self.assertEqual(motion.emotion, "happy" if index < 2 else "")
            self.assertEqual(motion.end, end)
            self.assertEqual(
                list(motion.animation.keyframes[0].emotion_weights), [0.25, 0.75]
            )
            self.assertEqual(list(motion.animation.keyframes[0].eyelid), [0.25, 0.5])
        self.assertEqual(interrupt.type, 6)
        self.assertEqual(interrupt.client_interrupt.req_id, next_id)
        self.assertEqual(errors, [])
