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

## Telemetry

The SDK exports anonymous OpenTelemetry metrics and traces by default. Configure the process-wide OTLP base endpoint before using a session, or disable export with an empty string:

```python
from spatius import configure_telemetry, shutdown_telemetry

configure_telemetry("https://telemetry.example.com")
# configure_telemetry("")  # disable metrics and traces

# Useful for short-lived processes that exit immediately after a session.
shutdown_telemetry()
```

The first audio message for each request carries W3C trace context to the backend. Later chunks omit it. OTel resources include `service.name=spatius-python`, `sdk.platform=python`, `app_id`, and the resolved `region`. The server SDK exports metrics and traces only; it does not upload telemetry logs.

## Warm-up

Session creation performs region scheduling (bootstrap API) and a session-token exchange (console API) before the ingress WebSocket connects. `prewarm()` moves that work ahead of dispatch — e.g. into a worker's process warm-up hook — so `init()` reuses the cached results:

```python
from spatius import prewarm

# best-effort, never raises
await prewarm(app_id="your-app-id", api_key="your-api-key")
```

This resolves and caches the `auto` region (reused for 5 minutes) and opens throwaway TLS connections to the console and ingress hosts, priming DNS and the process-wide TLS session cache shared by HTTP and WebSocket connections. Pass `prefetch_session_token=True` to also cache a session token, letting the next `init()` with matching credentials skip the console API round trip entirely (assumes the backend allows a token to back more than one session).

## Documentation

See the full Python SDK guide at [docs.spatius.ai/sdk-reference/python-sdk/python-sdk](https://docs.spatius.ai/sdk-reference/python-sdk/python-sdk).

## License

MIT
