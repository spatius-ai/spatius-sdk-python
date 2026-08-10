"""Opt-in live telemetry smoke test.

Run manually with:

    SPATIUS_RUN_TELEMETRY_INTEGRATION=1 uv run pytest \
        tests/test_telemetry_integration.py -q

The test uses a synthetic session and never contacts the avatar service. It
only exercises the SDK telemetry exporters against the deployed proxy.
"""

import os
import unittest

from spatius import AvatarSession, configure_telemetry, shutdown_telemetry
from spatius.proto.generated import message_pb2
from spatius.session_config import SessionConfig


@unittest.skipUnless(
    os.getenv("SPATIUS_RUN_TELEMETRY_INTEGRATION") == "1",
    "set SPATIUS_RUN_TELEMETRY_INTEGRATION=1 to send telemetry to the deployed proxy",
)
class TestLiveTelemetryProxy(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_exports_metrics_and_traces_to_deployed_proxy(self):
        class FakeConnection:
            async def send(self, _data):
                pass

            async def close(self):
                pass

        configure_telemetry("https://t.spatialwalk.top")
        try:
            session = AvatarSession(
                SessionConfig(
                    app_id="python-sdk-telemetry-integration",
                    avatar_id="telemetry-smoke-test",
                    region="us-west",
                    console_endpoint_url="https://console.example.com/v1/console",
                    ingress_endpoint_url="wss://api.example.com/v2/driveningress",
                )
            )
            session._connection = FakeConnection()

            req_id = await session.send_audio(b"\x00\x00" * 160, end=True)

            response = message_pb2.Message()
            response.type = message_pb2.MESSAGE_SERVER_RESPONSE_ANIMATION
            response.server_response_animation.req_id = req_id
            response.server_response_animation.end = True
            await session._handle_binary_message(response.SerializeToString())
            await session.close()
        finally:
            # shutdown() flushes the batch span processor and metric reader.
            shutdown_telemetry()


if __name__ == "__main__":
    unittest.main()
