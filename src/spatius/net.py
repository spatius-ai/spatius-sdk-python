"""Shared networking primitives for the Spatius SDK.

A single process-wide TLS context is shared by every HTTP request and
WebSocket connection so OpenSSL can reuse TLS session tickets between them,
and so warm-up connections made before dispatch benefit the real ones.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Optional, Tuple
from urllib.parse import urlparse

import aiohttp

logger = logging.getLogger(__name__)

# Default total timeout for the session-token exchange.
SESSION_TOKEN_TIMEOUT_S = 10.0

# How long DNS answers are cached by connectors created from this module.
DNS_CACHE_TTL_S = 300.0

_ssl_context: Optional[ssl.SSLContext] = None


def get_ssl_context() -> ssl.SSLContext:
    """Return the process-wide client TLS context, creating it on first use.

    Sharing one context lets OpenSSL cache and reuse TLS session tickets
    across connections, including connections made by different libraries
    (aiohttp for HTTP, websockets for the ingress WebSocket) and warm-up
    connections made ahead of time.
    """
    global _ssl_context
    if _ssl_context is None:
        _ssl_context = ssl.create_default_context()
    return _ssl_context


def new_connector() -> aiohttp.TCPConnector:
    """Create an aiohttp connector on the shared TLS context with DNS caching."""
    return aiohttp.TCPConnector(
        ssl=get_ssl_context(),
        use_dns_cache=True,
        ttl_dns_cache=DNS_CACHE_TTL_S,
    )


def host_port_for_url(url: str, *, default_port: int = 443) -> Tuple[str, int]:
    """Extract (host, port) from an http(s):// or ws(s):// endpoint URL."""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.hostname
    if not host:
        raise ValueError(f"cannot determine host from URL: {url!r}")
    return host, parsed.port or default_port


async def warm_tls_connection(url: str, *, timeout: float = 5.0) -> bool:
    """Open and immediately close a bare TLS connection to ``url``'s host.

    Best-effort warm-up: primes the OS resolver cache, the network path to
    the edge, and the shared TLS session cache so the real connection during
    ``AvatarSession.start()`` is cheaper. Never raises; returns whether the
    connection was established.
    """
    try:
        host, port = host_port_for_url(url)
    except ValueError:
        logger.debug("TLS warm-up skipped for unparseable URL %r", url)
        return False

    async def _connect() -> None:
        _, writer = await asyncio.open_connection(
            host, port, ssl=get_ssl_context(), server_hostname=host
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    try:
        await asyncio.wait_for(_connect(), timeout=timeout)
    except Exception as e:
        logger.debug("TLS warm-up to %s:%s failed: %s", host, port, e)
        return False
    logger.debug("warmed TLS connection to %s:%s", host, port)
    return True
