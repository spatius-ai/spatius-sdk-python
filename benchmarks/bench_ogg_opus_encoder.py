#!/usr/bin/env python3
"""Benchmark the SDK's built-in Ogg Opus encoder.

Run from the repository root with:

    uv run --extra opus python benchmarks/bench_ogg_opus_encoder.py
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import gc
import math
from pathlib import Path
import random
import statistics
import struct
import sys
import time

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if SRC_DIR.exists():
    sys.path.insert(0, str(SRC_DIR))

from spatius.audio_encoder import OggOpusStreamEncoder  # noqa: E402


@dataclass(frozen=True)
class RunResult:
    wall_seconds: float
    cpu_seconds: float
    encoded_bytes: int


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return parsed


def _generate_speech_like_pcm(sample_rate: int, duration_seconds: float) -> bytes:
    """Generate deterministic mono PCM s16le so benchmark input is reproducible."""
    sample_count = int(sample_rate * duration_seconds)
    rng = random.Random(0x5A17_105)
    pcm = bytearray(sample_count * 2)

    for index in range(sample_count):
        t = index / sample_rate

        # A cheap deterministic source that is less trivial than a single sine wave.
        envelope = 0.35 + 0.65 * (0.5 + 0.5 * math.sin(2.0 * math.pi * 2.7 * t))
        value = (
            0.38 * math.sin(2.0 * math.pi * 185.0 * t)
            + 0.21 * math.sin(2.0 * math.pi * 371.0 * t + 0.4)
            + 0.09 * math.sin(2.0 * math.pi * 743.0 * t + 1.1)
            + 0.025 * (rng.random() * 2.0 - 1.0)
        )
        value *= envelope
        value = max(-1.0, min(1.0, value))
        struct.pack_into("<h", pcm, index * 2, int(value * 32767))

    return bytes(pcm)


def _encode_once(args: argparse.Namespace, pcm: bytes) -> int:
    encoder = OggOpusStreamEncoder(
        sample_rate=args.sample_rate,
        bitrate=args.bitrate,
        frame_duration_ms=args.frame_duration_ms,
        application=args.application,
        collect_encoded_output=args.collect_encoded_output,
    )

    chunk_samples = max(1, args.sample_rate * args.chunk_ms // 1000)
    chunk_bytes = chunk_samples * 2
    encoded_bytes = 0

    for offset in range(0, len(pcm), chunk_bytes):
        end = offset + chunk_bytes >= len(pcm)
        result = encoder.encode(pcm[offset : offset + chunk_bytes], end=end)
        encoded_bytes += len(result.payload)

        if result.completed_stream is not None:
            completed_stream_size = len(result.completed_stream)
            if completed_stream_size != encoded_bytes:
                raise RuntimeError(
                    "collected encoded stream size does not match streamed payload size"
                )

    return encoded_bytes


def _run_benchmark(args: argparse.Namespace, pcm: bytes) -> list[RunResult]:
    results: list[RunResult] = []
    gc_was_enabled = gc.isenabled()

    try:
        gc.disable()
        total_runs = args.warmup_runs + args.runs
        for run_index in range(total_runs):
            wall_start = time.perf_counter()
            cpu_start = time.process_time()
            encoded_bytes = _encode_once(args, pcm)
            cpu_seconds = time.process_time() - cpu_start
            wall_seconds = time.perf_counter() - wall_start

            if run_index >= args.warmup_runs:
                results.append(
                    RunResult(
                        wall_seconds=wall_seconds,
                        cpu_seconds=cpu_seconds,
                        encoded_bytes=encoded_bytes,
                    )
                )
    finally:
        if gc_was_enabled:
            gc.enable()

    return results


def _format_bytes_per_second(byte_count: int, seconds: float) -> str:
    mib_per_second = byte_count / seconds / (1024 * 1024)
    return f"{mib_per_second:.2f} MiB/s"


def _print_results(
    args: argparse.Namespace, pcm: bytes, duration_seconds: float, results: list[RunResult]
) -> None:
    wall_times = [result.wall_seconds for result in results]
    cpu_times = [result.cpu_seconds for result in results]
    encoded_sizes = [result.encoded_bytes for result in results]

    print("Ogg Opus encoder benchmark")
    print("--------------------------")
    print(f"sample_rate:             {args.sample_rate} Hz")
    print(f"duration:                {duration_seconds:.3f} s")
    print(f"input_size:              {len(pcm)} bytes")
    print(f"bitrate:                 {args.bitrate or 'encoder default'}")
    print(f"frame_duration_ms:       {args.frame_duration_ms}")
    print(f"chunk_ms:                {args.chunk_ms}")
    print(f"application:             {args.application}")
    print(f"collect_encoded_output:  {args.collect_encoded_output}")
    print(f"warmup_runs:             {args.warmup_runs}")
    print(f"measured_runs:           {args.runs}")
    print()

    print(
        "run  wall_s    cpu_s     realtime_x  input_rate    encoded_bytes  "
        "encoded_ratio"
    )
    for index, result in enumerate(results, start=1):
        realtime_factor = duration_seconds / result.wall_seconds
        encoded_ratio = result.encoded_bytes / len(pcm)
        print(
            f"{index:>3}  "
            f"{result.wall_seconds:>7.4f}  "
            f"{result.cpu_seconds:>7.4f}  "
            f"{realtime_factor:>10.2f}  "
            f"{_format_bytes_per_second(len(pcm), result.wall_seconds):>10}  "
            f"{result.encoded_bytes:>13}  "
            f"{encoded_ratio:>13.4f}"
        )

    print()
    print("Summary")
    print(f"wall_mean_s:             {statistics.fmean(wall_times):.4f}")
    print(f"wall_median_s:           {statistics.median(wall_times):.4f}")
    print(f"wall_best_s:             {min(wall_times):.4f}")
    print(f"cpu_mean_s:              {statistics.fmean(cpu_times):.4f}")
    print(
        f"realtime_mean_x:         "
        f"{duration_seconds / statistics.fmean(wall_times):.2f}"
    )
    print(
        f"input_rate_mean:         "
        f"{_format_bytes_per_second(len(pcm), statistics.fmean(wall_times))}"
    )
    print(f"encoded_bytes_mean:      {statistics.fmean(encoded_sizes):.0f}")
    print(f"encoded_ratio_mean:      {statistics.fmean(encoded_sizes) / len(pcm):.4f}")

    if len(wall_times) > 1:
        print(f"wall_stdev_s:            {statistics.stdev(wall_times):.4f}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark spatius.audio_encoder.OggOpusStreamEncoder."
    )
    parser.add_argument(
        "--sample-rate",
        type=int,
        choices=(8000, 12000, 16000, 24000, 48000),
        default=24000,
        help="input PCM sample rate in Hz",
    )
    parser.add_argument(
        "--duration-sec",
        type=_positive_float,
        default=30.0,
        help="synthetic input duration to encode",
    )
    parser.add_argument(
        "--bitrate",
        type=int,
        default=32000,
        help="target Opus bitrate; use 0 for encoder default",
    )
    parser.add_argument(
        "--frame-duration-ms",
        type=int,
        choices=(10, 20, 40, 60),
        default=20,
        help="Opus frame duration",
    )
    parser.add_argument(
        "--chunk-ms",
        type=_positive_int,
        default=100,
        help="streaming input chunk size passed to encode()",
    )
    parser.add_argument(
        "--application",
        choices=("audio", "voip", "restricted_lowdelay"),
        default="restricted_lowdelay",
        help="Opus encoder application mode",
    )
    parser.add_argument(
        "--runs",
        type=_positive_int,
        default=5,
        help="number of measured runs",
    )
    parser.add_argument(
        "--warmup-runs",
        type=int,
        default=1,
        help="number of warmup runs before measurement",
    )
    parser.add_argument(
        "--collect-encoded-output",
        action="store_true",
        help="also collect the completed encoded stream, matching on_encoded_audio use",
    )

    args = parser.parse_args()
    if args.warmup_runs < 0:
        parser.error("--warmup-runs must be greater than or equal to 0")
    return args


def main() -> int:
    args = _parse_args()
    print("Generating benchmark PCM input...", file=sys.stderr)
    pcm = _generate_speech_like_pcm(args.sample_rate, args.duration_sec)

    try:
        results = _run_benchmark(args, pcm)
    except RuntimeError as exc:
        if "Install spatius[opus]" in str(exc):
            print(f"error: {exc}", file=sys.stderr)
            print(
                "hint: run `uv run --extra opus python "
                "benchmarks/bench_ogg_opus_encoder.py` from the repository root",
                file=sys.stderr,
            )
            return 2
        raise

    _print_results(args, pcm, args.duration_sec, results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
