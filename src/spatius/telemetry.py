"""Process-wide OpenTelemetry metrics and traces for the server SDK.

This module intentionally does not register global OpenTelemetry providers. An
application embedding the SDK may have its own providers, and this private
provider must remain independently configurable and flushable.

Only metrics and traces are exported. There is no OpenTelemetry logs provider
or log exporter in the server SDK.
"""

from __future__ import annotations

import atexit
import logging
import threading
from importlib.metadata import PackageNotFoundError, version
from math import isfinite
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span, StatusCode
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

logger = logging.getLogger(__name__)

DEFAULT_TELEMETRY_ENDPOINT = "https://t.spatialwalk.top"
TELEMETRY_EXPORT_INTERVAL_MS = 10_000
TELEMETRY_EXPORT_TIMEOUT_MS = 5_000

# A non-empty header prevents the OTLP exporters from importing credentials from
# OTEL_EXPORTER_OTLP_HEADERS. This SDK's endpoint is intentionally unauthenticated.
_EXPORT_HEADERS = {"User-Agent": "spatius-python-sdk"}

_lock = threading.RLock()
_endpoint = DEFAULT_TELEMETRY_ENDPOINT
_initialization_attempted = False
_tracer_provider: Optional[TracerProvider] = None
_meter_provider: Optional[MeterProvider] = None
_tracer = None
_meter = None
_histograms: dict[str, Any] = {}
_trace_propagator = TraceContextTextMapPropagator()


def _sdk_version() -> str:
    try:
        return version("spatius")
    except PackageNotFoundError:  # pragma: no cover - only when not installed
        return "0+unknown"


def _normalize_endpoint(endpoint: str) -> str:
    value = endpoint.strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(
            "Telemetry endpoint must be an absolute http:// or https:// URL"
        )
    if parsed.query or parsed.fragment:
        raise ValueError("Telemetry endpoint must not contain a query or fragment")
    return value.rstrip("/")


def _signal_endpoint(signal: str) -> str:
    return f"{_endpoint}/v1/{signal}"


def configure_telemetry(endpoint: Optional[str] = None) -> None:
    """Configure the process-wide OTLP base endpoint.

    Args:
        endpoint: A base URL. ``None`` restores the built-in endpoint and an
            empty string disables both metrics and traces.

    Raises:
        RuntimeError: If a different endpoint is configured after providers
            have already been initialized. Configure before creating/using a
            session, or call ``shutdown_telemetry()`` first.
        ValueError: If the endpoint is not an absolute HTTP(S) URL.
    """
    global _endpoint
    normalized = (
        DEFAULT_TELEMETRY_ENDPOINT
        if endpoint is None
        else _normalize_endpoint(endpoint)
    )
    with _lock:
        if (
            _tracer_provider is not None or _meter_provider is not None
        ) and normalized != _endpoint:
            raise RuntimeError(
                "Telemetry is already initialized; call shutdown_telemetry() "
                "before changing its endpoint"
            )
        _endpoint = normalized


def telemetry_enabled() -> bool:
    """Return whether telemetry has a non-empty configured endpoint."""
    with _lock:
        return bool(_endpoint)


def _ensure_initialized() -> None:
    global _initialization_attempted, _tracer_provider, _meter_provider, _tracer, _meter
    with _lock:
        if not _endpoint or _initialization_attempted:
            return
        _initialization_attempted = True

        resource = Resource.create(
            {
                "service.name": "spatius",
                "sdk.platform": "python",
                "sdk.package": "spatius-python",
                "sdk.version": _sdk_version(),
            }
        )

        try:
            span_exporter = OTLPSpanExporter(
                endpoint=_signal_endpoint("traces"),
                headers=dict(_EXPORT_HEADERS),
                timeout=TELEMETRY_EXPORT_TIMEOUT_MS / 1000,
            )
            provider = TracerProvider(resource=resource, shutdown_on_exit=False)
            provider.add_span_processor(BatchSpanProcessor(span_exporter))
            _tracer_provider = provider
            _tracer = provider.get_tracer("spatius", _sdk_version())
        except Exception:
            logger.warning("Failed to initialize OpenTelemetry traces", exc_info=True)

        try:
            metric_exporter = OTLPMetricExporter(
                endpoint=_signal_endpoint("metrics"),
                headers=dict(_EXPORT_HEADERS),
                timeout=TELEMETRY_EXPORT_TIMEOUT_MS / 1000,
            )
            reader = PeriodicExportingMetricReader(
                metric_exporter,
                export_interval_millis=TELEMETRY_EXPORT_INTERVAL_MS,
                export_timeout_millis=TELEMETRY_EXPORT_TIMEOUT_MS,
            )
            provider = MeterProvider(
                resource=resource,
                metric_readers=[reader],
                shutdown_on_exit=False,
            )
            _meter_provider = provider
            _meter = provider.get_meter("spatius", _sdk_version())
        except Exception:
            logger.warning("Failed to initialize OpenTelemetry metrics", exc_info=True)


