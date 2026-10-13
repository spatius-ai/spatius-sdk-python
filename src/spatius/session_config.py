"""Configuration options and factory for avatar sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Optional, TYPE_CHECKING
import re
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .avatar_session import AvatarSession


DEFAULT_REGION = "us-west"
CN_REGION_PREFIX = "cn-"

# Sentinel meaning "let the global bootstrap API schedule the ingress region".
# This is the default when the user does not pin a region; it is not a usable
# region by itself and is resolved in ``AvatarSession.init()``.
DEFAULT_REGION_REQUEST = "auto"

# Default session-token lifetime applied by ``new_avatar_session()`` when the
# caller does not pass ``expire_at``.
DEFAULT_SESSION_TOKEN_TTL = timedelta(hours=1)


class AudioFormat(str, Enum):
    """
    Audio input format negotiated with the avatar service.

    Values:
        PCM_S16LE: Raw mono 16-bit little-endian PCM bytes.
        OGG_OPUS: One continuous mono Ogg Opus stream per request ID.
    """

    PCM_S16LE = "pcm_s16le"
    OGG_OPUS = "ogg_opus"


@dataclass
class OggOpusEncoderConfig:
    """
    Settings for the optional client-side PCM to Ogg Opus encoder.

    Pass this to ``new_avatar_session(ogg_opus_encoder=...)`` together with
    ``audio_format=AudioFormat.OGG_OPUS`` when callers provide raw PCM input but the
    session should send Ogg Opus to the service.

    Attributes:
        frame_duration_ms: Opus frame duration in milliseconds. Supported values are
            10, 20, 40, and 60.
        application: Opus encoder application mode. Defaults to ``voip``. Supported
            values are ``audio``, ``voip``, and ``restricted_lowdelay``.
    """

    frame_duration_ms: int = 20
    application: str = "voip"


def _require_nonempty_string(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string")


@dataclass
class WebSocketTransportConfig:
    """Return motion data through the session's WebSocket connection."""


@dataclass
class LiveKitTransportConfig:
    """Publish audio and motion to a LiveKit room authorized by ``token``.

    The token supplies the room and publisher identity. ``extra_attributes``
    update the participant after joining, requiring the token's
    ``canUpdateOwnMetadata`` permission. Empty attribute values remove keys.
    ``agent_identity`` identifies the source agent receiving playback callbacks.
    A zero ``idle_timeout`` uses the server default.
    """

    url: str
    token: str = field(repr=False)
    idle_timeout: int = 0
    agent_identity: str = ""
    extra_attributes: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_nonempty_string("url", self.url)
        try:
            parsed = urlsplit(self.url)
            valid_url = parsed.scheme in {"ws", "wss", "http", "https"} and bool(
                parsed.hostname
            )
            parsed.port
        except ValueError:
            valid_url = False
        if not valid_url or any(character.isspace() for character in self.url):
            raise ValueError("url must be an absolute ws, wss, http, or https URL")
        _require_nonempty_string("token", self.token)
        if (
            isinstance(self.idle_timeout, bool)
            or not isinstance(self.idle_timeout, int)
            or not 0 <= self.idle_timeout <= 2**31 - 1
        ):
            raise ValueError("idle_timeout must be an integer from 0 to 2147483647")
        if not isinstance(self.agent_identity, str):
            raise ValueError("agent_identity must be a string")
        if not isinstance(self.extra_attributes, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.extra_attributes.items()
        ):
            raise ValueError("extra_attributes must map strings to strings")
        self.extra_attributes = dict(self.extra_attributes)


_AGORA_ACCOUNT = re.compile(r"[a-zA-Z0-9 !#$%&()+,.:;<=>?@\[\]^_{|}~-]+", re.ASCII)


