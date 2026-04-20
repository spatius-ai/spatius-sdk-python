"""Public package for the Spatius Python SDK."""

from avatarkit import (
    AgoraEgressConfig,
    AudioFormat,
    AvatarSDKError,
    AvatarSDKErrorCode,
    AvatarSession,
    LiveKitEgressConfig,
    OggOpusEncoderConfig,
    SessionConfig,
    SessionConfigBuilder,
    SessionTokenError,
    generate_log_id,
    new_avatar_session,
)
from avatarkit import __version__

__all__ = [
    "AvatarSession",
    "SessionTokenError",
    "AvatarSDKError",
    "AvatarSDKErrorCode",
    "new_avatar_session",
    "AudioFormat",
    "OggOpusEncoderConfig",
    "SessionConfig",
    "SessionConfigBuilder",
    "LiveKitEgressConfig",
    "AgoraEgressConfig",
    "generate_log_id",
    "__version__",
]
