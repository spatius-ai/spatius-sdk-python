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
