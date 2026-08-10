import pytest

from spatius import configure_telemetry, shutdown_telemetry


@pytest.fixture(autouse=True)
def disable_network_telemetry_for_tests():
    """Keep the test suite independent from the production OTLP endpoint."""
    shutdown_telemetry()
    configure_telemetry("")
    yield
    shutdown_telemetry()
    configure_telemetry("")
