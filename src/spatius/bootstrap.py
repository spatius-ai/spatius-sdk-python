"""Global bootstrap entry: region resolution via the global scheduling API.

A single POST to the global bootstrap endpoint returns both the recommended
ingress region (automatic regional scheduling) and server time-sync fields.
This SDK currently consumes only the region field.

Region resolution semantics (aligned with the web SDK):

- A concrete requested region is used as-is; bootstrap is not called.
- ``auto`` calls bootstrap once and uses ``region.current`` from the response.
- On failure (network error, timeout, non-200, or a malformed response) it
  falls back to the last successfully resolved region cached in this process,
  or to ``DEFAULT_REGION`` when nothing is cached. It never raises, so session
  initialization is never blocked by region scheduling.
"""

from __future__ import annotations

import asyncio
import logging
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Optional

import aiohttp

from .session_config import DEFAULT_REGION, DEFAULT_REGION_REQUEST
from .telemetry import record_http_client_duration

logger = logging.getLogger(__name__)

# Global bootstrap entry (region scheduling + server time sync).
BOOTSTRAP_URL = "https://global.spatialwalk.top/bootstrap"

# Timeout for a single bootstrap request, in seconds.
RESOLVE_TIMEOUT_S = 5.0

# Platform reported in the bootstrap request body.
PLATFORM = "python"

# Last region successfully resolved for "auto", reused as the fallback when a
# later resolution fails. Process-level equivalent of the web SDK's
# localStorage cache.
_cached_region: Optional[str] = None


class BootstrapError(Exception):
    """Raised when the bootstrap request fails or returns an unusable response."""


def _sdk_version() -> str:
    try:
        return version("spatius")
    except PackageNotFoundError:  # pragma: no cover - only when not installed
        return "0+unknown"


async def fetch_bootstrap(
    *,
    app_id: str,
    sdk_version: Optional[str] = None,
    region: str = DEFAULT_REGION_REQUEST,
    platform: str = PLATFORM,
    timeout: float = RESOLVE_TIMEOUT_S,
) -> dict[str, Any]:
    """
    Send one bootstrap request.

    Args:
        app_id: Application identifier.
        sdk_version: SDK version reported to the backend. Defaults to the
            installed ``spatius`` package version.
        region: Requested region hint; ``auto`` asks the backend to schedule.
        platform: Platform identifier reported to the backend.
        timeout: Total request timeout in seconds.

    Returns:
        Parsed response body (``region`` / ``time_sync`` fields, picked as needed).

    Raises:
        BootstrapError: On transport errors, timeouts, or non-200 responses.
    """
    payload = {
        "app_id": app_id,
        "sdk_version": sdk_version or _sdk_version(),
        "region": region,
        "platform": platform,
    }
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    started_at = time.perf_counter()
    status_code: Optional[int] = None
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.post(BOOTSTRAP_URL, json=payload) as response:
                status_code = response.status
                if response.status != 200:
                    raise BootstrapError(f"bootstrap HTTP {response.status}")
                body = await response.json()
    except BootstrapError:
        record_http_client_duration(
            operation="/bootstrap",
            method="POST",
            duration_ms=(time.perf_counter() - started_at) * 1000,
            status_code=status_code,
            server_address="global.spatialwalk.top",
        )
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        record_http_client_duration(
            operation="/bootstrap",
            method="POST",
            duration_ms=(time.perf_counter() - started_at) * 1000,
            server_address="global.spatialwalk.top",
        )
        raise BootstrapError(f"bootstrap request failed: {e}") from e

    record_http_client_duration(
        operation="/bootstrap",
        method="POST",
        duration_ms=(time.perf_counter() - started_at) * 1000,
        status_code=status_code,
        server_address="global.spatialwalk.top",
    )
    if not isinstance(body, dict):
        raise BootstrapError("bootstrap response is not a JSON object")
    return body


async def resolve_region(
    *,
    app_id: str,
    requested_region: str,
    sdk_version: Optional[str] = None,
) -> str:
    """
    Resolve the requested region into a concrete ingress region.

    Args:
        app_id: Application identifier.
        requested_region: User-provided region (``auto`` or a concrete value).
        sdk_version: SDK version reported to the backend.

    Returns:
        A concrete region (never ``auto``). Resolution failures fall back to
        the cached region or ``DEFAULT_REGION`` and never raise.
    """
    global _cached_region

    requested = requested_region.strip()
    if requested and requested != DEFAULT_REGION_REQUEST:
        # User pinned a concrete region - use it directly, no scheduling.
        return requested

    try:
        body = await fetch_bootstrap(
            app_id=app_id,
            sdk_version=sdk_version,
            region=DEFAULT_REGION_REQUEST,
        )
        region = body.get("region")
        current = region.get("current") if isinstance(region, dict) else None
        if isinstance(current, str) and current:
            _cached_region = current
            logger.info("[RegionResolver] auto -> %s", current)
            return current
        raise BootstrapError("bootstrap response missing region.current")
    except Exception as e:
        fallback = _cached_region or DEFAULT_REGION
        logger.warning(
            "[RegionResolver] auto resolve failed, falling back to %s "
            "(from_cache=%s): %s",
            fallback,
            _cached_region is not None,
            e,
        )
        return fallback
