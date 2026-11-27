# AGENTS.md

Guidance for coding agents working in this repository.

## What this repo is

Python SDK for WebSocket-based avatar sessions, published as `spatius` on PyPI. Clients stream audio to the backend and receive V3 motion messages back. The wire protocol is protobuf (`proto/message.proto`).

## Layout

- `src/spatius/avatar_session.py` — `AvatarSession`: session-token HTTP exchange, WebSocket handshake (v3 protocol), audio send, motion receive loop. Session flow: `new_avatar_session()` → `init()` → `start()` → `send_audio()` → `close()`.
- `src/spatius/session_config.py` — `SessionConfig`, LiveKit/Agora transport configs, `new_avatar_session()` factory.
- `src/spatius/bootstrap.py` — resolves `region="auto"` via the global bootstrap API; process-wide 5-minute cache, never raises.
- `src/spatius/net.py` — process-wide TLS context and connector factory shared by HTTP and WebSocket connections.
- `src/spatius/prewarm.py`, `src/spatius/token_cache.py` — optional warm-up (region resolution, TLS, session token) ahead of session dispatch.
- `src/spatius/telemetry.py` — process-wide OpenTelemetry metrics/traces, on by default; `configure_telemetry("")` disables.
- `src/spatius/errors.py` — `AvatarSDKError` with stable error codes.
- `proto/message.proto`, `src/spatius/proto/generated/` — protocol definition and generated code (regenerate with `cd proto && buf generate`).

Behavioral facts that are easy to get wrong:

- Auth is header-based by default; `use_query_auth=True` switches to query-param auth.
- WebSocket is the default transport. Optional LiveKit/Agora transports stream output to a room/channel; configuration, audio input and interruption still use the WebSocket.
- The `transport_frames` callback receives the complete, unchanged V3 `Message` binary containing `server_response_motion`, plus its `end` flag.
- W3C trace context belongs to `ClientConfigureSession`, not individual audio messages.
- The Python `sample_rate` option maps to the wire field `audio_sample_rate`. Python `bitrate` configures only the optional local audio encoder.
- Keep public protobuf source free of comments and internal implementation metadata.

## Build and development commands

```bash
uv sync                  # install dependencies
pytest                   # run tests
pytest tests/test_avatar_session_v3.py::TestAvatarSessionV3::test_init_success  # single test
./test-local.sh all      # test all Python versions (3.10-3.14) and dependency combinations
cd proto && buf generate # regenerate protobuf code after editing message.proto
```

## Rules for agents

- Never add or change content in `README.md` unless explicitly told to.
- After editing `proto/message.proto`, always regenerate the code under `src/spatius/proto/generated/` with `buf generate`.
