"""Process-level cache for prefetched session tokens.

``prewarm()`` can fetch a session token ahead of time so that the next
``AvatarSession.init()`` skips the console API round trip. Entries are keyed
by credentials and endpoint, and are reused until shortly before they expire.

Note: reusing a token assumes the backend allows a session token to back more
than one session over its lifetime. Keep ``prefetch_session_token`` disabled
if your deployment enforces one connection per token.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# A cached token is considered unusable this long before its expiry, so a
# session never starts with a token that is about to lapse.
TOKEN_EXPIRY_MARGIN = timedelta(minutes=1)


@dataclass
class _CachedToken:
    token: str
    expire_at: datetime


_CacheKey = Tuple[str, str, str]  # (api_key, app_id, console_endpoint_url)

_token_cache: Dict[_CacheKey, _CachedToken] = {}


def store_session_token(
    *,
    api_key: str,
    app_id: str,
    console_endpoint_url: str,
    token: str,
    expire_at: datetime,
) -> None:
    """Cache a session token for later ``AvatarSession.init()`` calls."""
    if expire_at.tzinfo is None:
        expire_at = expire_at.replace(tzinfo=timezone.utc)
    key = (api_key, app_id, console_endpoint_url)
    _token_cache[key] = _CachedToken(token=token, expire_at=expire_at)
    logger.debug(
        "cached session token",
        extra={"app_id": app_id, "expire_at": expire_at.isoformat()},
    )


def get_cached_session_token(
    *, api_key: str, app_id: str, console_endpoint_url: str
) -> Optional[str]:
    """Return a fresh-enough cached token, or None if missing/near expiry."""
    key = (api_key, app_id, console_endpoint_url)
    entry = _token_cache.get(key)
    if entry is None:
        return None
    if datetime.now(timezone.utc) >= entry.expire_at - TOKEN_EXPIRY_MARGIN:
        del _token_cache[key]
        return None
    return entry.token


def clear_cached_session_tokens() -> None:
    """Drop all cached tokens (mainly useful in tests)."""
    _token_cache.clear()
