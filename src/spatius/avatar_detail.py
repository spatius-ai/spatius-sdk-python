"""Avatar metadata used to select supported expressions."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

import aiohttp

from .errors import AvatarSDKError, AvatarSDKErrorCode
from .net import new_connector
from .session_config import SessionConfig

AVATAR_DETAIL_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class AvatarDetail:
    """The avatar's available expression labels and default selection.

    Labels retain the server's exact spelling and order. Details are a snapshot
    cached for the lifetime of the session that fetched them.
    """

    available_emotions: tuple[str, ...]
    default_emotion: str


def _detail_endpoint(config: SessionConfig) -> str:
    parsed = urlparse(config.ingress_endpoint_url)
    schemes = {"ws": "http", "wss": "https", "http": "http", "https": "https"}
    try:
        valid = bool(parsed.hostname) and parsed.username is None
        parsed.port
    except ValueError:
        valid = False
    if parsed.scheme not in schemes or not valid:
        raise ValueError("Invalid ingress endpoint URL")

    path = parsed.path.rstrip("/")
    ingress_path = "/v3/driveningress"
    if not path.endswith(ingress_path):
        raise ValueError("Ingress endpoint must end with /v3/driveningress")
    prefix = path[: -len(ingress_path)]
    query = parse_qs(parsed.query, keep_blank_values=True)
    query.pop("appId", None)
    query.pop("sessionKey", None)
    return urlunparse(
        (
            schemes[parsed.scheme],
            parsed.netloc,
            f"{prefix}/v3/avatar/{quote(config.avatar_id, safe='')}",
            "",
            urlencode(query, doseq=True),
            "",
        )
    )


def _parse_avatar_detail(payload: object) -> AvatarDetail:
    if not isinstance(payload, dict) or payload.get("errors"):
        raise ValueError("Invalid avatar detail response")
    emotions = payload.get("availableEmotions")
    default = payload.get("defaultEmotion")
    if (
        not isinstance(emotions, list)
        or not emotions
        or any(not isinstance(label, str) or not label.strip() for label in emotions)
        or len(set(emotions)) != len(emotions)
        or not isinstance(default, str)
        or default not in emotions
    ):
        raise ValueError("Invalid avatar expression metadata")
    return AvatarDetail(available_emotions=tuple(emotions), default_emotion=default)


async def fetch_avatar_detail(
    config: SessionConfig, session_token: str
) -> AvatarDetail:
    endpoint = _detail_endpoint(config)
    headers = {
        "Accept": "application/json",
        "X-App-ID": config.app_id,
        "X-Session-Key": session_token,
    }

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=AVATAR_DETAIL_TIMEOUT_S),
            connector=new_connector(),
        ) as session:
            async with session.get(
                endpoint, headers=headers, allow_redirects=False
            ) as response:
                if response.status != 200:
                    code = AvatarSDKErrorCode.invalidRequest
                    if response.status == 404:
                        code = AvatarSDKErrorCode.avatarNotFound
                    elif response.status == 401:
                        code = AvatarSDKErrorCode.sessionTokenInvalid
                    elif response.status >= 500:
                        code = AvatarSDKErrorCode.serverError
                    raise AvatarSDKError(
                        code=code,
                        message=f"Failed to fetch avatar detail (HTTP {response.status})",
                        phase="avatar_detail",
                        http_status=response.status,
                    )
                try:
                    payload = await response.json(content_type=None)
                    return _parse_avatar_detail(payload)
                except ValueError:
                    raise AvatarSDKError(
                        code=AvatarSDKErrorCode.protocolError,
                        message="Avatar detail response has invalid expression metadata",
                        phase="avatar_detail",
                    ) from None
    except asyncio.TimeoutError:
        raise AvatarSDKError(
            code=AvatarSDKErrorCode.connectionFailed,
            message="Timed out while fetching avatar detail",
            phase="avatar_detail",
        ) from None
    except aiohttp.ClientError:
        raise AvatarSDKError(
            code=AvatarSDKErrorCode.connectionFailed,
            message="Failed to connect while fetching avatar detail",
            phase="avatar_detail",
        ) from None
