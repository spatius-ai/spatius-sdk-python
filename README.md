# Spatius Python SDK

A Python SDK for connecting to avatar services via WebSocket, supporting audio streaming and receiving animation frames.

## Installation

```bash
pip install spatius
```

To enable the built-in PCM-to-Ogg-Opus encoder, install the optional `opus` extra:

```bash
pip install "spatius[opus]"
```

The optional encoder uses `opuslib`, which requires a working `libopus` runtime on the
host system.

## Quick Start

```python
import asyncio
from datetime import datetime, timedelta, timezone

from spatius import AudioFormat, new_avatar_session

async def main():
    # Create session
    session = new_avatar_session(
        api_key="your-api-key",
        app_id="your-app-id",
        region="us-west",
        avatar_id="your-avatar-id",
        expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        transport_frames=lambda frame, last: print(f"Received frame: {len(frame)} bytes"),
        on_error=lambda err: print(f"Error: {err}"),
        on_close=lambda: print("Session closed")
    )

    # Initialize and connect
    await session.init()
    connection_id = await session.start()
    print(f"Connected: {connection_id}")

    # Send audio
    audio_data = b"..."  # Your PCM or Ogg Opus audio data
    request_id = await session.send_audio(audio_data, end=True)
    print(f"Sent audio: {request_id}")

    # Wait for frames...
    await asyncio.sleep(10)

    # Close
    await session.close()

if __name__ == "__main__":
    asyncio.run(main())
```

## Detailed Usage

### Session Configuration

Create sessions with `new_avatar_session()`. By default, the SDK targets the `us-west`
region and composes endpoints automatically:

```python
from spatius import AudioFormat, new_avatar_session

session = new_avatar_session(
    avatar_id="avatar-123",
    api_key="your-api-key",
    app_id="your-app-id",
    # For web-style auth, set use_query_auth=True to put (appId, sessionKey)
    # in the websocket URL query params instead of headers.
    use_query_auth=False,
    expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    region="us-west",
    sample_rate=16000,  # Default: 16000 Hz
    audio_format=AudioFormat.PCM_S16LE,
    transport_frames=on_frame_received,
    on_error=on_error,
    on_close=on_close
)
```

If you need custom endpoints, pass them explicitly. Explicit URLs override `region`.

```python
session = new_avatar_session(
    avatar_id="avatar-123",
    api_key="your-api-key",
    app_id="your-app-id",
    expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    console_endpoint_url="https://console.example.com/v1/console",
    ingress_endpoint_url="wss://api.example.com/v2/driveningress",
    transport_frames=on_frame_received,
)
```

Region endpoints use this pattern:

```text
https://console.<region>.spatius.ai/v1/console
wss://api.<region>.spatius.ai/v2/driveningress
```

### Session Lifecycle

```python
# 1. Initialize (get session token)
await session.init()

# 2. Start WebSocket connection
connection_id = await session.start()

# 3. Send audio data
request_id = await session.send_audio(audio_bytes, end=True)

# 4. Receive frames via callback
# (automatically handled in background)

# 5. Close session
await session.close()
```

### Audio Format

The SDK supports two session-level input formats:

- `AudioFormat.PCM_S16LE` - mono 16-bit PCM bytes
- `AudioFormat.OGG_OPUS` - one continuous Ogg Opus stream per request ID

#### PCM input

- Sample Rate: one of `[8000, 16000, 22050, 24000, 32000, 44100, 48000]`
- Channels: 1 (mono)
- Bit Depth: 16-bit
- Format: Raw PCM bytes

```python
from spatius import AudioFormat

session = new_avatar_session(
    ...,
    sample_rate=16000,
    audio_format=AudioFormat.PCM_S16LE,
)

with open("tests/fixtures/audio/audio.pcm", "rb") as f:
    audio_data = f.read()

await session.send_audio(audio_data, end=True)
```

#### Ogg Opus input

- Sample Rate: one of `[8000, 12000, 16000, 24000, 48000]`
- Channels: 1 (mono)
- Format: Ogg Opus pages/chunks
- Request contract: each request ID must carry one continuous Ogg Opus stream across one or more `send_audio()` calls, and the final chunk must use `end=True`

```python
from spatius import AudioFormat

session = new_avatar_session(
    ...,
    sample_rate=24000,
    bitrate=32000,
    audio_format=AudioFormat.OGG_OPUS,
)

with open("audio.ogg", "rb") as f:
    while chunk := f.read(4096):
        await session.send_audio(chunk, end=False)

await session.send_audio(b"", end=True)
```

#### Built-in PCM to Ogg Opus encoder

If you want the session to negotiate `AudioFormat.OGG_OPUS` but still provide raw PCM
bytes to `send_audio()`, enable the optional internal encoder.

