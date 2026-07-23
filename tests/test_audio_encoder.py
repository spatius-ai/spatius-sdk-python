import gc
import importlib.util
import math
import struct
import sys
import tracemalloc
import types
import unittest
from unittest.mock import patch

from spatius.audio_encoder import OggOpusStreamEncoder


_HAS_OPUSLIB = importlib.util.find_spec("opuslib_next") is not None


def _generate_speech_like_pcm(sample_rate: int, duration_seconds: int) -> bytes:
    sample_count = sample_rate * duration_seconds
    pcm = bytearray(sample_count * 2)

    for index in range(sample_count):
        time_seconds = index / sample_rate
        fade = min(
            1.0,
            index / (sample_rate * 0.05),
            (sample_count - index) / (sample_rate * 0.05),
        )
        envelope = fade * (
            0.55
            + 0.45
            * (0.5 + 0.5 * math.sin(2.0 * math.pi * 3.0 * time_seconds))
        )
        value = envelope * (
            0.50 * math.sin(2.0 * math.pi * 180.0 * time_seconds)
            + 0.22 * math.sin(2.0 * math.pi * 360.0 * time_seconds + 0.3)
            + 0.08 * math.sin(2.0 * math.pi * 720.0 * time_seconds + 1.0)
        )
        struct.pack_into("<h", pcm, index * 2, int(value * 32767))

    return bytes(pcm)


def _parse_ogg_packets(ogg_stream: bytes) -> tuple[list[bytes], int]:
    packets: list[bytes] = []
    pending_packet = bytearray()
    offset = 0
    final_granule = 0

    while offset < len(ogg_stream):
        if ogg_stream[offset : offset + 4] != b"OggS":
            raise ValueError("invalid Ogg capture pattern")

        segment_count = ogg_stream[offset + 26]
        segment_table_start = offset + 27
        segment_table_end = segment_table_start + segment_count
        lacing_values = ogg_stream[segment_table_start:segment_table_end]
        payload_offset = segment_table_end
        final_granule = struct.unpack_from("<Q", ogg_stream, offset + 6)[0]

        for segment_size in lacing_values:
            segment_end = payload_offset + segment_size
            pending_packet.extend(ogg_stream[payload_offset:segment_end])
            payload_offset = segment_end
            if segment_size < 255:
                packets.append(bytes(pending_packet))
                pending_packet.clear()

        offset = payload_offset

    if pending_packet:
        raise ValueError("unterminated Ogg packet")

    return packets, final_granule


def _pcm_cosine(left: bytes, right: bytes) -> float:
    if len(left) != len(right) or len(left) % 2 != 0:
        raise ValueError("PCM inputs must have equal, 16-bit-aligned lengths")

    sample_count = len(left) // 2
    left_samples = struct.unpack(f"<{sample_count}h", left)
    right_samples = struct.unpack(f"<{sample_count}h", right)
    dot_product = sum(a * b for a, b in zip(left_samples, right_samples))
    left_energy = sum(sample * sample for sample in left_samples)
    right_energy = sum(sample * sample for sample in right_samples)
    return dot_product / math.sqrt(left_energy * right_energy)


class _FakeOpusError(Exception):
    pass


class _FakeEncoder:
    def __init__(self, sample_rate, channels, application):
        self.sample_rate = sample_rate
        self.channels = channels
        self.application = application

    @property
    def bitrate(self):
        return None

    @bitrate.setter
    def bitrate(self, value):
        raise _FakeOpusError(f"invalid bitrate: {value}")


