# AGENTS.md

Guidance for coding agents working in this repository.

## What this repo is

Python SDK for WebSocket-based avatar sessions, published as `spatius` on PyPI. Clients stream audio to the backend and receive animation frames back. The wire protocol is protobuf (`proto/message.proto`).

## Layout

- `src/spatius/avatar_session.py` — `AvatarSession`: session-token HTTP exchange, WebSocket handshake (v2 protocol), audio send, frame receive loop. Session flow: `new_avatar_session()` → `init()` → `start()` → `send_audio()` → `close()`.
- `src/spatius/session_config.py` — `SessionConfig`, LiveKit/Agora egress configs, `new_avatar_session()` factory.
- `src/spatius/bootstrap.py` — resolves `region="auto"` via the global bootstrap API; process-wide 5-minute cache, never raises.
- `src/spatius/net.py` — process-wide TLS context and connector factory shared by HTTP and WebSocket connections.
- `src/spatius/prewarm.py`, `src/spatius/token_cache.py` — optional warm-up (region resolution, TLS, session token) ahead of session dispatch.
- `src/spatius/telemetry.py` — process-wide OpenTelemetry metrics/traces, on by default; `configure_telemetry("")` disables.
- `src/spatius/errors.py` — `AvatarSDKError` with stable error codes.
- `proto/message.proto`, `src/spatius/proto/generated/` — protocol definition and generated code (regenerate with `cd proto && buf generate`).

Behavioral facts that are easy to get wrong:

- Auth is header-based by default; `use_query_auth=True` switches to query-param auth.
- Egress modes (LiveKit/Agora) stream output to a room/channel instead of the WebSocket; the `transport_frames` callback is not invoked in egress mode, and `interrupt()` only works there.
- The first audio message per request carries W3C trace context; later chunks omit it.

## Build and development commands

```bash
uv sync                  # install dependencies
pytest                   # run tests
pytest tests/test_avatar_session_v2.py::TestAvatarSessionV2::test_init_success  # single test
./test-local.sh all      # test all Python versions (3.10-3.14) and dependency combinations
cd proto && buf generate # regenerate protobuf code after editing message.proto
```

## Rules for agents

- Never add or change content in `README.md` unless explicitly told to.
- After editing `proto/message.proto`, always regenerate the code under `src/spatius/proto/generated/` with `buf generate`.