```python
from spatius import AudioFormat, OggOpusEncoderConfig

encoded_outputs = []

session = new_avatar_session(
    ...,
    sample_rate=24000,
    bitrate=32000,
    audio_format=AudioFormat.OGG_OPUS,
    ogg_opus_encoder=OggOpusEncoderConfig(frame_duration_ms=20),
    on_encoded_audio=lambda req_id, payload: encoded_outputs.append((req_id, payload)),
)

with open("tests/fixtures/audio/audio_24000.pcm", "rb") as f:
    pcm_audio = f.read()

await session.send_audio(pcm_audio, end=True)
```

Notes:

- The internal encoder is optional; if you do not install `spatius[opus]`, keep using PCM or provide pre-encoded Ogg Opus bytes yourself.
- `on_encoded_audio` fires when internal encoding completes for a request and receives `(req_id, encoded_audio_bytes)`.
- Advanced usage still works: if `audio_format=AudioFormat.OGG_OPUS` and `ogg_opus_encoder` is unset, `send_audio()` forwards your pre-encoded Ogg Opus bytes unchanged.

### LiveKit Egress Mode

When configured with `livekit_egress`, audio and animation data are streamed to a LiveKit room via the egress service instead of being returned through the WebSocket connection.

```python
from spatius import new_avatar_session, LiveKitEgressConfig

session = new_avatar_session(
    avatar_id="avatar-123",
    api_key="your-api-key",
    app_id="your-app-id",
    region="us-west",
    expire_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    livekit_egress=LiveKitEgressConfig(
        url="wss://livekit.example.com",
        api_token="livekit-token",
        room_name="my-room",
        publisher_id="avatar-publisher",
    ),
)
```

`api_key` and `api_secret` remain supported for backward compatibility, but they are
deprecated in the Python SDK. Prefer `api_token` for new integrations.

When LiveKit egress is enabled:
- The server streams output to the specified LiveKit room
- The `transport_frames` callback will not be invoked
- Audio and animation data are published to the room under the specified publisher ID

#### Interrupt (LiveKit Egress Only)

The `interrupt()` method sends an interrupt signal to stop current audio processing. This is only available when using LiveKit egress mode.

```python
# Send audio
request_id = await session.send_audio(audio_data, end=True)

# Later, if you need to interrupt (e.g., user wants to stop playback)
interrupted_id = await session.interrupt()
print(f"Interrupted request: {interrupted_id}")
```

The interrupt uses the most recent request ID, even after `end=True` was sent. This allows interrupting requests that have finished sending audio but are still being processed by the server.

### Callbacks

#### Transport Frames Callback

Receives animation frames from the server:

```python
def on_frame_received(frame_data: bytes, is_last: bool):
    print(f"Received frame: {len(frame_data)} bytes")
    if is_last:
        print("This is the last frame")
    # Process frame_data (contains serialized Message protobuf)
```

#### Error Callback

Handles errors from the session:

```python
from spatius import AvatarSDKError


def on_error(error: Exception):
    print(f"Session error: {error}")

    if isinstance(error, AvatarSDKError):
        print("  code:", error.code.value)
        print("  phase:", error.phase)
        print("  http_status:", error.http_status)
        print("  server_code:", error.server_code)
        print("  server_detail:", error.server_detail)
```

The SDK reports structured `AvatarSDKError` instances for token creation failures,
WebSocket upgrade rejections, handshake failures, runtime `ServerError` messages,
and unexpected connection drops.

### Error Handling

Use `SessionTokenError` for token creation failures and `AvatarSDKError` for all
other structured SDK errors:

```python
from spatius import AvatarSDKError, SessionTokenError

try:
    await session.init()
    await session.start()
except SessionTokenError as error:
    print("token failed", error.code.value, error.server_detail)
except AvatarSDKError as error:
    print("sdk error", error.code.value, error.phase, error.server_detail)
```

`AvatarSDKError` and `SessionTokenError` expose these fields:

- `code` - Stable SDK error code
- `message` - Human-readable message
- `phase` - Failure phase such as `session_token`, `websocket_connect`, `websocket_handshake`, `websocket_runtime`, or `websocket_send`
- `http_status` - HTTP status for token or WebSocket upgrade rejections
- `server_code` - Server-provided error code, including runtime protobuf `ServerError.code`
- `server_title` / `server_detail` - Parsed server error details when available
- `connection_id` / `req_id` - Server correlation identifiers when available
- `raw_body` - Raw HTTP rejection body for token or WebSocket upgrade failures
- `close_code` / `close_reason` - WebSocket close details for unexpected disconnects

Common `AvatarSDKErrorCode` values:

