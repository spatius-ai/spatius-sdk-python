"""Configuration options and factory for avatar sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Callable, Optional, Union, TYPE_CHECKING
import warnings

if TYPE_CHECKING:
    from .avatar_session import AvatarSession


DEFAULT_REGION = "us-west"


class AudioFormat(str, Enum):
    """Audio input encoding negotiated for a session."""

    PCM_S16LE = "pcm_s16le"
    OGG_OPUS = "ogg_opus"


@dataclass
class OggOpusEncoderConfig:
    """Optional client-side Ogg Opus encoder settings."""

    frame_duration_ms: int = 20
    application: str = "audio"


@dataclass
class LiveKitEgressConfig:
    """
    Configuration for streaming to a LiveKit room.

    When set on a SessionConfig, audio and animation data are streamed to a LiveKit room
    via the egress service instead of being returned through the WebSocket connection.

    Attributes:
        url: LiveKit server URL (e.g., wss://livekit.example.com).
        api_key: Deprecated. LiveKit API key. Optional when api_token is provided.
        api_secret: Deprecated. LiveKit API secret. Optional when api_token is provided.
        api_token: Pre-generated LiveKit access token. Preferred over api_key and
            api_secret when provided.
        room_name: LiveKit room name to join.
        publisher_id: Publisher identity in the room.
        extra_attributes: Additional key-value attributes for the LiveKit participant.
        idle_timeout: Idle timeout in seconds for egress connection auto-close. 0 means
            use server defaults.
    """

    url: str = ""
    # deprecated
    api_key: str = field(default="", repr=False)
    # deprecated
    api_secret: str = field(default="", repr=False)
    api_token: str = field(default="", repr=False)
    room_name: str = ""
    publisher_id: str = ""
    extra_attributes: dict[str, str] = field(default_factory=dict)
    idle_timeout: int = 0

    def __post_init__(self) -> None:
        if self.api_key or self.api_secret:
            warnings.warn(
                "LiveKitEgressConfig.api_key and LiveKitEgressConfig.api_secret are "
                "deprecated and will be removed in a future release; use api_token "
                "instead.",
                FutureWarning,
                stacklevel=2,
            )


@dataclass
class AgoraEgressConfig:
    """
    Configuration for streaming to an Agora channel.

    When set on a SessionConfig, audio and animation data are streamed to an Agora channel
    via the egress service instead of being returned through the WebSocket connection.

    Attributes:
        channel_name: Agora channel name to join.
        token: Agora token for authentication (optional for testing).
        uid: Publisher UID in the channel (0 for auto-assign).
        publisher_id: Publisher identity/name.
    """

    channel_name: str = ""
    token: str = field(default="", repr=False)
    uid: int = 0
    publisher_id: str = ""


@dataclass
class SessionConfig:
    """
    Configuration for an AvatarSession.

    Attributes:
        avatar_id: The avatar identifier for the session.
        api_key: The API key for authentication.
        app_id: The application identifier.
        use_query_auth: If true, send app/session credentials as URL query params (web-style
            auth). If false (default), send them as headers (mobile-style auth).
        expire_at: Expiration time for the session.
        sample_rate: Audio sample rate in Hz (default: 16000).
        bitrate: Audio bitrate (if applicable to the selected audio_format). For PCM this
            may be 0.
        audio_format: Session audio input format. PCM remains the default for backward
            compatibility. Use OGG_OPUS when streaming one continuous Ogg Opus stream per
            request ID.
        ogg_opus_encoder: Optional client-side encoder settings. When set together with
            ``audio_format=AudioFormat.OGG_OPUS``, ``send_audio()`` accepts raw PCM input
            and the SDK encodes it to continuous Ogg Opus before sending.
        on_encoded_audio: Optional callback invoked when internal Ogg Opus encoding
            finishes for a request. Receives ``(req_id, encoded_audio_bytes)``.
        transport_frames: Callback for receiving animation frames (frame_data, is_last).
        on_error: Callback for error handling.
        on_close: Callback invoked when session closes.
        region: Spatius region used to compose endpoint URLs when explicit URLs are not
            provided. Defaults to "us-west".
        console_endpoint_url: URL for the console API endpoint.
        ingress_endpoint_url: URL for the ingress websocket endpoint.
        livekit_egress: If set, enables LiveKit egress mode - audio and animation are
            streamed to a LiveKit room via the egress service.
        agora_egress: If set, enables Agora egress mode - audio and animation are
            streamed to an Agora channel via the egress service.
    """

    avatar_id: str = ""
    api_key: str = field(default="", repr=False)
    app_id: str = ""
    use_query_auth: bool = False
    expire_at: Optional[datetime] = None
    sample_rate: int = 16000
    bitrate: int = 0
    audio_format: AudioFormat = AudioFormat.PCM_S16LE
    ogg_opus_encoder: Optional[OggOpusEncoderConfig] = None
    on_encoded_audio: Optional[Callable[[str, bytes], None]] = None
    transport_frames: Callable[[bytes, bool], None] = field(
        default=lambda data, last: None
    )
    on_error: Callable[[Exception], None] = field(default=lambda err: None)
    on_close: Callable[[], None] = field(default=lambda: None)
    region: str = DEFAULT_REGION
    console_endpoint_url: str = ""
    ingress_endpoint_url: str = ""
    livekit_egress: Optional[LiveKitEgressConfig] = None
    agora_egress: Optional[AgoraEgressConfig] = None

    def __post_init__(self) -> None:
        self.audio_format = AudioFormat(self.audio_format)
        self.region = self.region.strip()
        if self.region and not self.console_endpoint_url:
            self.console_endpoint_url = (
                f"https://console.{self.region}.spatius.ai/v1/console"
            )
        if self.region and not self.ingress_endpoint_url:
            self.ingress_endpoint_url = (
                f"wss://api.{self.region}.spatius.ai/v2/driveningress"
            )


def _noop_transport_frames(data: bytes, last: bool) -> None:
    pass


def _noop_error(error: Exception) -> None:
    pass


def _noop_close() -> None:
    pass


def new_avatar_session(
    *,
    avatar_id: str = "",
    api_key: str = "",
    app_id: str = "",
    use_query_auth: bool = False,
    expire_at: Optional[datetime] = None,
    sample_rate: int = 16000,
    bitrate: int = 0,
    audio_format: Union[AudioFormat, str] = AudioFormat.PCM_S16LE,
    ogg_opus_encoder: Optional[OggOpusEncoderConfig] = None,
    on_encoded_audio: Optional[Callable[[str, bytes], None]] = None,
    transport_frames: Callable[[bytes, bool], None] = _noop_transport_frames,
    on_error: Callable[[Exception], None] = _noop_error,
    on_close: Callable[[], None] = _noop_close,
    region: str = DEFAULT_REGION,
    console_endpoint_url: str = "",
    ingress_endpoint_url: str = "",
    livekit_egress: Optional[LiveKitEgressConfig] = None,
    agora_egress: Optional[AgoraEgressConfig] = None,
) -> "AvatarSession":
    """
    Create an AvatarSession from typed configuration parameters.

    Explicit endpoint URLs take precedence. Otherwise, ``region`` composes:
    ``https://console.<region>.spatius.ai/v1/console`` and
    ``wss://api.<region>.spatius.ai/v2/driveningress``.
    """
    from .avatar_session import AvatarSession

    config = SessionConfig(
        avatar_id=avatar_id,
        api_key=api_key,
        app_id=app_id,
        use_query_auth=use_query_auth,
        expire_at=expire_at,
        sample_rate=sample_rate,
        bitrate=bitrate,
        audio_format=audio_format,
        ogg_opus_encoder=ogg_opus_encoder,
        on_encoded_audio=on_encoded_audio,
        transport_frames=transport_frames,
        on_error=on_error,
        on_close=on_close,
        region=region,
        console_endpoint_url=console_endpoint_url,
        ingress_endpoint_url=ingress_endpoint_url,
        livekit_egress=livekit_egress,
        agora_egress=agora_egress,
    )
    return AvatarSession(config)
