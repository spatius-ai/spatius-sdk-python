"""Process warm-up: move connection setup off the session-start critical path.

``prewarm()`` performs the network work that would otherwise happen inside
``AvatarSession.init()``/``start()`` at dispatch time:

- resolves an ``auto`` region via the bootstrap API (cached process-wide for
  ``REGION_CACHE_TTL_S``, so later ``init()`` calls skip the HTTP round trip),
- optionally opens throwaway TLS connections to the console and ingress hosts
  (priming the DNS resolver, the network path, and the shared TLS session
  cache used by both aiohttp and websockets),
- optionally prefetches a session token so the next ``init()`` skips the
  console API entirely.

Everything is best-effort: failures are logged and reported in the result,
never raised, so warm-up can run safely in worker prewarm hooks.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .avatar_session import fetch_session_token, resolve_session_endpoints
from .bootstrap import RESOLVE_TIMEOUT_S
from .net import host_port_for_url, warm_tls_connection
from .session_config import DEFAULT_REGION_REQUEST, SessionConfig
from .telemetry import finish_span, record_metric, set_resource_context, start_span
from .token_cache import store_session_token

logger = logging.getLogger(__name__)

# Default lifetime for a prefetched session token when none is given.
DEFAULT_PREFETCH_TOKEN_TTL = timedelta(hours=1)


@dataclass
class PrewarmResult:
    """Outcome of a ``prewarm()`` call. Fields report what actually succeeded."""

    region: Optional[str] = None
    """The concrete region sessions will use (None when it could not be resolved)."""
    console_endpoint_url: str = ""
    ingress_endpoint_url: str = ""
    tls_warmed: List[str] = field(default_factory=list)
    """Endpoint hosts a warm-up TLS connection was established to."""
    session_token_prefetched: bool = False
    """Whether a session token was fetched and cached for later init() calls."""


async def prewarm(
    *,
    app_id: str,
    api_key: Optional[str] = None,
    region: str = DEFAULT_REGION_REQUEST,
    console_endpoint_url: str = "",
    ingress_endpoint_url: str = "",
    sdk_version: Optional[str] = None,
    warm_tls: bool = True,
    prefetch_session_token: bool = False,
    session_expire_at: Optional[datetime] = None,
    timeout: float = RESOLVE_TIMEOUT_S,
) -> PrewarmResult:
    """Warm region resolution and connection state ahead of session creation.

    Args:
        app_id: Application identifier.
        api_key: Console API key. Required only when ``prefetch_session_token``
            is true.
        region: Requested region; ``auto`` (the default) resolves and caches
            the recommended region via the bootstrap API.
        console_endpoint_url: Explicit console API URL. Overrides ``region``.
        ingress_endpoint_url: Explicit ingress WebSocket URL. Overrides
            ``region``.
        sdk_version: SDK version reported to the backend.
        warm_tls: Open a throwaway TLS connection to each endpoint host.
        prefetch_session_token: Fetch a session token and cache it so the next
            ``AvatarSession.init()`` with matching credentials skips the
            console API round trip. Assumes the backend allows a token to back
            more than one session; keep disabled if tokens are single-use.
        session_expire_at: Expiration for the prefetched token. Defaults to
            ``DEFAULT_PREFETCH_TOKEN_TTL`` from now.
        timeout: Per-operation timeout in seconds.

    Returns:
        A ``PrewarmResult`` describing what was warmed. Never raises.
    """
    started_at = time.perf_counter()
    span = start_span(
        "spatius.prewarm",
        {
            "app_id": app_id,
            "region": region,
            "warm_tls": warm_tls,
            "prefetch_session_token": prefetch_session_token,
        },
    )
    try:
        result = await _prewarm_impl(
            app_id=app_id,
            api_key=api_key,
            region=region,
            console_endpoint_url=console_endpoint_url,
            ingress_endpoint_url=ingress_endpoint_url,
            sdk_version=sdk_version,
            warm_tls=warm_tls,
            prefetch_session_token=prefetch_session_token,
            session_expire_at=session_expire_at,
            timeout=timeout,
        )
    except BaseException as error:
        record_metric(
            "spatius.prewarm.duration",
            (time.perf_counter() - started_at) * 1000,
            {"success": False},
        )
        finish_span(span, error=error)
        raise

    metric_attributes: dict = {
        "success": True,
        "tls_warmed": len(result.tls_warmed),
        "session_token_prefetched": result.session_token_prefetched,
    }
    span_attributes: dict = {
        "tls_warmed": len(result.tls_warmed),
        "session_token_prefetched": result.session_token_prefetched,
    }
    if result.region is not None:
        metric_attributes["region"] = result.region
        span_attributes["resolved_region"] = result.region
    record_metric(
        "spatius.prewarm.duration",
        (time.perf_counter() - started_at) * 1000,
        metric_attributes,
    )
    finish_span(span, attributes=span_attributes)
    return result


async def _prewarm_impl(
    *,
    app_id: str,
    api_key: Optional[str],
    region: str,
    console_endpoint_url: str,
    ingress_endpoint_url: str,
    sdk_version: Optional[str],
    warm_tls: bool,
    prefetch_session_token: bool,
    session_expire_at: Optional[datetime],
    timeout: float,
) -> PrewarmResult:
    result = PrewarmResult()
    try:
        config = SessionConfig(
            app_id=app_id,
            api_key=api_key or "",
            region=region,
            console_endpoint_url=console_endpoint_url,
            ingress_endpoint_url=ingress_endpoint_url,
        )
        await asyncio.wait_for(
            resolve_session_endpoints(config, sdk_version=sdk_version),
            timeout=timeout,
        )
        result.region = config.region if config._has_concrete_region() else None
        result.console_endpoint_url = config.console_endpoint_url
        result.ingress_endpoint_url = config.ingress_endpoint_url
    except Exception as e:
        logger.warning("prewarm: region resolution failed: %s", e)
        return result

    if result.region is not None:
        set_resource_context(app_id=app_id, region=result.region)

    warmups = []

    if warm_tls:
        for url in (config.console_endpoint_url, config.ingress_endpoint_url):
            if url:
                warmups.append(_warm_one(result, url))

    if prefetch_session_token:
        if not api_key:
            logger.warning("prewarm: prefetch_session_token requires api_key")
        else:
            config.api_key = api_key
            config.expire_at = session_expire_at or (
                datetime.now(timezone.utc) + DEFAULT_PREFETCH_TOKEN_TTL
            )
            warmups.append(_prefetch_token(result, config, timeout=timeout))

    if warmups:
        await asyncio.gather(*warmups)

    return result


async def _warm_one(result: PrewarmResult, url: str) -> None:
    if await warm_tls_connection(url):
        try:
            host, _ = host_port_for_url(url)
        except ValueError:  # pragma: no cover - warm_tls_connection already parsed it
            return
        result.tls_warmed.append(host)


async def _prefetch_token(
    result: PrewarmResult, config: SessionConfig, *, timeout: float
) -> None:
    try:
        token = await fetch_session_token(config, timeout=timeout)
    except Exception as e:
        logger.warning("prewarm: session token prefetch failed: %s", e)
        return
    store_session_token(
        api_key=config.api_key,
        app_id=config.app_id,
        console_endpoint_url=config.console_endpoint_url,
        token=token,
        expire_at=config.expire_at,
    )
    result.session_token_prefetched = True
