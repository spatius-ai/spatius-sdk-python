"""Spatius Python SDK for avatar sessions."""

from importlib.metadata import PackageNotFoundError, version

from .avatar_session import AvatarSession
from .errors import AvatarSDKError, AvatarSDKErrorCode, SessionTokenError
from .session_config import (
    AudioFormat,
    OggOpusEncoderConfig,
    SessionConfig,
    LiveKitEgressConfig,
    AgoraEgressConfig,
    new_avatar_session,
)
from .logid import generate_log_id
from .telemetry import (
    DEFAULT_TELEMETRY_ENDPOINT,
    configure_telemetry,
    shutdown_telemetry,
)

try:
    __version__ = version("spatius")
except PackageNotFoundError:  # pragma: no cover - only when imported without install
    __version__ = "0+unknown"

__all__ = [
    "AvatarSession",
    "SessionTokenError",
    "AvatarSDKError",
    "AvatarSDKErrorCode",
    "new_avatar_session",
    "AudioFormat",
    "OggOpusEncoderConfig",
    "SessionConfig",
    "LiveKitEgressConfig",
    "AgoraEgressConfig",
    "generate_log_id",
    "DEFAULT_TELEMETRY_ENDPOINT",
    "configure_telemetry",
    "shutdown_telemetry",
]