@dataclass
class AgoraTransportConfig:
    """Publish audio and motion to an Agora channel.

    Set exactly one of ``uid`` or ``user_account``. Numeric UIDs must be from 1
    through 4294967295. String accounts are case-sensitive, at most 255 ASCII
    bytes, and must use Agora's supported account characters. All participants
    in a channel must use the same identity type, with matching tokens.
    """

    channel_name: str
    token: str = field(repr=False)
    uid: Optional[int] = None
    user_account: Optional[str] = None

    def __post_init__(self) -> None:
        _require_nonempty_string("channel_name", self.channel_name)
        _require_nonempty_string("token", self.token)
        if (self.uid is None) == (self.user_account is None):
            raise ValueError("set exactly one of uid or user_account")
        if self.uid is not None:
            if (
                isinstance(self.uid, bool)
                or not isinstance(self.uid, int)
                or not 1 <= self.uid <= 2**32 - 1
            ):
                raise ValueError("uid must be an integer from 1 to 4294967295")
        elif (
            not isinstance(self.user_account, str)
            or len(self.user_account) > 255
            or _AGORA_ACCOUNT.fullmatch(self.user_account) is None
        ):
            raise ValueError(
                "user_account must contain 1 to 255 supported ASCII characters"
            )


TransportConfig = (
    WebSocketTransportConfig | LiveKitTransportConfig | AgoraTransportConfig
)


def _endpoint_domain_for_region(region: str) -> str:
    if region.startswith(CN_REGION_PREFIX):
        return "spatialwalk.top"
    return "spatius.ai"


def _console_endpoint_url_for_region(region: str) -> str:
    return f"https://console.{region}.{_endpoint_domain_for_region(region)}/v1/console"


def _ingress_endpoint_url_for_region(region: str) -> str:
    return f"wss://api.{region}.{_endpoint_domain_for_region(region)}/v3/driveningress"