- `sessionTokenExpired` - Session token expired or unauthorized
- `sessionTokenInvalid` - Invalid or empty session token
- `appIDUnrecognized` - App ID is not recognized by the server
- `appIDMismatch` - Session token belongs to a different app
- `avatarNotFound` - Avatar does not exist
- `billingRequired` - Session denied by billing checks
- `creditsExhausted` - Runtime or connect-time credits exhausted
- `sessionDurationExceeded` - Billing-enforced session timeout reached
- `unsupportedSampleRate` - Handshake rejected unsupported audio sample rate
- `invalidEgressConfig` - LiveKit or Agora egress config is invalid
- `egressUnavailable` - Egress service is unavailable or not configured
- `idleTimeout` - Server closed the session after input inactivity
- `upstreamError` - Internal upstream service failed
- `protocolError` - Invalid protobuf or unexpected message sequence
- `connectionFailed` - Transport-level connection failure
- `connectionClosed` - Unexpected WebSocket close
- `serverError` - Server-side failure that did not match a more specific mapping
- `invalidRequest` - Other client-side request validation errors
- `unknown` - Fallback when the SDK cannot classify the failure

#### Close Callback

Called when the session closes:

```python
def on_close():
    print("Session has been closed")
```

## API Reference

### AvatarSession

Main class for managing avatar sessions.

#### Methods

- `async init()` - Initialize session and obtain token
- `async start() -> str` - Start WebSocket connection, returns connection ID
- `async send_audio(audio: bytes, end: bool = False) -> str` - Send audio data, returns request ID
- `async interrupt() -> str` - Interrupt current audio processing (LiveKit egress mode only), returns interrupted request ID
- `async close()` - Close the session and clean up resources
- `config -> SessionConfig` - Get session configuration (property)

### SessionConfig

Configuration dataclass for avatar sessions.

#### Fields

- `avatar_id: str` - Avatar identifier
- `api_key: str` - API key for authentication
- `app_id: str` - Application identifier
- `use_query_auth: bool` - Send websocket auth via query params (web) instead of headers (mobile)
- `expire_at: datetime` - Session expiration time
- `sample_rate: int` - Audio sample rate (default: 16000)
- `bitrate: int` - Audio bitrate (default: 0; PCM typically uses 0)
- `transport_frames: Callable[[bytes, bool], None]` - Frame callback
- `on_error: Callable[[Exception], None]` - Error callback
- `on_close: Callable[[], None]` - Close callback
- `region: str` - Region used to compose endpoints (default: `us-west`)
- `console_endpoint_url: str` - Optional explicit console API URL
- `ingress_endpoint_url: str` - Optional explicit ingress WebSocket URL
- `livekit_egress: Optional[LiveKitEgressConfig]` - LiveKit egress configuration

### LiveKitEgressConfig

Configuration for streaming to a LiveKit room.

#### Fields

- `url: str` - LiveKit server URL (e.g., `wss://livekit.example.com`)
- `api_key: str` - Deprecated LiveKit API key
- `api_secret: str` - Deprecated LiveKit API secret
- `api_token: str` - Preferred pre-generated LiveKit access token
- `room_name: str` - LiveKit room name to join
- `publisher_id: str` - Publisher identity in the room
- `extra_attributes: dict[str, str]` - Extra LiveKit participant attributes
- `idle_timeout: int` - Idle timeout in seconds (0 uses server defaults)

### Utility Functions

- `generate_log_id() -> str` - Generate unique log ID in format "YYYYMMDDHHMMSS_\<nanoid\>"

### Exceptions

- `AvatarSDKError` - Structured SDK error with stable code and context fields
- `SessionTokenError` - Subclass of `AvatarSDKError` raised when session token request fails

## Examples

See the [examples](./examples) directory for complete working examples:

- [single_audio_clip](./examples/single_audio_clip) - Basic usage with a single audio file
- [http_service](./examples/http_service) - Simple HTTP API that returns PCM audio (by sample rate) and generated animation Message binaries

## Protocol Buffers

The SDK uses Protocol Buffers for efficient serialization. The proto definitions are in `proto/message.proto`.

### Generating Proto Code

