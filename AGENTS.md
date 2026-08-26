# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

## Build and Development Commands

```bash
# Install dependencies
uv sync

# Run tests
pytest

# Run a single test file
pytest tests/test_avatar_session_v2.py

# Run a specific test
pytest tests/test_avatar_session_v2.py::TestAvatarSessionV2::test_init_success

# Test across multiple Python versions locally
./test-local.sh all          # Test all Python versions (3.10-3.14) with all dependency combinations
./test-local.sh py310        # Test Python 3.10 only
./test-local.sh min          # Test minimum dependency versions on all Python versions
./test-local.sh latest       # Test latest dependency versions on all Python versions
./test-local.sh quick        # Quick test on current Python version

# Regenerate protobuf code (after modifying proto/message.proto)
cd proto && buf generate
```

## Architecture

This is a Python SDK for WebSocket-based avatar services with audio streaming and animation frame reception. Published as `spatius` on PyPI.

### Core Components

- **`avatar_session.py`** - Main `AvatarSession` class managing WebSocket connections, audio streaming, and frame reception. Uses v2 protocol with HTTP-based session token acquisition followed by WebSocket handshake. Exports `SessionTokenError` for token acquisition failures.

- **`session_config.py`** - `SessionConfig` dataclass, `LiveKitEgressConfig` dataclass, `AgoraEgressConfig` dataclass, and typed `new_avatar_session()` factory for session configuration.

- **`bootstrap.py`** - Global bootstrap API client. `resolve_region()` resolves `region="auto"` into a concrete ingress region via `POST https://global.spatialwalk.top/bootstrap` (request: app_id, sdk_version, region, platform; 5s timeout). Successful resolutions are reused process-wide for `REGION_CACHE_TTL_S` (5 minutes); on failure it falls back to the last cached region (even stale) or `DEFAULT_REGION` ("us-west") and never raises.

- **`net.py`** - Shared networking primitives: a process-wide TLS context (`get_ssl_context()`) used by both aiohttp and websockets so TLS session tickets are reused, connector factory with DNS caching, and `warm_tls_connection()` for best-effort warm-up connects.

- **`prewarm.py`** - Public `prewarm()` API: resolves and caches the `auto` region, warms TLS to the console/ingress hosts, and optionally prefetches a session token (opt-in via `prefetch_session_token=True`). Best-effort; never raises. Designed for worker prewarm hooks (e.g. LiveKit Agents `prewarm_fnc`).

- **`token_cache.py`** - Process-level cache for prefetched session tokens, consumed by `AvatarSession.init()` when credentials and endpoint match and the token is not near expiry.

### Telemetry instrumentation for warm-up

`AvatarSession.init()` records an `avatar.session.init.duration` histogram and finishes its `avatar.session.init` span with `region_cache_hit` / `token_cache_hit` attributes (omitted when no resolution/prefetch applied), so warm vs cold dispatches are distinguishable in metrics and traces. `prewarm()` emits a `spatius.prewarm` span and a `spatius.prewarm.duration` histogram with `success`, `region`, `tls_warmed`, and `session_token_prefetched` attributes.

- **`errors.py`** - `AvatarSDKError` exception with stable error codes (`AvatarSDKErrorCode` enum). Error codes: `sessionTokenExpired`, `sessionTokenInvalid`, `appIDUnrecognized`, `unknown`.

- **`logid.py`** - `generate_log_id()` utility for generating unique log IDs in format "YYYYMMDDHHMMSS_<nanoid>".

- **`telemetry.py`** - Process-wide unauthenticated OpenTelemetry metrics and traces. Uses `https://t.spatialwalk.top` by default, derives `/v1/metrics` and `/v1/traces`, and supports `configure_telemetry("")` to disable export. No OpenTelemetry logs are emitted.

- **`proto/generated/`** - Auto-generated protobuf code from `proto/message.proto`. Message types: ClientConfigureSession, ServerConfirmSession, TraceContext, ClientAudioInput, ServerError, ServerResponseAnimation, ClientInterrupt.

