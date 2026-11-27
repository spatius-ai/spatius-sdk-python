"""Main AvatarSession implementation for managing avatar websocket sessions."""

import asyncio
import enum
import inspect
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import aiohttp
import websockets

from .audio_encoder import OggOpusStreamEncoder
from .avatar_detail import AvatarDetail, fetch_avatar_detail
from .bootstrap import resolve_region_with_cache_info
from .errors import AvatarSDKError, AvatarSDKErrorCode, SessionTokenError
from .expression import Expression, validate_expressions
from .logid import generate_log_id
from .net import SESSION_TOKEN_TIMEOUT_S, get_ssl_context, new_connector
from .proto.generated import message_pb2
from .session_config import (
    DEFAULT_REGION,
    DEFAULT_REGION_REQUEST,
    AgoraTransportConfig,
    AudioFormat,
    LiveKitTransportConfig,
    OggOpusEncoderConfig,
    SessionConfig,
    WebSocketTransportConfig,
)
from .token_cache import get_cached_session_token
from .telemetry import (
    add_span_event,
    finish_span,
    inject_trace_context,
    record_http_client_duration,
    record_metric,
    set_resource_context,
    start_span,
    telemetry_enabled,
)

SESSION_TOKEN_PATH = "/session-tokens"
INGRESS_WEBSOCKET_PATH = "/websocket"

# Max time to wait for the server's handshake response (ServerConfirmSession or
# ServerError) after the WebSocket is connected and the configuration is sent.
HANDSHAKE_TIMEOUT_S = 10.0

logger = logging.getLogger(__name__)


class _TurnPhase(enum.Enum):
    """Where the current conversation turn is; mirrors the ingress ordering rules."""

    LISTENING = "listening"  # user_speak_start sent
    AWAITING_EXPRESSION = "awaiting_expression"  # user_speak_end sent
    READY = "ready"  # set_expression sent, no audio yet
    SPEAKING = "speaking"  # audio sent, input not ended
    FINISHED = "finished"  # audio input ended or the turn was interrupted


def _detect_headers_kwarg() -> str:
    """Name of the headers kwarg accepted by the installed websockets version.

    websockets renamed ``extra_headers`` to ``additional_headers`` in 14.0.
    Passing the wrong name forwards it to asyncio's create_connection(), which
    raises "got an unexpected keyword argument". Computed once at import time.
    """
    params = inspect.signature(websockets.connect).parameters
    if "additional_headers" in params:
        return "additional_headers"
    return "extra_headers"


_HEADERS_KWARG = _detect_headers_kwarg()


async def resolve_session_endpoints(
    config: SessionConfig, *, sdk_version: Optional[str] = None
) -> Optional[bool]:
    """
    Resolve an ``auto`` region via the global bootstrap API and compose the
    endpoint URLs from the result.

    Resolution only runs when relying on region-composed endpoints without a
    pinned concrete region. It is skipped entirely when:

    - both endpoint URLs were configured explicitly, or
    - a concrete region was configured (URLs were composed at construction).

    When only some endpoint URLs are explicit, the missing ones are composed
    from ``DEFAULT_REGION`` (preserving the historical default) instead of
    calling bootstrap. Resolution failures never raise: the region falls
    back to the last cached region or ``DEFAULT_REGION``.

    Returns:
        True when an ``auto`` region was served from the fresh process cache,
        False when the bootstrap API was contacted (successfully or not), and
        None when no region resolution was needed.
    """
    if config.console_endpoint_url and config.ingress_endpoint_url:
        # Fully explicit endpoints - nothing to compose.
        return None

    if config._has_concrete_region():
        # Concrete region - compose any URLs still missing (e.g. when the
        # config was mutated after construction).
        config.apply_resolved_region(config.region)
        return None

    if config.console_endpoint_url or config.ingress_endpoint_url:
        # Partially explicit endpoints with an auto region: compose the
        # remaining URLs from the default region instead of scheduling.
        config.apply_resolved_region(DEFAULT_REGION)
        return None

    region, _cache_hit = await resolve_region_with_cache_info(
        app_id=config.app_id,
        requested_region=config.region or DEFAULT_REGION_REQUEST,
        sdk_version=sdk_version,
    )
    config.apply_resolved_region(region)
    return _cache_hit


async def fetch_session_token(
    config: SessionConfig, *, timeout: float = SESSION_TOKEN_TIMEOUT_S
) -> str:
    """
    Exchange configuration credentials for a session token from the console API.

    Used by ``AvatarSession.init()`` and by ``prewarm()`` to fetch a token
    ahead of dispatch. Raises ``SessionTokenError`` on any failure.
    """
    endpoint = config.console_endpoint_url.rstrip("/") + SESSION_TOKEN_PATH

    payload = {"expireAt": int(config.expire_at.timestamp())}

    headers = {
        "X-Api-Key": config.api_key,
        "Content-Type": "application/json",
    }

    request_started_at = time.perf_counter()
    server_address = urlparse(endpoint).hostname
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(
        timeout=client_timeout, connector=new_connector()
    ) as session:
        try:
            async with session.post(
                endpoint, json=payload, headers=headers
            ) as response:
                response_text = await response.text()
        except aiohttp.ClientError as e:
            record_http_client_duration(
                operation=SESSION_TOKEN_PATH,
                method="POST",
                duration_ms=(time.perf_counter() - request_started_at) * 1000,
                server_address=server_address,
            )
            raise SessionTokenError(
                f"Failed to create session token: {e}",
                code=AvatarSDKErrorCode.connectionFailed,
            ) from e
        except asyncio.TimeoutError as e:
            record_http_client_duration(
                operation=SESSION_TOKEN_PATH,
                method="POST",
                duration_ms=(time.perf_counter() - request_started_at) * 1000,
                server_address=server_address,
            )
            raise SessionTokenError(
                "Timed out while creating session token",
                code=AvatarSDKErrorCode.connectionFailed,
            ) from e

        record_http_client_duration(
            operation=SESSION_TOKEN_PATH,
            method="POST",
            duration_ms=(time.perf_counter() - request_started_at) * 1000,
            status_code=response.status,
            server_address=server_address,
        )
        response_data = AvatarSession._try_parse_json(response_text)

        if response.status != 200:
            raise AvatarSession._build_session_token_error(
                response.status,
                payload=response_data,
                raw_body=response_text,
            )

        if not isinstance(response_data, dict):
            raise SessionTokenError(
                "Failed to decode session token response",
                code=AvatarSDKErrorCode.protocolError,
                raw_body=response_text,
            )

        errors = response_data.get("errors", [])
        if errors:
            error_status = AvatarSession._coerce_int(
                AvatarSession._extract_error_details(response_data).get("status")
            )
            raise AvatarSession._build_session_token_error(
                error_status if error_status is not None else response.status,
                payload=response_data,
                raw_body=response_text,
            )

        session_token = response_data.get("sessionToken")
        if not session_token:
            raise SessionTokenError(
                "Empty session token in response",
                code=AvatarSDKErrorCode.protocolError,
                raw_body=response_text,
            )

        return session_token