@dataclass
class SessionConfig:
    """
    Complete configuration for an ``AvatarSession``.

    Most users should create this indirectly with ``new_avatar_session()``. Direct
    construction is still useful for advanced tests or dependency injection. If explicit
    endpoint URLs are omitted, ``region`` composes Spatius production endpoints.

    Attributes:
        avatar_id: The avatar identifier for the session.
        api_key: The API key for authentication.
        app_id: The application identifier.
        use_query_auth: If true, send app/session credentials as URL query params (web-style
            auth). If false (default), send them as headers (mobile-style auth).
        expire_at: Expiration time for the session.
        sample_rate: Audio sample rate in Hz (default: 16000).
        bitrate: Target bitrate for the optional local Ogg Opus encoder. This setting
            is not sent to the service. For PCM this may be 0.
        audio_format: Session audio input format. PCM remains the default for backward
            compatibility. Use OGG_OPUS when streaming one continuous Ogg Opus stream per
            request ID.
        ogg_opus_encoder: Optional client-side encoder settings. When set together with
            ``audio_format=AudioFormat.OGG_OPUS``, ``send_audio()`` accepts raw PCM input
            and the SDK encodes it to continuous Ogg Opus before sending.
        on_encoded_audio: Optional callback invoked when internal Ogg Opus encoding
            finishes for a request. Receives ``(req_id, encoded_audio_bytes)``.
        transport_frames: Callback for receiving serialized V3 Message envelopes (frame_data, is_last).
        on_error: Callback for error handling.
        on_close: Callback invoked when session closes.
        region: Spatius region used to compose endpoint URLs when explicit URLs are not
            provided. Defaults to "auto", which resolves the recommended region via the
            global bootstrap API during ``AvatarSession.init()``. Pass a concrete region
            (e.g. "us-west") to skip resolution, or explicit endpoint URLs to override
            region composition entirely.
        console_endpoint_url: URL for the console API endpoint.
        ingress_endpoint_url: URL for the ingress websocket endpoint.
        transport: Output transport configuration. None returns motion on WebSocket.
            Set LiveKitTransportConfig or AgoraTransportConfig to publish to RTC.
        extra_params: Optional extension parameters sent during the WebSocket session
            handshake.
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
    region: str = DEFAULT_REGION_REQUEST
    console_endpoint_url: str = ""
    ingress_endpoint_url: str = ""
    transport: Optional[TransportConfig] = None
    extra_params: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.audio_format = AudioFormat(self.audio_format)
        if self.transport is not None and not isinstance(
            self.transport,
            (WebSocketTransportConfig, LiveKitTransportConfig, AgoraTransportConfig),
        ):
            raise ValueError(
                "transport must be a supported transport configuration or None"
            )
        self.region = self.region.strip()
        if self._has_concrete_region():
            self.apply_resolved_region(self.region)

    def _has_concrete_region(self) -> bool:
        return bool(self.region) and self.region != DEFAULT_REGION_REQUEST

    def apply_resolved_region(self, region: str) -> None:
        """
        Set a concrete region and compose any missing endpoint URLs from it.

        Explicitly configured endpoint URLs are always preserved. Called by
        ``AvatarSession.init()`` after ``auto`` region resolution, and at
        construction time when a concrete region was given.
        """
        self.region = region.strip()
        if not self.console_endpoint_url:
            self.console_endpoint_url = _console_endpoint_url_for_region(self.region)
        if not self.ingress_endpoint_url:
            self.ingress_endpoint_url = _ingress_endpoint_url_for_region(self.region)


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
    audio_format: AudioFormat = AudioFormat.PCM_S16LE,
    ogg_opus_encoder: Optional[OggOpusEncoderConfig] = None,
    on_encoded_audio: Optional[Callable[[str, bytes], None]] = None,
    transport_frames: Callable[[bytes, bool], None] = _noop_transport_frames,
    on_error: Callable[[Exception], None] = _noop_error,
    on_close: Callable[[], None] = _noop_close,
    region: str = DEFAULT_REGION_REQUEST,
    console_endpoint_url: str = "",
    ingress_endpoint_url: str = "",
    transport: Optional[TransportConfig] = None,
    extra_params: Optional[dict[str, str]] = None,
) -> "AvatarSession":
    """
    Args:
        avatar_id: Avatar identifier to connect to.
        api_key: Console API key used to request a session token.
        app_id: Application identifier used during WebSocket authentication.
        use_query_auth: When true, send ``appId`` and ``sessionKey`` in the WebSocket
            URL query string. When false, send credentials as headers.
        expire_at: UTC expiration time for the session token. Defaults to
            ``DEFAULT_SESSION_TOKEN_TTL`` from now. Naive datetimes are assumed
            to be UTC (consistent with the token cache).
        sample_rate: Input audio sample rate in Hz.
        bitrate: Target bitrate for the optional local Ogg Opus encoder. Not sent
            during session configuration. PCM sessions normally use 0.
        audio_format: Session input format.
        ogg_opus_encoder: Optional internal encoder config for converting PCM input to
            Ogg Opus before upload.
        on_encoded_audio: Callback invoked with ``(req_id, encoded_audio_bytes)`` when
            internal encoding finishes for a request.
        transport_frames: Callback invoked with ``(frame_bytes, is_last)`` for each
            serialized V3 Message envelope containing server_response_motion.
        on_error: Callback invoked for structured SDK/runtime errors.
        on_close: Callback invoked after the session closes.
        region: Region used to compose default endpoint URLs. Defaults to "auto":
            the recommended region is resolved via the global bootstrap API during
            ``init()``. Pass a concrete region to skip resolution.
        console_endpoint_url: Optional explicit Console API URL. Overrides ``region``.
        ingress_endpoint_url: Optional explicit ingress WebSocket URL. Overrides
            ``region``.
        transport: Optional LiveKit, Agora, or WebSocket output configuration. None
            returns motion on the WebSocket.
        extra_params: Optional extension parameters sent during the WebSocket session
            handshake. Keys and values must be strings.

    Returns:
        A configured, uninitialized ``AvatarSession``. Call ``init()`` then ``start()``
        before sending audio.
    """
    from .avatar_session import AvatarSession

    if expire_at is None:
        expire_at = datetime.now(timezone.utc) + DEFAULT_SESSION_TOKEN_TTL
    elif expire_at.tzinfo is None:
        expire_at = expire_at.replace(tzinfo=timezone.utc)

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
        transport=transport,
        extra_params=dict(extra_params or {}),
    )
    return AvatarSession(config)