### Session Flow

1. `new_avatar_session()` creates configuration
2. `session.init()` - Resolves `region="auto"` (the default) via the bootstrap API and composes endpoint URLs (skipped when a concrete region or explicit endpoint URLs are configured), then HTTP POST to console API for session token
3. `session.start()` - WebSocket connection + v2 handshake, returns connection_id
4. `session.send_audio()` - Send PCM audio via protobuf
5. Background read loop delivers animation frames via `transport_frames` callback
6. `session.close()` - Cleanup. Call `shutdown_telemetry()` when a short-lived process exits immediately after a session so pending metrics/traces are flushed.

### Telemetry

Metrics and traces are enabled by default and sent without authentication to `https://t.spatialwalk.top/v1/metrics` and `https://t.spatialwalk.top/v1/traces`. Configure the process-wide OTLP base endpoint before using a session:

```python
from spatius import configure_telemetry, shutdown_telemetry

configure_telemetry("https://telemetry.example.com")
# configure_telemetry("")  # disable metrics and traces

# At process shutdown, if pending data must be flushed:
shutdown_telemetry()
```

The first audio message for each request carries W3C `traceparent` context through the shared protobuf `TraceContext` field. Later chunks omit it. The SDK exports metrics and traces only; it does not upload telemetry logs.

### Audio Format

Mono 16-bit PCM (s16le) only. Supported sample rates: 8000, 16000, 22050, 24000, 32000, 44100, 48000 Hz.

### Authentication

Two modes controlled by `use_query_auth`:
- `False` (default): Headers-based auth (mobile pattern)
- `True`: Query params-based auth (web pattern)

### LiveKit Egress Mode

When configured with `livekit_egress`, audio and animation data are streamed to a LiveKit room via the egress service instead of being returned through the WebSocket connection. The egress configuration is sent via the `ClientConfigureSession` proto message.

To use LiveKit egress mode:
1. Configure the session with `livekit_egress=LiveKitEgressConfig(...)`
2. Provide LiveKit connection details: url, api_key, api_secret, room_name, and publisher_id
3. The server will create an egress connection and stream output to the LiveKit room
4. The `transport_frames` callback will not be invoked since data goes to LiveKit

```python
from spatius import new_avatar_session, LiveKitEgressConfig

session = new_avatar_session(
    livekit_egress=LiveKitEgressConfig(
        url="wss://livekit.example.com",
        api_key="your-api-key",
        api_secret="your-api-secret",
        room_name="room-name",
        publisher_id="publisher-id",
    ),
    # ... other options
)
```

### Agora Egress Mode

When configured with `agora_egress`, audio and animation data are streamed to an Agora channel via the egress service instead of being returned through the WebSocket connection. The egress configuration is sent via the `ClientConfigureSession` proto message.

To use Agora egress mode:
1. Configure the session with `agora_egress=AgoraEgressConfig(...)`
2. Provide Agora connection details: channel_name, token (optional for testing), uid (0 for auto-assign), and publisher_id
3. The server will create an egress connection and stream output to the Agora channel
4. The `transport_frames` callback will not be invoked since data goes to Agora

```python
from spatius import new_avatar_session, AgoraEgressConfig

session = new_avatar_session(
    agora_egress=AgoraEgressConfig(
        channel_name="channel-name",
        token="your-agora-token",  # optional for testing
        uid=0,  # 0 for auto-assign
        publisher_id="publisher-id",
    ),
    # ... other options
)
```

### Interrupt Functionality (Egress Mode Only)

The `interrupt()` method sends an interrupt signal to stop current audio processing. This is available when using egress mode (LiveKit or Agora).

```python
# Send some audio
req_id = await session.send_audio(audio_data, end=True)

# Interrupt if needed (e.g., user wants to stop)
interrupted_id = await session.interrupt()
```

The interrupt uses `last_req_id` which tracks the most recent request, even after `end=True` was sent. This allows interrupting requests that have finished sending audio but are still being processed.
