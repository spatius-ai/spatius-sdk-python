import re
import unittest
from unittest.mock import patch

from opentelemetry.sdk.metrics.export import MetricExportResult
from opentelemetry.sdk.trace.export import SpanExportResult

import spatius.telemetry as telemetry
from spatius import (
    AvatarSession,
    SessionConfig,
    configure_telemetry,
    shutdown_telemetry,
)
from spatius.proto.generated import message_pb2


class _SpanExporter:
    def __init__(self):
        self.spans = []

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self, **_kwargs):
        return None

    def force_flush(self, **_kwargs):
        return True


class _MetricExporter:
    def __init__(self):
        self.exports = []
        self._preferred_temporality = {}
        self._preferred_aggregation = {}

    def export(self, metrics_data, **_kwargs):
        self.exports.append(metrics_data)
        return MetricExportResult.SUCCESS

    def shutdown(self, **_kwargs):
        return None

    def force_flush(self, **_kwargs):
        return True


class _FakeConnection:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(bytes(data))


class TestTelemetryConfiguration(unittest.TestCase):
    def tearDown(self):
        shutdown_telemetry()
        configure_telemetry("")

    def test_default_endpoint_and_signal_paths(self):
        configure_telemetry(None)
        self.assertEqual(
            telemetry.DEFAULT_TELEMETRY_ENDPOINT,
            "https://t.spatialwalk.top",
        )
        self.assertEqual(
            telemetry._signal_endpoint("metrics"),
            "https://t.spatialwalk.top/v1/metrics",
        )
        self.assertEqual(
            telemetry._signal_endpoint("traces"),
            "https://t.spatialwalk.top/v1/traces",
        )

    def test_empty_endpoint_disables_telemetry(self):
        configure_telemetry("")
        self.assertFalse(telemetry.telemetry_enabled())
        self.assertIsNone(telemetry.start_span("disabled"))

    def test_invalid_endpoint_is_rejected(self):
        with self.assertRaises(ValueError):
            configure_telemetry("collector.example.com")
        with self.assertRaises(ValueError):
            configure_telemetry("https://collector.example.com?token=secret")

    def test_custom_endpoint_is_used_without_authentication(self):
        captured = {}
        span_exporter = _SpanExporter()
        metric_exporter = _MetricExporter()

        def make_span_exporter(*, endpoint, headers, timeout):
            captured["traces"] = (endpoint, headers, timeout)
            return span_exporter

        def make_metric_exporter(*, endpoint, headers, timeout, **kwargs):
            captured["metrics"] = (endpoint, headers, timeout, kwargs)
            return metric_exporter

        configure_telemetry("https://collector.example.com/otlp/")
        with (
            patch.object(telemetry, "OTLPSpanExporter", side_effect=make_span_exporter),
            patch.object(
                telemetry, "OTLPMetricExporter", side_effect=make_metric_exporter
            ),
        ):
            telemetry.set_resource_context(app_id="app-1", region="eu-central")
            telemetry.record_metric("test.metric", 1)
            span = telemetry.start_span("test.span")
            telemetry.finish_span(span)
            self.assertEqual(
                telemetry._tracer_provider.resource.attributes["service.name"],
                "spatius-python",
            )
            self.assertEqual(
                telemetry._tracer_provider.resource.attributes["sdk.platform"],
                "python",
            )
            self.assertEqual(
                telemetry._tracer_provider.resource.attributes["app_id"], "app-1"
            )
            self.assertEqual(
                telemetry._tracer_provider.resource.attributes["region"],
                "eu-central",
            )
            self.assertEqual(
                telemetry._meter_provider._sdk_config.views[0]._aggregation._boundaries,
                [100, 200, 500, 1000, 2000, 3000, 4000, 5000],
            )
            telemetry.shutdown_telemetry()

        self.assertEqual(
            captured["metrics"][0], "https://collector.example.com/otlp/v1/metrics"
        )
        self.assertEqual(
            captured["traces"][0], "https://collector.example.com/otlp/v1/traces"
        )
        self.assertNotIn("Authorization", captured["metrics"][1])
        self.assertNotIn("Authorization", captured["traces"][1])
        self.assertEqual(captured["metrics"][1]["User-Agent"], "spatius-python-sdk")
        self.assertEqual(
            captured["metrics"][3]["preferred_temporality"][telemetry.Histogram],
            telemetry.AggregationTemporality.DELTA,
        )

    def test_exporter_failures_do_not_escape_recording(self):
        configure_telemetry("https://collector.example.com")
        with (
            patch.object(
                telemetry,
                "OTLPMetricExporter",
                side_effect=RuntimeError("metric exporter unavailable"),
            ),
            patch.object(
                telemetry,
                "OTLPSpanExporter",
                side_effect=RuntimeError("trace exporter unavailable"),
            ),
        ):
            telemetry.record_metric("test.metric", 1)
            span = telemetry.start_span("test.span")
            telemetry.finish_span(span)


class TestTracePropagation(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        shutdown_telemetry()
        configure_telemetry("")

    async def test_only_first_audio_chunk_contains_trace_context(self):
        span_exporter = _SpanExporter()
        metric_exporter = _MetricExporter()
        configure_telemetry("https://collector.example.com")
        with (
            patch.object(telemetry, "OTLPSpanExporter", return_value=span_exporter),
            patch.object(telemetry, "OTLPMetricExporter", return_value=metric_exporter),
        ):
            session = AvatarSession(
                SessionConfig(
                    api_key="api",
                    app_id="app",
                    avatar_id="avatar",
                    console_endpoint_url="https://console.example.com",
                    ingress_endpoint_url="wss://api.example.com",
                )
            )
            session._connection = _FakeConnection()

            req_id = await session.send_audio(b"first", end=False)
            await session.send_audio(b"second", end=True)

            first = message_pb2.Message()
            first.ParseFromString(session._connection.sent[0])
            second = message_pb2.Message()
            second.ParseFromString(session._connection.sent[1])

            traceparent = first.client_audio_input.trace_context.traceparent
            self.assertRegex(
                traceparent,
                re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"),
            )
            self.assertFalse(second.client_audio_input.HasField("trace_context"))

            response = message_pb2.Message()
            response.type = message_pb2.MESSAGE_SERVER_RESPONSE_ANIMATION
            response.server_response_animation.req_id = req_id
            response.server_response_animation.end = True
            await session._handle_binary_message(response.SerializeToString())

            self.assertNotIn(req_id, session._request_telemetry)
            await session.close()


if __name__ == "__main__":
    unittest.main()
