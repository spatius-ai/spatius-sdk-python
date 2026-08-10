# Spatius Python SDK

Python server SDK for creating Spatius avatar sessions.

## Installation

```bash
pip install spatius
```

Install the optional Ogg Opus encoder support when you want the SDK to encode raw PCM before sending:

```bash
pip install "spatius[opus]"
```

## Quick Start

```python
import asyncio
from datetime import datetime, timedelta, timezone

from spatius import new_avatar_session


async def main():
    session = new_avatar_session(
        api_key="your-api-key",
        app_id="your-app-id",
        avatar_id="your-avatar-id",
        expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        transport_frames=lambda frame, last: print(
            f"Received frame: {len(frame)} bytes, last={last}"
        ),
        on_error=lambda err: print(f"Session error: {err}"),
        on_close=lambda: print("Session closed"),
    )

    await session.init()
    connection_id = await session.start()
    print(f"Connected: {connection_id}")

    audio_data = b"..."  # mono PCM s16le audio bytes
    request_id = await session.send_audio(audio_data, end=True)
    print(f"Sent audio request: {request_id}")

    await asyncio.sleep(10)
    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
```

## Telemetry

The SDK exports OpenTelemetry metrics and traces without authentication by default:

- Metrics: `https://t.spatialwalk.top/v1/metrics`
- Traces: `https://t.spatialwalk.top/v1/traces`

Configure the process-wide OTLP base endpoint before using a session, or disable export with an empty string:

```python
from spatius import configure_telemetry, shutdown_telemetry

configure_telemetry("https://telemetry.example.com")
# configure_telemetry("")  # disable metrics and traces

# Useful for short-lived processes that exit immediately after a session.
shutdown_telemetry()
```

The first audio message for each request carries W3C trace context to the backend. Later chunks omit it. OTel resources include `service.name=spatius-python`, `sdk.platform=python`, `app_id`, and the resolved `region`. The server SDK exports metrics and traces only; it does not upload telemetry logs.

## Benchmarks

Benchmark the built-in PCM to Ogg Opus encoder from a source checkout:

```bash
uv run --extra opus python benchmarks/bench_ogg_opus_encoder.py
```

Use `--help` to see options for sample rate, bitrate, frame duration, input chunk size, and run count.

## Documentation

See the full Python SDK guide at [docs.spatius.ai/sdk-reference/python-sdk/python-sdk](https://docs.spatius.ai/sdk-reference/python-sdk/python-sdk).

## License

MIT