def start_span(
    name: str,
    attributes: Optional[Mapping[str, Any]] = None,
) -> Optional[Span]:
    """Start a span, returning ``None`` when telemetry is disabled/unavailable."""
    _ensure_initialized()
    with _lock:
        tracer = _tracer
    if tracer is None:
        return None
    try:
        return tracer.start_span(name, attributes=dict(attributes or {}))
    except Exception:
        logger.debug("Failed to start OpenTelemetry span %s", name, exc_info=True)
        return None


def finish_span(
    span: Optional[Span],
    *,
    attributes: Optional[Mapping[str, Any]] = None,
    error: Optional[BaseException] = None,
) -> None:
    """Set final span data and end it, isolating all telemetry failures."""
    if span is None:
        return
    try:
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        if error is not None:
            span.record_exception(error)
            span.set_status(StatusCode.ERROR)
        span.end()
    except Exception:
        logger.debug("Failed to finish OpenTelemetry span", exc_info=True)


def add_span_event(
    span: Optional[Span],
    name: str,
    attributes: Optional[Mapping[str, Any]] = None,
) -> None:
    if span is None:
        return
    try:
        span.add_event(name, attributes=dict(attributes or {}))
    except Exception:
        logger.debug("Failed to add OpenTelemetry span event %s", name, exc_info=True)


def inject_trace_context(span: Optional[Span]) -> dict[str, str]:
    """Return W3C trace context for a first audio message."""
    if span is None:
        return {}
    try:
        carrier: dict[str, str] = {}
        context: Context = trace.set_span_in_context(span)
        _trace_propagator.inject(carrier, context=context)
        return carrier
    except Exception:
        logger.debug("Failed to inject OpenTelemetry trace context", exc_info=True)
        return {}


def record_metric(
    name: str,
    value: float,
    attributes: Optional[Mapping[str, Any]] = None,
) -> None:
    """Record a histogram observation without affecting SDK behavior."""
    if not isfinite(value):
        return
    _ensure_initialized()
    with _lock:
        meter = _meter
        histogram = _histograms.get(name)
        if meter is None:
            return
        if histogram is None:
            try:
                histogram = meter.create_histogram(name)
                _histograms[name] = histogram
            except Exception:
                logger.debug(
                    "Failed to create OpenTelemetry metric %s", name, exc_info=True
                )
                return
    try:
        histogram.record(value, attributes=dict(attributes or {}))
    except Exception:
        logger.debug("Failed to record OpenTelemetry metric %s", name, exc_info=True)


def record_http_client_duration(
    *,
    operation: str,
    method: str,
    duration_ms: float,
    status_code: Optional[int] = None,
    server_address: Optional[str] = None,
) -> None:
    attributes: dict[str, Any] = {
        "http.request.method": method,
        "operation": operation or "_OTHER",
    }
    if server_address:
        attributes["server.address"] = server_address
    if status_code is not None:
        attributes["http.response.status_code"] = status_code
    else:
        attributes["error.type"] = "transport_error"
    record_metric("http.client.request.duration", duration_ms, attributes)


def force_flush() -> None:
    """Flush both providers without shutting them down."""
    with _lock:
        tracer_provider = _tracer_provider
        meter_provider = _meter_provider
    for provider in (tracer_provider, meter_provider):
        if provider is None:
            continue
        try:
            provider.force_flush(timeout_millis=TELEMETRY_EXPORT_TIMEOUT_MS)
        except Exception:
            logger.debug("Failed to flush OpenTelemetry provider", exc_info=True)


def shutdown_telemetry() -> None:
    """Flush and shut down the process-wide providers."""
    global _initialization_attempted, _tracer_provider, _meter_provider, _tracer, _meter
    with _lock:
        tracer_provider = _tracer_provider
        meter_provider = _meter_provider
        _tracer_provider = None
        _meter_provider = None
        _tracer = None
        _meter = None
        _histograms.clear()
        _initialization_attempted = False
    for provider in (tracer_provider, meter_provider):
        if provider is None:
            continue
        try:
            provider.shutdown()
        except Exception:
            logger.debug("Failed to shut down OpenTelemetry provider", exc_info=True)


atexit.register(shutdown_telemetry)

__all__ = [
    "DEFAULT_TELEMETRY_ENDPOINT",
    "configure_telemetry",
    "force_flush",
    "record_http_client_duration",
    "record_metric",
    "shutdown_telemetry",
    "start_span",
    "finish_span",
    "add_span_event",
    "inject_trace_context",
    "telemetry_enabled",
]