Proto code is generated using [buf](https://buf.build):

```bash
cd proto
buf generate
```

The generated Python code is placed in `src/spatius/proto/generated/`.

### Message Types

- `MESSAGE_CLIENT_CONFIGURE_SESSION` (1) - Client session negotiation parameters
- `MESSAGE_SERVER_CONFIRM_SESSION` (2) - Server confirms and returns `connection_id`
- `MESSAGE_CLIENT_AUDIO_INPUT` (3) - Client audio input
- `MESSAGE_SERVER_ERROR` (4) - Server-side error message
- `MESSAGE_SERVER_RESPONSE_ANIMATION` (5) - Server animation response (`end` indicates final)
- `MESSAGE_CLIENT_INTERRUPT` (7) - Client interrupt signal to stop processing

## Development

### Setup

```bash
# Install uv if not already installed
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone and setup
git clone <repository-url>
cd spatius-sdk-python
uv sync --all-extras
```

### Running Tests

```bash
# Unit tests
uv run pytest
```

### End-to-End Tests

The repository includes opt-in network tests in `tests/test_e2e_errors.py` and
`tests/test_e2e_request.py`. They are skipped by default and only run when
`SPATIUS_RUN_E2E=1` is set.

```bash
SPATIUS_RUN_E2E=1 uv run pytest tests/test_e2e_errors.py tests/test_e2e_request.py
```

Available e2e cases:

- invalid WebSocket credentials -> expects `sessionTokenInvalid`
- valid credentials + missing avatar -> expects `avatarNotFound`
- valid credentials + real avatar + real audio -> sends a request and waits for the final animation frame

Environment variables:

- `SPATIUS_RUN_E2E=1` - Enables e2e tests
- `SPATIUS_E2E_API_KEY` - Required for the real `avatarNotFound` test
- `SPATIUS_E2E_APP_ID` - Required for the real `avatarNotFound` test
- `SPATIUS_E2E_REGION` - Optional region; defaults to `us-west`
- `SPATIUS_E2E_CONSOLE_ENDPOINT` - Optional explicit console endpoint override
- `SPATIUS_E2E_INGRESS_ENDPOINT` - Optional explicit ingress endpoint override
- `SPATIUS_E2E_MISSING_AVATAR_ID` - Optional avatar id that should not exist; defaults to `spatius-e2e-missing-avatar-404`
- `SPATIUS_E2E_AVATAR_ID` - Required for the real request test
- `SPATIUS_E2E_AUDIO_FORMAT` - Optional, `pcm_s16le` or `ogg_opus`; defaults to `pcm_s16le`
- `SPATIUS_E2E_USE_INTERNAL_OGG_OPUS_ENCODER` - Optional, set to `1` to test the SDK's built-in PCM-to-Ogg-Opus encoder
- `SPATIUS_E2E_AUDIO_PATH` - Optional audio file path; defaults to `tests/fixtures/audio/audio_16000.pcm` for PCM and for Ogg Opus when the internal encoder is enabled, otherwise `audio.ogg`
- `SPATIUS_E2E_SAMPLE_RATE` - Optional sample rate; defaults to `16000` for PCM and for Ogg Opus when the internal encoder is enabled, otherwise `24000`
- `SPATIUS_E2E_BITRATE` - Optional bitrate; defaults to `32000`
- `SPATIUS_E2E_CHUNK_SIZE` - Optional chunk size for streaming Ogg Opus; defaults to `4096`
- `SPATIUS_E2E_TIMEOUT_SECONDS` - Optional request timeout; defaults to `45`
- `SPATIUS_E2E_LIVEKIT_URL` - Required for the real invalid livekit tokrn test

Example:

```bash
export SPATIUS_RUN_E2E=1
export SPATIUS_E2E_API_KEY="your-api-key"
export SPATIUS_E2E_APP_ID="your-app-id"
export SPATIUS_E2E_REGION="us-west"
export SPATIUS_E2E_MISSING_AVATAR_ID="spatius-e2e-missing-avatar-404"
export SPATIUS_E2E_AVATAR_ID="your-real-avatar-id"
export SPATIUS_E2E_AUDIO_FORMAT="pcm_s16le"
export SPATIUS_E2E_AUDIO_PATH="tests/fixtures/audio/audio_16000.pcm"
export SPATIUS_E2E_LIVEKIT_URL="wss://livekit.example.com"

uv run pytest tests/test_e2e_errors.py tests/test_e2e_request.py
```

To test the SDK's built-in Ogg Opus encoder with a raw PCM fixture:

```bash
export SPATIUS_RUN_E2E=1
export SPATIUS_E2E_API_KEY="your-api-key"
export SPATIUS_E2E_APP_ID="your-app-id"
export SPATIUS_E2E_REGION="us-west"
export SPATIUS_E2E_AVATAR_ID="your-real-avatar-id"
export SPATIUS_E2E_AUDIO_FORMAT="ogg_opus"
export SPATIUS_E2E_USE_INTERNAL_OGG_OPUS_ENCODER="1"
export SPATIUS_E2E_AUDIO_PATH="tests/fixtures/audio/audio_16000.pcm"
export SPATIUS_E2E_SAMPLE_RATE="16000"

uv run pytest tests/test_e2e_request.py -k send_audio_receives_animation_frames -s
```

If the credentialed variables are missing, the invalid-token e2e test still runs, and the
real `avatarNotFound` test is skipped automatically.

To run only the real request smoke test:

```bash
SPATIUS_RUN_E2E=1 uv run pytest tests/test_e2e_request.py -k send_audio_receives_animation_frames -s
```

## License

See [LICENSE](./LICENSE) for details.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.