@dataclass
class _RequestTelemetry:
    span: Any
    started_at: float
    audio_bytes: int = 0
    first_animation_at: Optional[float] = None
    animation_frame_count: int = 0


class AvatarSession:
    """
    Manages one avatar session over a WebSocket connection.

    Typical usage is:

    1. Create a session with ``new_avatar_session()``.
    2. Call ``init()`` to exchange the API key for a short-lived session token.
    3. Call ``start()`` to open the ingress WebSocket and perform the protocol
       handshake.
    4. Call ``send_audio()`` one or more times.
    5. Receive motion messages through the configured ``transport_frames`` callback.
    6. Call ``close()`` when finished.

    The session is also an async context manager; ``async with`` runs ``init()``
    and ``start()`` on entry and ``close()`` on exit::

        async with new_avatar_session(...) as session:
            await session.send_audio(pcm, end=True)

    ``AvatarSession`` instances are stateful and should not be reused after close.
    """

    def __init__(self, config: SessionConfig):
        """
        Initialize a new AvatarSession with the provided configuration.

        Args:
            config: SessionConfig instance with session parameters.
        """
        self._config = config
        self._session_token: Optional[str] = None
        self._avatar_detail: Optional[AvatarDetail] = None
        self._avatar_detail_lock = asyncio.Lock()
        self._connection: Optional[Any] = None
        # Every event and audio packet of a conversation turn shares its req_id.
        self._turn_req_id: Optional[str] = None
        self._turn_phase: Optional[_TurnPhase] = None
        self._audio_encoder: Optional[OggOpusStreamEncoder] = None
        self._read_task: Optional[asyncio.Task] = None
        self._connection_id: Optional[str] = None
        self._request_telemetry: dict[str, _RequestTelemetry] = {}
        self._session_started_at: Optional[float] = None
        self._session_telemetry_finished = False

    @property
    def config(self) -> SessionConfig:
        """Return the session configuration used by this instance."""
        return self._config

    async def __aenter__(self) -> "AvatarSession":
        """Enter the async context: ``init()`` then ``start()`` the session."""
        await self.init()
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Exit the async context: close the session (idempotent)."""
        await self.close()

    async def init(self) -> None:
        """Resolve the region and exchange credentials for a session token."""
        started_at = time.perf_counter()
        region_cache_hit = await self._ensure_region_resolved()
        set_resource_context(
            app_id=self._config.app_id,
            region=self._config.region,
        )
        span = start_span(
            "avatar.session.init",
            self._telemetry_trace_attributes(),
        )
        token_cache_hit: Optional[bool] = None
        try:
            token_cache_hit = await self._init_internal()
        except BaseException as error:
            self._finish_init_telemetry(
                span,
                started_at,
                region_cache_hit=region_cache_hit,
                token_cache_hit=token_cache_hit,
                error=error,
            )
            raise
        else:
            self._finish_init_telemetry(
                span,
                started_at,
                region_cache_hit=region_cache_hit,
                token_cache_hit=token_cache_hit,
                error=None,
            )

    async def get_avatar_detail(self) -> AvatarDetail:
        """Fetch the avatar's expression metadata after ``init()``.

        The first call uses the resolved ingress endpoint and session credentials
        in HTTP headers, regardless of the WebSocket ``use_query_auth`` option.
        Successful metadata is cached on this session; concurrent callers share
        the lookup. This does not open the WebSocket or download avatar assets.
        A failed or cancelled lookup can be retried by calling this method again.
        """
        if not self._session_token:
            raise ValueError("Session not initialized")
        if not self._config.ingress_endpoint_url:
            raise ValueError("Missing ingress endpoint URL")
        if not self._config.avatar_id:
            raise ValueError("Missing avatar ID")
        if not self._config.app_id:
            raise ValueError("Missing app ID")

        async with self._avatar_detail_lock:
            if self._avatar_detail is None:
                self._avatar_detail = await fetch_avatar_detail(
                    self._config, self._session_token
                )
            return self._avatar_detail

    def _finish_init_telemetry(
        self,
        span: Any,
        started_at: float,
        *,
        region_cache_hit: Optional[bool],
        token_cache_hit: Optional[bool],
        error: Optional[BaseException],
    ) -> None:
        span_attributes: dict[str, Any] = {"region": self._config.region}
        metric_attributes = self._telemetry_metric_attributes(success=error is None)
        if region_cache_hit is not None:
            span_attributes["region_cache_hit"] = region_cache_hit
            metric_attributes["region_cache_hit"] = region_cache_hit
        if token_cache_hit is not None:
            span_attributes["token_cache_hit"] = token_cache_hit
            metric_attributes["token_cache_hit"] = token_cache_hit
        record_metric(
            "avatar.session.init.duration",
            (time.perf_counter() - started_at) * 1000,
            metric_attributes,
        )
        finish_span(span, attributes=span_attributes, error=error)

    async def _init_internal(self) -> bool:
        """
        Exchange configuration credentials for a session token from the console
        API, reusing a token prefetched by ``prewarm()`` when one is cached.

        Returns True when the session token came from the warm cache.
        """
        if not self._config.api_key:
            raise ValueError("Missing API key")
        if not self._config.console_endpoint_url:
            raise ValueError("Missing console endpoint URL")
        if not self._config.expire_at:
            raise ValueError("Missing expireAt")

        cached = get_cached_session_token(
            api_key=self._config.api_key,
            app_id=self._config.app_id,
            console_endpoint_url=self._config.console_endpoint_url,
        )
        if cached is not None:
            logger.debug("using prefetched session token")
            self._session_token = cached
            return True

        self._session_token = await fetch_session_token(self._config)
        return False

    async def _ensure_region_resolved(self) -> Optional[bool]:
        """Resolve the ``auto`` region and compose endpoint URLs (see
        ``resolve_session_endpoints``). Returns its cache-hit indicator."""
        return await resolve_session_endpoints(self._config)

    async def start(self) -> str:
        """Open the ingress WebSocket and complete the session handshake."""
        span = start_span(
            "avatar.session.start",
            self._telemetry_trace_attributes(),
        )
        started_at = time.perf_counter()
        try:
            connection_id = await self._start_internal(inject_trace_context(span))
        except BaseException as error:
            record_metric(
                "avatar.session.start.duration",
                (time.perf_counter() - started_at) * 1000,
                self._telemetry_metric_attributes(success=False),
            )
            finish_span(span, error=error)
            raise
        self._session_started_at = time.perf_counter()
        self._session_telemetry_finished = False
        record_metric(
            "avatar.session.start.duration",
            (time.perf_counter() - started_at) * 1000,
            self._telemetry_metric_attributes(success=True),
        )
        finish_span(span, attributes={"connection_id": connection_id})
        return connection_id

    async def _start_internal(
        self, trace_context: Optional[dict[str, str]] = None
    ) -> str:
        """
        Open the ingress WebSocket and complete the session handshake.

        ``start()`` sends the session configuration to the service, waits for a
        ``ServerConfirmSession`` message, and starts the background read loop that
        dispatches callbacks.
        """
        if self._connection is not None:
            raise ValueError("Session already started")
        if not self._session_token:
            raise ValueError("Session not initialized")
        if not self._config.ingress_endpoint_url:
            raise ValueError("Missing ingress endpoint URL")
        if not self._config.avatar_id:
            raise ValueError("Missing avatar ID")
        if not self._config.app_id:
            raise ValueError("Missing app ID")

        endpoint = (
            self._config.ingress_endpoint_url.rstrip("/") + INGRESS_WEBSOCKET_PATH
        )

        # Parse URL and convert to WebSocket scheme
        parsed = urlparse(endpoint)
        scheme = parsed.scheme.lower()

        if scheme == "http":
            ws_scheme = "ws"
        elif scheme == "https":
            ws_scheme = "wss"
        elif scheme in ("ws", "wss"):
            ws_scheme = scheme
        elif not scheme:
            raise ValueError("Ingress endpoint scheme missing")
        else:
            raise ValueError(f"Unsupported scheme: {scheme}")

        # Add avatar ID to query parameters
        query_params = parse_qs(parsed.query)
        query_params["id"] = [self._config.avatar_id]

        # Auth supports headers or query parameters.
        session_key = self._session_token
        headers: dict[str, str] = {}
        if self._config.use_query_auth:
            query_params["appId"] = [self._config.app_id]
            query_params["sessionKey"] = [session_key]
        else:
            headers = {
                "X-App-ID": self._config.app_id,
                "X-Session-Key": session_key,
            }

        new_query = urlencode(query_params, doseq=True)

        ws_url = urlunparse(
            (
                ws_scheme,
                parsed.netloc,
                parsed.path,
                parsed.params,
                new_query,
                parsed.fragment,
            )
        )

        try:
            connect_kwargs: dict[str, Any] = {_HEADERS_KWARG: headers}
            if ws_scheme == "wss":
                # share the process-wide TLS context so session tickets from
                # earlier connections (e.g. prewarm) are reused
                connect_kwargs["ssl"] = get_ssl_context()
            self._connection = await websockets.connect(ws_url, **connect_kwargs)
        except Exception as e:
            raise self._build_websocket_connect_error(e) from e

        # Session handshake:
        # 1) client sends ClientConfigureSession
        # 2) server responds with ServerConfirmSession (connection_id) OR ServerError
        try:
            await self._send_client_configure_session(trace_context)
            server_connection_id = await self._await_server_confirm_session()
        except Exception:
            if self._connection is not None:
                try:
                    await self._connection.close()
                except Exception:
                    pass
                finally:
                    self._connection = None
            raise
        self._connection_id = server_connection_id

        # Start read loop in background
        self._read_task = asyncio.create_task(self._read_loop())

        return server_connection_id

    async def _send_client_configure_session(
        self, trace_context: Optional[dict[str, str]] = None
    ) -> None:
        if self._connection is None:
            raise ValueError("WebSocket connection is not established")

        msg = message_pb2.Message()
        msg.type = message_pb2.MESSAGE_CLIENT_CONFIGURE_SESSION
        config = msg.client_configure_session
        config.audio_sample_rate = int(self._config.sample_rate)
        config.audio_format = self._proto_audio_format(self._config.audio_format)
        config.extra_params.update(self._config.extra_params)
        if trace_context:
            config.trace_context.traceparent = trace_context.get("traceparent", "")
            config.trace_context.tracestate = trace_context.get("tracestate", "")

        transport = self._config.transport
        if isinstance(transport, WebSocketTransportConfig):
            config.transport.websocket.SetInParent()
        elif isinstance(transport, LiveKitTransportConfig):
            livekit = config.transport.livekit
            livekit.url = transport.url
            livekit.token = transport.token
            livekit.idle_timeout = transport.idle_timeout
            livekit.agent_identity = transport.agent_identity
            livekit.extra_attributes.update(transport.extra_attributes)
        elif isinstance(transport, AgoraTransportConfig):
            agora = config.transport.agora
            agora.channel_name = transport.channel_name
            agora.token = transport.token
            agora.uid = transport.uid
        elif transport is not None:
            raise ValueError("Unsupported transport configuration")

        try:
            await self._connection.send(msg.SerializeToString())
        except Exception as e:
            raise self._build_transport_error(
                e,
                phase="websocket_handshake",
                action="send session configuration",
            ) from e

    async def _await_server_confirm_session(self) -> str:
        if self._connection is None:
            raise ValueError("WebSocket connection is not established")

        try:
            raw = await asyncio.wait_for(
                self._connection.recv(), timeout=HANDSHAKE_TIMEOUT_S
            )
        except asyncio.TimeoutError as e:
            raise AvatarSDKError(
                code=AvatarSDKErrorCode.connectionFailed,
                message=(
                    "Failed during websocket handshake: timed out waiting for "
                    f"server response after {HANDSHAKE_TIMEOUT_S}s"
                ),
                phase="websocket_handshake",
            ) from e
        except Exception as e:
            raise self._build_transport_error(
                e,
                phase="websocket_handshake",
                action="receive handshake response",
            ) from e

        if not isinstance(raw, (bytes, bytearray)):
            raise AvatarSDKError(
                code=AvatarSDKErrorCode.protocolError,
                message="Failed during websocket handshake: expected binary protobuf message",
                phase="websocket_handshake",
            )

        envelope = message_pb2.Message()
        try:
            envelope.ParseFromString(bytes(raw))
        except Exception as e:
            raise AvatarSDKError(
                code=AvatarSDKErrorCode.protocolError,
                message=f"Failed during websocket handshake: invalid protobuf payload ({e})",
                phase="websocket_handshake",
            ) from e

        if envelope.type == message_pb2.MESSAGE_SERVER_CONFIRM_SESSION:
            cid = envelope.server_confirm_session.connection_id
            if not cid:
                raise AvatarSDKError(
                    code=AvatarSDKErrorCode.protocolError,
                    message="Handshake succeeded but server_confirm_session.connection_id is empty",
                    phase="websocket_handshake",
                )
            return cid

        if envelope.type == message_pb2.MESSAGE_SERVER_ERROR:
            err = envelope.server_error
            server_code = str(err.code)
            server_detail = err.message or None
            raise AvatarSDKError(
                code=self._classify_error_code(
                    phase="websocket_handshake",
                    server_code=server_code,
                    detail=server_detail,
                ),
                message=self._format_server_error_message(
                    "WebSocket handshake rejected by server",
                    code=server_code,
                    detail=err.message,
                ),
                phase="websocket_handshake",
                connection_id=err.connection_id or None,
                req_id=err.req_id or None,
                server_code=server_code,
                server_detail=server_detail,
            )

        raise AvatarSDKError(
            code=AvatarSDKErrorCode.protocolError,
            message=f"Unexpected message during handshake: type={envelope.type}",
            phase="websocket_handshake",
        )

    def _telemetry_egress_type(self) -> str:
        if isinstance(self._config.transport, LiveKitTransportConfig):
            return "livekit"
        if isinstance(self._config.transport, AgoraTransportConfig):
            return "agora"
        return "websocket"

    def _telemetry_metric_attributes(
        self, *, success: Optional[bool] = None
    ) -> dict[str, Any]:
        attributes: dict[str, Any] = {
            "region": self._config.region,
            "audio_format": self._config.audio_format.value,
            "egress_type": self._telemetry_egress_type(),
        }
        if success is not None:
            attributes["success"] = success
        return attributes

    def _telemetry_trace_attributes(self) -> dict[str, Any]:
        attributes = self._telemetry_metric_attributes()
        attributes.update(
            {
                "app_id": self._config.app_id,
                "avatar_id": self._config.avatar_id,
            }
        )
        if self._connection_id:
            attributes["connection_id"] = self._connection_id
        return attributes

    def _start_request_telemetry(self, req_id: str) -> None:
        if req_id in self._request_telemetry:
            return
        span = start_span(
            "driven.request", {**self._telemetry_trace_attributes(), "req_id": req_id}
        )
        if span is None and not telemetry_enabled():
            return
        self._request_telemetry[req_id] = _RequestTelemetry(
            span=span,
            started_at=time.perf_counter(),
        )

    def _finish_request_telemetry(
        self,
        req_id: Optional[str],
        *,
        end_reason: str,
        error: Optional[BaseException] = None,
    ) -> None:
        if not req_id:
            return
        state = self._request_telemetry.pop(req_id, None)
        if state is None:
            return

        duration_ms = (time.perf_counter() - state.started_at) * 1000
        attributes = self._telemetry_metric_attributes()
        attributes["end_reason"] = end_reason
        record_metric("avatar.request.duration", duration_ms, attributes)
        record_metric("avatar.request.audio_bytes", state.audio_bytes, attributes)
        if state.first_animation_at is not None:
            record_metric(
                "avatar.request.first_animation_latency",
                (state.first_animation_at - state.started_at) * 1000,
                attributes,
            )
        finish_span(
            state.span,
            attributes={
                "req_id": req_id,
                "audio_bytes": state.audio_bytes,
                "animation_frame_count": state.animation_frame_count,
                "end_reason": end_reason,
            },
            error=error,
        )

    def _record_animation_telemetry(self, req_id: str, *, is_last: bool) -> None:
        state = self._request_telemetry.get(req_id)
        if state is None:
            return
        state.animation_frame_count += 1
        if state.first_animation_at is None:
            state.first_animation_at = time.perf_counter()
            add_span_event(state.span, "animation.first_frame")
        if is_last:
            self._finish_request_telemetry(req_id, end_reason="animation_end")

    async def user_speak_start(self) -> str:
        """
        Start a conversation turn: the user began speaking.

        The service interrupts the previous turn's reply, so stop sending its audio
        before calling this.

        Returns:
            The new turn's request ID, shared by its later events and audio.
        """
        req_id = generate_log_id()
        msg = message_pb2.Message()
        msg.type = message_pb2.MESSAGE_CLIENT_USER_SPEAK_START
        msg.client_user_speak_start.req_id = req_id
        await self._send_turn_event(msg, req_id, action="send user speak start")
        self._finish_request_telemetry(self._turn_req_id, end_reason="superseded")
        self._begin_turn(req_id, _TurnPhase.LISTENING)
        return req_id

    async def user_speak_end(self) -> str:
        """
        End the user's speech in the current turn.

        Returns:
            The turn's request ID.

        Raises:
            ValueError: If the current turn did not start with ``user_speak_start()``.
        """
        req_id = self._turn_req_id
        if req_id is None or self._turn_phase is not _TurnPhase.LISTENING:
            raise ValueError("user_speak_end must follow user_speak_start")
        msg = message_pb2.Message()
        msg.type = message_pb2.MESSAGE_CLIENT_USER_SPEAK_END
        msg.client_user_speak_end.req_id = req_id
        await self._send_turn_event(msg, req_id, action="send user speak end")
        self._turn_phase = _TurnPhase.AWAITING_EXPRESSION
        return req_id

    async def set_expression(self, expressions: Sequence[Expression]) -> str:
        """
        Select the expression for the upcoming spoken reply.

        After ``user_speak_end()`` it continues that turn. Otherwise it opens a new
        turn for a reply without user speech, such as a greeting. Each turn takes one
        expression event, before its audio.

        Args:
            expressions: One or more expressions with unique labels. The first one
                drives the reply; the rest are delivered to RTC clients.

        Returns:
            The turn's request ID.

        Raises:
            ValueError: If the list is invalid, the user is still speaking, or the
                current turn already has an expression.
        """
        selected = validate_expressions(expressions)
        if self._turn_phase is _TurnPhase.LISTENING:
            raise ValueError("call user_speak_end before set_expression")
        if self._turn_phase in (_TurnPhase.READY, _TurnPhase.SPEAKING):
            raise ValueError("the current turn already has an expression")
        if self._turn_phase is _TurnPhase.AWAITING_EXPRESSION and self._turn_req_id:
            req_id = self._turn_req_id
        else:
            req_id = generate_log_id()
        msg = message_pb2.Message()
        msg.type = message_pb2.MESSAGE_CLIENT_SET_EXPRESSION
        msg.client_set_expression.req_id = req_id
        for expression in selected:
            msg.client_set_expression.expressions.add(
                label=expression.label, strength=expression.strength
            )
        await self._send_turn_event(msg, req_id, action="send expression")
        if req_id != self._turn_req_id:
            self._begin_turn(req_id, _TurnPhase.READY)
        else:
            self._turn_phase = _TurnPhase.READY
        return req_id

    async def send_audio(self, audio: bytes, end: bool = False) -> str:
        """
        Send one audio chunk of the current turn's spoken reply.

        Call ``set_expression()`` first; every chunk of the reply then shares the
        turn's request ID. Audio bytes must match the session-level ``audio_format``
        negotiated during ``start()``. For ``AudioFormat.OGG_OPUS``, callers can
        either send pre-encoded Ogg Opus bytes directly, or enable
        ``ogg_opus_encoder`` on the session config so ``send_audio()`` accepts raw
        PCM input and the SDK encodes it internally.

        Passing ``end=True`` completes the reply's input; the next reply needs a new
        ``set_expression()``.

        Args:
            audio: Audio bytes to send. May be empty for the final chunk, for both
                PCM and Ogg Opus.
            end: Whether this is the last audio chunk of the reply.

        Returns:
            The turn's request ID.

        Raises:
            ValueError: If the connection is not established, or the current turn
                has no expression yet or has already ended its audio.
        """
        if self._connection is None:
            raise ValueError("WebSocket connection is not established")
        req_id = self._turn_req_id
        if req_id is None or self._turn_phase not in (
            _TurnPhase.READY,
            _TurnPhase.SPEAKING,
        ):
            raise ValueError("call set_expression before sending a reply's audio")

        encoded_stream: Optional[bytes] = None
        payload = audio
        use_internal_encoder = self._uses_internal_ogg_opus_encoder()
        if use_internal_encoder:
            encoder = self._get_or_create_audio_encoder()
            encoded_chunk = encoder.encode(audio, end=end)
            payload = encoded_chunk.payload
            encoded_stream = encoded_chunk.completed_stream

        if use_internal_encoder and not payload and not end:
            return req_id

        self._start_request_telemetry(req_id)

        msg = message_pb2.Message()
        msg.type = message_pb2.MESSAGE_CLIENT_AUDIO_INPUT
        msg.client_audio_input.req_id = req_id
        msg.client_audio_input.audio = payload
        msg.client_audio_input.end = end

        # Serialize and send
        data = msg.SerializeToString()
        try:
            await self._connection.send(data)
        except Exception as e:
            error = self._build_transport_error(
                e,
                phase="websocket_send",
                action="send audio",
                req_id=req_id,
            )
            self._finish_request_telemetry(req_id, end_reason="send_error", error=error)
            raise error from e

        state = self._request_telemetry.get(req_id)
        if state is not None:
            state.audio_bytes += len(payload)
            if end:
                add_span_event(state.span, "audio.input.complete")

        if encoded_stream is not None:
            self._notify_encoded_audio(req_id, encoded_stream)

        self._turn_phase = _TurnPhase.FINISHED if end else _TurnPhase.SPEAKING
        if end:
            self._audio_encoder = None

        return req_id

    def _begin_turn(self, req_id: str, phase: _TurnPhase) -> None:
        self._turn_req_id = req_id
        self._turn_phase = phase
        # an Ogg Opus stream belongs to one reply
        self._audio_encoder = None

    async def _send_turn_event(
        self, msg: message_pb2.Message, req_id: str, *, action: str
    ) -> None:
        if self._connection is None:
            raise ValueError("WebSocket connection is not established")
        try:
            await self._connection.send(msg.SerializeToString())
        except Exception as e:
            raise self._build_transport_error(
                e, phase="websocket_send", action=action, req_id=req_id
            ) from e

    def _uses_internal_ogg_opus_encoder(self) -> bool:
        return (
            self._config.audio_format == AudioFormat.OGG_OPUS
            and self._config.ogg_opus_encoder is not None
        )

    def _get_or_create_audio_encoder(self) -> OggOpusStreamEncoder:
        if self._audio_encoder is None:
            encoder_config = self._config.ogg_opus_encoder or OggOpusEncoderConfig()
            self._audio_encoder = OggOpusStreamEncoder(
                sample_rate=int(self._config.sample_rate),
                bitrate=int(self._config.bitrate),
                frame_duration_ms=int(encoder_config.frame_duration_ms),
                application=str(encoder_config.application),
                collect_encoded_output=self._config.on_encoded_audio is not None,
            )

        return self._audio_encoder

    def _notify_encoded_audio(self, req_id: str, encoded_audio: bytes) -> None:
        callback = self._config.on_encoded_audio
        if callback is None:
            return

        try:
            callback(req_id, encoded_audio)
        except Exception:
            logger.exception("on_encoded_audio callback raised an exception")

    @staticmethod
    def _proto_audio_format(audio_format: str) -> int:
        audio_format = AudioFormat(audio_format)

        if audio_format == AudioFormat.PCM_S16LE:
            return message_pb2.AUDIO_FORMAT_PCM_S16LE

        if audio_format == AudioFormat.OGG_OPUS:
            return message_pb2.AUDIO_FORMAT_OGG_OPUS

        raise ValueError(f"Unsupported audio format: {audio_format}")

    async def interrupt(self) -> str:
        """
        Interrupt the current turn's reply.

        Generation may continue after the client has sent the final audio chunk, so
        this targets the latest turn even after ``send_audio(..., end=True)``.

        Returns:
            The request ID that was interrupted.

        Raises:
            ValueError: If connection is not established or no turn to interrupt.
        """
        if self._connection is None:
            raise ValueError("interrupt: websocket connection is not established")

        req_id = self._turn_req_id
        if not req_id:
            raise ValueError("interrupt: no request to interrupt")

        # Create protobuf message
        msg = message_pb2.Message()
        msg.type = message_pb2.MESSAGE_CLIENT_INTERRUPT
        msg.client_interrupt.req_id = req_id

        # Serialize and send
        data = msg.SerializeToString()
        try:
            await self._connection.send(data)
        except Exception as e:
            error = self._build_transport_error(
                e,
                phase="websocket_send",
                action="send interrupt",
                req_id=req_id,
            )
            self._finish_request_telemetry(
                req_id, end_reason="interrupt_error", error=error
            )
            raise error from e

        self._finish_request_telemetry(req_id, end_reason="interrupted")

        # the next reply needs a new turn
        self._turn_phase = _TurnPhase.FINISHED
        self._audio_encoder = None

        return req_id

    async def close(self) -> None:
        """
        Close the WebSocket connection, cancel the read loop, and run ``on_close``.

        This method is idempotent. Callback exceptions are swallowed so cleanup does not
        fail because of application callback code.
        """
        for req_id in list(self._request_telemetry):
            self._finish_request_telemetry(req_id, end_reason="session_closed")

        if (
            self._session_started_at is not None
            and not self._session_telemetry_finished
        ):
            record_metric(
                "avatar.session.duration",
                (time.perf_counter() - self._session_started_at) * 1000,
                self._telemetry_metric_attributes(success=True),
            )
            self._session_telemetry_finished = True

        if self._connection is not None:
            try:
                await self._connection.close()
            except Exception:
                pass
            finally:
                self._connection = None

        self._audio_encoder = None
        self._turn_req_id = None
        self._turn_phase = None

        if self._read_task is not None:
            # If we're calling close() from inside the read task itself (e.g. the
            # read loop's finally block), don't cancel/await ourselves.
            if asyncio.current_task() is not self._read_task:
                self._read_task.cancel()
                try:
                    await self._read_task
                except asyncio.CancelledError:
                    pass
            self._read_task = None

        # The batch processors export asynchronously. Applications that exit
        # immediately should call ``shutdown_telemetry()`` explicitly; close()
        # must not block session cleanup on an unreachable telemetry endpoint.

        # Call close callback
        if self._config.on_close:
            try:
                self._config.on_close()
            except Exception:
                # Don't let callback errors propagate
                pass

    async def _read_loop(self) -> None:
        """Background task that reads messages from the WebSocket."""
        connection = self._connection
        if connection is None:
            return

        try:
            async for message in connection:
                if isinstance(message, bytes):
                    await self._handle_binary_message(message)
        except websockets.exceptions.ConnectionClosedOK:
            # Normal closure.
            pass
        except websockets.exceptions.ConnectionClosed as e:
            self._notify_error(self._build_connection_closed_error(e))
        except asyncio.CancelledError:
            # Task was cancelled
            raise
        except Exception as e:
            self._notify_error(
                self._coerce_avatar_error(
                    e,
                    code=AvatarSDKErrorCode.connectionFailed,
                    phase="websocket_runtime",
                    message=f"Read loop error: {e}",
                )
            )
        finally:
            # Ensure connection is closed
            await self.close()

    async def _handle_binary_message(self, payload: bytes) -> None:
        """Handle a binary message received from the server."""
        try:
            envelope = message_pb2.Message()
            envelope.ParseFromString(payload)
        except Exception as e:
            self._notify_error(
                AvatarSDKError(
                    code=AvatarSDKErrorCode.protocolError,
                    message=f"Failed to decode message: {e}",
                    phase="websocket_runtime",
                )
            )
            return

        if envelope.type == message_pb2.MESSAGE_SERVER_RESPONSE_MOTION:
            if envelope.WhichOneof("data") != "server_response_motion":
                self._notify_error(
                    AvatarSDKError(
                        code=AvatarSDKErrorCode.protocolError,
                        message="Motion response is missing its payload",
                        phase="websocket_runtime",
                    )
                )
                return
            req_id = envelope.server_response_motion.req_id
            is_last = bool(envelope.server_response_motion.end)
            self._record_animation_telemetry(req_id, is_last=is_last)
            if self._config.transport_frames:
                # payload is already immutable bytes from the read loop; the
                # protobuf parse above does not retain a reference to it.
                try:
                    self._config.transport_frames(payload, is_last)
                except Exception:
                    pass

        elif envelope.type == message_pb2.MESSAGE_SERVER_ERROR:
            err = envelope.server_error
            server_code = str(err.code)
            server_detail = err.message or None
            request_error = AvatarSDKError(
                code=self._classify_error_code(
                    phase="websocket_runtime",
                    server_code=server_code,
                    detail=server_detail,
                ),
                message=self._format_server_error_message(
                    "Avatar session error",
                    code=server_code,
                    detail=err.message,
                ),
                phase="websocket_runtime",
                connection_id=err.connection_id or None,
                req_id=err.req_id or None,
                server_code=server_code,
                server_detail=server_detail,
            )
            self._finish_request_telemetry(
                err.req_id or None,
                end_reason="server_error",
                error=request_error,
            )
            self._notify_error(request_error)

    @staticmethod
    def _try_parse_json(body: str) -> Optional[Any]:
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _coerce_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _stringify(value: Any) -> Optional[str]:
        if value is None:
            return None
        return str(value)

    @classmethod
    def _extract_error_details(cls, payload: Any) -> dict[str, Optional[str]]:
        if isinstance(payload, str):
            parsed = cls._try_parse_json(payload)
            if parsed is None:
                text = payload.strip()
                return {"message": text or None}
            payload = parsed

        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list) and errors:
                first_error = errors[0]
                if isinstance(first_error, dict):
                    return {
                        "status": cls._stringify(first_error.get("status")),
                        "code": cls._stringify(
                            first_error.get("code") or first_error.get("id")
                        ),
                        "title": cls._stringify(first_error.get("title")),
                        "detail": cls._stringify(first_error.get("detail")),
                        "message": cls._stringify(first_error.get("message")),
                    }

            return {
                "status": cls._stringify(payload.get("status")),
                "code": cls._stringify(payload.get("code") or payload.get("error")),
                "title": cls._stringify(payload.get("title")),
                "detail": cls._stringify(payload.get("detail")),
                "message": cls._stringify(payload.get("message")),
            }

        return {}

    @classmethod
    def _compose_error_message(
        cls,
        prefix: str,
        *,
        status: Optional[int],
        details: dict[str, Optional[str]],
    ) -> str:
        message = prefix
        if status is not None:
            message += f" (HTTP {status})"

        detail_parts = [
            part
            for part in (
                details.get("title"),
                details.get("detail") or details.get("message"),
            )
            if part
        ]

        if detail_parts:
            return f"{message}: {' - '.join(detail_parts)}"

        if details.get("code"):
            return f"{message}: {details['code']}"

        return message

    @classmethod
    def _format_server_error_message(
        cls,
        prefix: str,
        *,
        code: Optional[str],
        detail: Optional[str],
    ) -> str:
        if code and detail:
            return f"{prefix}: {detail} (server code {code})"
        if detail:
            return f"{prefix}: {detail}"
        if code:
            return f"{prefix}: server code {code}"
        return prefix

    @staticmethod
    def _normalize_error_text(*values: Optional[str]) -> str:
        parts = [value.strip().lower() for value in values if value and value.strip()]
        return " | ".join(parts)

    @classmethod
    def _classify_error_code(
        cls,
        *,
        phase: str,
        http_status: Optional[int] = None,
        server_code: Optional[str] = None,
        title: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> AvatarSDKErrorCode:
        detail_text = cls._normalize_error_text(server_code, title, detail)

        if server_code in ("3", "INVALID_ARGUMENT"):
            if (
                "livekit" in detail_text
                or "agora" in detail_text
                or "egress" in detail_text
            ):
                return AvatarSDKErrorCode.invalidEgressConfig
            return AvatarSDKErrorCode.invalidRequest
        if server_code in ("16", "UNAUTHENTICATED"):
            return AvatarSDKErrorCode.invalidEgressConfig
        if server_code in ("14", "UNAVAILABLE"):
            return AvatarSDKErrorCode.egressUnavailable

        if server_code == "4001":
            return AvatarSDKErrorCode.creditsExhausted
        if server_code == "4002":
            return AvatarSDKErrorCode.sessionDurationExceeded

        if "credits exhausted" in detail_text:
            return AvatarSDKErrorCode.creditsExhausted
        if (
            "session time limit reached" in detail_text
            or "maximum session duration" in detail_text
        ):
            return AvatarSDKErrorCode.sessionDurationExceeded
        if "session denied" in detail_text or http_status == 402:
            return AvatarSDKErrorCode.billingRequired
        if "invalid session token" in detail_text or "empty token" in detail_text:
            return AvatarSDKErrorCode.sessionTokenInvalid
        if "token is expired" in detail_text or "session token expired" in detail_text:
            return AvatarSDKErrorCode.sessionTokenExpired
        if "app id mismatch" in detail_text:
            return AvatarSDKErrorCode.appIDMismatch
        if "appidunrecognized" in detail_text or "app id unrecognized" in detail_text:
            return AvatarSDKErrorCode.appIDUnrecognized
        if "avatar not found" in detail_text:
            return AvatarSDKErrorCode.avatarNotFound
        if "unsupported sample rate" in detail_text:
            return AvatarSDKErrorCode.unsupportedSampleRate
        if (
            "livekit silence timeout" in detail_text
            or "no audio input for" in detail_text
        ):
            return AvatarSDKErrorCode.idleTimeout
        if "livekit_egress" in detail_text or "agora_egress" in detail_text:
            return AvatarSDKErrorCode.invalidEgressConfig
        if (
            "missing livekit credentials" in detail_text
            or "provide api_token or both api_key and api_secret" in detail_text
            or "unauthorized" in detail_text
        ):
            return AvatarSDKErrorCode.invalidEgressConfig
        if (
            "egress client is not configured on server" in detail_text
            or "failed to create egress connection" in detail_text
        ):
            return AvatarSDKErrorCode.egressUnavailable
        if (
            "driven server returned non-200 status code" in detail_text
            or "driven server request failed" in detail_text
        ):
            return AvatarSDKErrorCode.upstreamError
        if (
            "expected clientconfiguresession message" in detail_text
            or "clientconfiguresession message is nil" in detail_text
            or "unexpected message type" in detail_text
            or "failed to unmarshal initial message" in detail_text
            or "failed during websocket handshake: expected binary protobuf message"
            in detail_text
            or "failed during websocket handshake: invalid protobuf payload"
            in detail_text
        ):
            return AvatarSDKErrorCode.protocolError

        mapped = cls._map_http_status_to_error_code(http_status, phase, detail_text)
        if mapped != AvatarSDKErrorCode.unknown:
            return mapped

        if phase in ("websocket_handshake", "websocket_runtime"):
            return AvatarSDKErrorCode.serverError

        return AvatarSDKErrorCode.unknown

    @staticmethod
    def _map_http_status_to_error_code(
        status: Optional[int], phase: str, detail_text: str = ""
    ) -> AvatarSDKErrorCode:
        if status == 401:
            return AvatarSDKErrorCode.sessionTokenExpired
        if status == 404 and "avatar not found" in detail_text:
            return AvatarSDKErrorCode.avatarNotFound
        if status == 404:
            return AvatarSDKErrorCode.appIDUnrecognized
        if status == 402:
            return AvatarSDKErrorCode.billingRequired
        if status == 400 and "app id mismatch" in detail_text:
            return AvatarSDKErrorCode.appIDMismatch
        if phase == "websocket_connect" and status == 400:
            return AvatarSDKErrorCode.sessionTokenInvalid
        if status is not None and 400 <= status < 500:
            return AvatarSDKErrorCode.invalidRequest
        if status is not None and status >= 500:
            return AvatarSDKErrorCode.serverError
        return AvatarSDKErrorCode.unknown

    @classmethod
    def _build_session_token_error(
        cls,
        status: int,
        *,
        payload: Any,
        raw_body: str,
    ) -> SessionTokenError:
        details = cls._extract_error_details(payload)
        return SessionTokenError(
            cls._compose_error_message(
                "Failed to create session token",
                status=status,
                details=details,
            ),
            code=cls._classify_error_code(
                phase="session_token",
                http_status=status,
                server_code=details.get("code"),
                title=details.get("title"),
                detail=details.get("detail") or details.get("message"),
            ),
            http_status=status,
            server_code=details.get("code"),
            server_title=details.get("title"),
            server_detail=details.get("detail") or details.get("message"),
            raw_body=raw_body,
        )

    @classmethod
    def _build_websocket_connect_error(cls, exc: Exception) -> AvatarSDKError:
        status = cls._coerce_int(cls._extract_http_status(exc))
        raw_body = cls._extract_http_body(exc)
        details = cls._extract_error_details(raw_body)

        if status is not None:
            server_detail = details.get("detail") or details.get("message")
            return AvatarSDKError(
                code=cls._classify_error_code(
                    phase="websocket_connect",
                    http_status=status,
                    server_code=details.get("code"),
                    title=details.get("title"),
                    detail=server_detail,
                ),
                message=cls._compose_error_message(
                    "WebSocket connection rejected",
                    status=status,
                    details=details,
                ),
                phase="websocket_connect",
                http_status=status,
                server_code=details.get("code"),
                server_title=details.get("title"),
                server_detail=server_detail,
                raw_body=raw_body,
            )

        return cls._coerce_avatar_error(
            exc,
            code=AvatarSDKErrorCode.connectionFailed,
            phase="websocket_connect",
            message=f"Failed to connect to websocket: {exc}",
        )

    @classmethod
    def _build_transport_error(
        cls,
        exc: Exception,
        *,
        phase: str,
        action: str,
        req_id: Optional[str] = None,
    ) -> AvatarSDKError:
        if isinstance(exc, websockets.exceptions.ConnectionClosed):
            return cls._build_connection_closed_error(exc, phase=phase, req_id=req_id)

        return cls._coerce_avatar_error(
            exc,
            code=AvatarSDKErrorCode.connectionFailed,
            phase=phase,
            message=f"Failed to {action}: {exc}",
            req_id=req_id,
        )

    @staticmethod
    def _extract_http_status(exc: Exception) -> Optional[int]:
        for attr in ("status_code", "status"):
            status = getattr(exc, attr, None)
            if status is not None:
                try:
                    return int(status)
                except (TypeError, ValueError):
                    pass

        response = getattr(exc, "response", None)
        if response is None:
            return None

        for attr in ("status_code", "status"):
            status = getattr(response, attr, None)
            if status is not None:
                try:
                    return int(status)
                except (TypeError, ValueError):
                    pass
        return None

    @staticmethod
    def _extract_http_body(exc: Exception) -> Optional[str]:
        response = getattr(exc, "response", None)
        if response is None:
            return None

        body = getattr(response, "body", None)
        if body is None:
            return None
        if isinstance(body, (bytes, bytearray)):
            return bytes(body).decode("utf-8", errors="replace")
        return str(body)

    @classmethod
    def _build_connection_closed_error(
        cls,
        exc: Exception,
        *,
        phase: str = "websocket_runtime",
        req_id: Optional[str] = None,
    ) -> AvatarSDKError:
        close_frame = getattr(exc, "rcvd", None) or getattr(exc, "sent", None)
        close_code = getattr(close_frame, "code", None)
        close_reason = getattr(close_frame, "reason", None)

        message = "WebSocket connection closed unexpectedly"
        if close_code is not None and close_reason:
            message += f" (code {close_code}: {close_reason})"
        elif close_code is not None:
            message += f" (code {close_code})"
        elif close_reason:
            message += f" ({close_reason})"

        return AvatarSDKError(
            code=AvatarSDKErrorCode.connectionClosed,
            message=message,
            phase=phase,
            req_id=req_id,
            close_code=close_code,
            close_reason=close_reason,
        )

    @staticmethod
    def _coerce_avatar_error(
        exc: Exception,
        *,
        code: AvatarSDKErrorCode,
        phase: str,
        message: str,
        req_id: Optional[str] = None,
    ) -> AvatarSDKError:
        if isinstance(exc, AvatarSDKError):
            return exc

        return AvatarSDKError(
            code=code,
            message=message,
            phase=phase,
            req_id=req_id,
        )

    def _notify_error(self, error: Exception) -> None:
        try:
            self._config.on_error(error)
        except Exception:
            pass
