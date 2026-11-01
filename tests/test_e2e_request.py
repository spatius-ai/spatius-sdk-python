import asyncio
import math
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from spatius import (
    AudioFormat,
    Expression,
    OggOpusEncoderConfig,
    SessionTokenError,
    new_avatar_session,
)
from spatius.proto.generated import message_pb2


def _require_env(*names: str) -> dict[str, str]:
    values: dict[str, str] = {}
    missing: list[str] = []
    for name in names:
        value = os.getenv(name, "").strip()
        if not value:
            missing.append(name)
        else:
            values[name] = value
    if missing:
        raise unittest.SkipTest("Missing required e2e env vars: " + ", ".join(missing))
    return values


def _endpoint_kwargs() -> dict[str, str]:
    return {
        "region": os.getenv("SPATIUS_E2E_REGION", "us-west").strip() or "us-west",
        "console_endpoint_url": os.getenv("SPATIUS_E2E_CONSOLE_ENDPOINT", "").strip(),
        "ingress_endpoint_url": os.getenv("SPATIUS_E2E_INGRESS_ENDPOINT", "").strip(),
    }


class _MotionCollector:
    def __init__(self):
        self.frames: list[tuple[message_pb2.Message, bool]] = []
        self.last = False
        self.error: Exception | None = None
        self._done = asyncio.Event()

    def on_frame(self, payload: bytes, is_last: bool) -> None:
        envelope = message_pb2.Message()
        envelope.ParseFromString(payload)
        print(f"motion frame payload length: {len(payload)} bytes", flush=True)
        self.frames.append((envelope, is_last))
        if is_last:
            self.last = True
            self.finish(None)

    def on_error(self, error: Exception) -> None:
        self.finish(error)

    def on_close(self) -> None:
        if not self.last and not self.error:
            self.finish(Exception("Session closed before final motion frame"))
        else:
            self.finish(None)

    def finish(self, error: Exception | None) -> None:
        if error is not None and self.error is None:
            self.error = error
        self._done.set()

    async def wait(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._done.wait(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise TimeoutError("Timed out waiting for motion frames") from exc

        if self.error is not None:
            raise self.error


@unittest.skipUnless(
    os.getenv("SPATIUS_RUN_E2E") == "1",
    "Set SPATIUS_RUN_E2E=1 to run end-to-end network tests",
)
class TestE2ERequest(unittest.IsolatedAsyncioTestCase):
    async def test_send_audio_receives_motion_frames(self):
        env = _require_env(
            "SPATIUS_E2E_API_KEY",
            "SPATIUS_E2E_APP_ID",
            "SPATIUS_E2E_AVATAR_ID",
        )

        audio_format = AudioFormat(
            os.getenv("SPATIUS_E2E_AUDIO_FORMAT", AudioFormat.PCM_S16LE.value)
            .strip()
            .lower()
        )
        use_internal_ogg_opus_encoder = _env_flag(
            "SPATIUS_E2E_USE_INTERNAL_OGG_OPUS_ENCODER"
        )
        sample_rate = int(
            os.getenv(
                "SPATIUS_E2E_SAMPLE_RATE",
                (
                    "16000"
                    if audio_format == AudioFormat.OGG_OPUS
                    and use_internal_ogg_opus_encoder
                    else "24000"
                    if audio_format == AudioFormat.OGG_OPUS
                    else "16000"
                ),
            )
        )
        bitrate = int(os.getenv("SPATIUS_E2E_BITRATE", "32000"))
        timeout_seconds = float(os.getenv("SPATIUS_E2E_TIMEOUT_SECONDS", "45"))
        chunk_size = int(os.getenv("SPATIUS_E2E_CHUNK_SIZE", "4096"))
        self.assertGreater(chunk_size, 0)
        emotion = os.getenv("SPATIUS_E2E_EMOTION")
        strength = float(os.getenv("SPATIUS_E2E_STRENGTH", "1"))
        weights_value = os.getenv("SPATIUS_E2E_EXPECTED_EMOTION_WEIGHTS", "")
        expected_weights = (
            [float(value) for value in weights_value.split(",")]
            if weights_value
            else None
        )
        audio_path = Path(
            os.getenv(
                "SPATIUS_E2E_AUDIO_PATH",
                "tests/fixtures/audio/audio_16000.pcm"
                if audio_format == AudioFormat.PCM_S16LE
                or use_internal_ogg_opus_encoder
                else "audio.ogg",
            )
        )
        if not audio_path.is_absolute():
            audio_path = Path(__file__).resolve().parents[1] / audio_path

        if not audio_path.exists():
            raise unittest.SkipTest(f"E2E audio file not found: {audio_path}")

        audio_bytes = audio_path.read_bytes()
        collector = _MotionCollector()
        session = new_avatar_session(
            api_key=env["SPATIUS_E2E_API_KEY"],
            app_id=env["SPATIUS_E2E_APP_ID"],
            **_endpoint_kwargs(),
            avatar_id=env["SPATIUS_E2E_AVATAR_ID"],
            expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            sample_rate=sample_rate,
            bitrate=bitrate,
            audio_format=audio_format,
            ogg_opus_encoder=(
                OggOpusEncoderConfig()
                if audio_format == AudioFormat.OGG_OPUS
                and use_internal_ogg_opus_encoder
                else None
            ),
            transport_frames=collector.on_frame,
            on_error=collector.on_error,
            on_close=collector.on_close,
        )

        try:
            await session.init()
            detail = await session.get_avatar_detail()
            await session.start()
        except SessionTokenError as exc:
            raise AssertionError(
                "Expected valid e2e credentials, but session token creation failed"
            ) from exc

        label = emotion or detail.default_emotion
        try:
            # a full user turn: speech boundaries, then the reply's expression and audio
            req_id = await session.user_speak_start()
            await session.user_speak_end()
            await session.set_expression([Expression(label, strength)])
            sent_id = await self._send_audio_for_format(
                session, audio_bytes, audio_format, chunk_size
            )
            self.assertEqual(sent_id, req_id)
            await collector.wait(timeout=timeout_seconds)
        finally:
            await session.close()

        self.assertGreater(len(collector.frames), 0)
        self.assertTrue(collector.last)
        keyframes = [
            frame
            for envelope, _ in collector.frames
            for frame in envelope.server_response_motion.animation.keyframes
        ]
        self.assertGreater(len(keyframes), 0)
        # private avatars carry the 712-byte field 8 and leave fields 1-6 empty
        self.assertTrue(all(len(frame.pbx) == 712 or frame.expr for frame in keyframes))
        self.assertTrue(
            all(
                len(frame.emotion_weights) == len(detail.available_emotions)
                for frame in keyframes
            )
        )
        for envelope, is_last in collector.frames:
            self.assertEqual(envelope.type, message_pb2.MESSAGE_SERVER_RESPONSE_MOTION)
            motion = envelope.server_response_motion
            self.assertEqual(motion.req_id, req_id)
            self.assertEqual(motion.avatar_id, env["SPATIUS_E2E_AVATAR_ID"])
            self.assertTrue(motion.connection_id)
            self.assertEqual(motion.end, is_last)
            self.assertEqual(motion.emotion, label)
        for frame in keyframes:
            for channel in (
                frame.expr,
                frame.body_pose,
                frame.jaw_pose,
                frame.eyelid,
                frame.leye_pose,
                frame.reye_pose,
                frame.emotion_weights,
            ):
                self.assertTrue(all(math.isfinite(value) for value in channel))
            if expected_weights is not None:
                self.assertEqual(len(frame.emotion_weights), len(expected_weights))
                for actual, expected in zip(frame.emotion_weights, expected_weights):
                    self.assertAlmostEqual(actual, expected, places=6)
        print(f"received {len(keyframes)} motion frames", flush=True)

        last_message, last_flag = collector.frames[-1]
        self.assertTrue(last_flag)
        self.assertEqual(last_message.type, message_pb2.MESSAGE_SERVER_RESPONSE_MOTION)
        self.assertEqual(last_message.server_response_motion.req_id, req_id)
        self.assertTrue(last_message.server_response_motion.end)

    async def _send_audio_for_format(
        self,
        session,
        audio_bytes: bytes,
        audio_format: AudioFormat,
        chunk_size: int,
    ) -> str:
        req_id = ""
        for offset in range(0, len(audio_bytes), chunk_size):
            req_id = await session.send_audio(
                audio_bytes[offset : offset + chunk_size],
                end=(
                    audio_format == AudioFormat.PCM_S16LE
                    and offset + chunk_size >= len(audio_bytes)
                ),
            )
        if audio_format == AudioFormat.OGG_OPUS or not audio_bytes:
            return await session.send_audio(b"", end=True)
        return req_id


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}