class TestAudioEncoder(unittest.TestCase):
    @unittest.skipUnless(_HAS_OPUSLIB, "opuslib-next is required for audio quality tests")
    def test_audio_64k_quality_and_timing_meet_codec_gate(self):
        import opuslib_next as opuslib

        sample_rate = 24000
        frame_duration_ms = 20
        pcm = _generate_speech_like_pcm(sample_rate, duration_seconds=3)
        encoder = OggOpusStreamEncoder(
            sample_rate=sample_rate,
            bitrate=64000,
            frame_duration_ms=frame_duration_ms,
            application="audio",
            collect_encoded_output=True,
        )
        native_encoder = encoder._encoder
        self.assertEqual(native_encoder.bitrate, 64000)
        self.assertEqual(native_encoder.vbr, 1)
        self.assertEqual(native_encoder.complexity, 10)
        self.assertEqual(native_encoder.signal, opuslib.AUTO)
        self.assertEqual(native_encoder.lsb_depth, 16)
        self.assertEqual(native_encoder.inband_fec, 0)
        self.assertEqual(
            opuslib.api.encoder.encoder_ctl(
                native_encoder.encoder_state,
                opuslib.api.ctl.get_dtx,
            ),
            0,
        )

        completed_stream = None
        chunk_bytes = sample_rate * 2 // 10
        for offset in range(0, len(pcm), chunk_bytes):
            end = offset + chunk_bytes >= len(pcm)
            result = encoder.encode(pcm[offset : offset + chunk_bytes], end=end)
            if result.completed_stream is not None:
                completed_stream = result.completed_stream

        self.assertIsNotNone(completed_stream)
        packets, final_granule = _parse_ogg_packets(completed_stream or b"")
        self.assertEqual(packets[0][:8], b"OpusHead")
        self.assertEqual(packets[1][:8], b"OpusTags")
        self.assertEqual(
            sum(packet.startswith(b"OpusHead") for packet in packets),
            1,
        )

        pre_skip_48k = struct.unpack_from("<H", packets[0], 10)[0]
        sample_scale = 48000 // sample_rate
        expected_output_samples = len(pcm) // 2
        output_samples_from_granule = (
            final_granule - pre_skip_48k
        ) // sample_scale
        self.assertLessEqual(
            abs(output_samples_from_granule - expected_output_samples),
            1,
        )

        frame_size = sample_rate * frame_duration_ms // 1000
        decoder = opuslib.Decoder(sample_rate, 1)
        decoded_with_pre_skip = b"".join(
            decoder.decode(packet, frame_size, False) for packet in packets[2:]
        )
        pre_skip_samples = pre_skip_48k // sample_scale
        decoded = decoded_with_pre_skip[
            pre_skip_samples * 2 : pre_skip_samples * 2 + len(pcm)
        ]

        self.assertEqual(len(decoded), len(pcm))
        self.assertGreaterEqual(_pcm_cosine(pcm, decoded), 0.99)

    @unittest.skipUnless(_HAS_OPUSLIB, "opuslib-next is required for memory leak tests")
    def test_long_running_stream_does_not_accumulate_memory(self):
        frame_duration_ms = 20
        encoder = OggOpusStreamEncoder(
            sample_rate=24000,
            bitrate=32000,
            frame_duration_ms=frame_duration_ms,
            application="voip",
            collect_encoded_output=False,
        )
        pcm_frame = b"\x00\x00" * 480
        frames_per_minute = 60_000 // frame_duration_ms

        # Warm up Python and libopus so one-time allocations do not affect the
        # retained-memory comparison.
        for _ in range(100):
            encoder.encode(pcm_frame, end=False)

        gc.collect()
        tracemalloc.start()
        try:
            for _ in range(frames_per_minute):
                encoder.encode(pcm_frame, end=False)
            gc.collect()
            retained_after_one_minute = tracemalloc.get_traced_memory()[0]

            for _ in range(frames_per_minute):
                encoder.encode(pcm_frame, end=False)
            gc.collect()
            retained_after_two_minutes = tracemalloc.get_traced_memory()[0]
        finally:
            tracemalloc.stop()
            encoder.encode(b"", end=True)

        retained_growth = (
            retained_after_two_minutes - retained_after_one_minute
        )
        self.assertLess(
            retained_growth,
            64 * 1024,
            f"retained memory grew by {retained_growth} bytes during streaming",
        )

    def test_lacing_values_terminate_packets_divisible_by_255_once(self):
        self.assertEqual(
            OggOpusStreamEncoder._build_lacing_values(b"x" * 255),
            b"\xff\x00",
        )

    def test_create_encoder_logs_warning_when_bitrate_ctl_fails(self):
        fake_opuslib = types.ModuleType("opuslib_next")
        fake_opuslib.Encoder = _FakeEncoder
        fake_opuslib.exceptions = types.SimpleNamespace(OpusError=_FakeOpusError)

        with (
            patch.dict(sys.modules, {"opuslib_next": fake_opuslib}),
            self.assertLogs("spatius.audio_encoder", level="WARNING") as logs,
        ):
            encoder = OggOpusStreamEncoder._create_encoder(24000, 32000, "audio")

        self.assertIsInstance(encoder, _FakeEncoder)
        self.assertIn("Failed to set Opus encoder bitrate", logs.output[0])
