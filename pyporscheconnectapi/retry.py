#  SPDX-License-Identifier: Apache-2.0
"""Shared retry policy for transient HTTP failures.

Used by both the API layer (:mod:`.connection`) and the OAuth2 token
endpoints (:mod:`.oauth2`). Only transient failures are retried here —
mapping HTTP errors to library exceptions (401 re-auth, 403 invalid
refresh token, ...) stays with the callers.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from typing import TYPE_CHECKING

import httpx

from .exceptions import PorscheExceptionError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_LOGGER = logging.getLogger(__name__)

# HTTP status codes that justify a retry (transient server-side issues).
# 429 (rate limit), 502/503/504 (gateway / upstream timeouts) — all surface
# during normal Porsche Connect usage and are the recommended retry targets
# per the upstream maintainer's comments on issues #61 and #63.
RETRY_STATUS_CODES = frozenset({429, 502, 503, 504})
MAX_RETRIES = 3
# Cap a single retry delay so a misbehaving server can't pin a caller for
# minutes on a Retry-After header.
MAX_RETRY_DELAY = 30.0


def compute_retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """Return how many seconds to wait before retrying after a transient error.

    Prefer the server-provided Retry-After header (RFC 9110 §10.2.3) when
    it's a positive integer of seconds — that's what's been served in
    practice by the Porsche API on 429. Otherwise fall back to exponential
    backoff (1s, 2s, 4s) with jitter to spread out concurrent retries.
    """
    retry_after = response.headers.get("retry-after", "") if response is not None else ""
    if retry_after.isdigit():
        return min(float(retry_after), MAX_RETRY_DELAY)
    # secrets.randbelow keeps this deterministic-free without pulling random.
    jitter = secrets.randbelow(300) / 1000.0  # 0-0.3s
    return min((2**attempt) + jitter, MAX_RETRY_DELAY)


async def send_with_retries(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    description: str,
    max_retries: int = MAX_RETRIES,
) -> httpx.Response:
    """Await ``send()`` until it yields a non-transient response.

    Retries 429/502/503/504 and httpx transport errors with backoff. The
    final response is returned WITHOUT raise_for_status — non-transient
    statuses (including a transient one that survived the whole budget)
    are the caller's to interpret. Raises :class:`PorscheExceptionError`
    only when transport errors persist beyond the budget.
    """
    for attempt in range(max_retries + 1):
        try:
            response = await send()
        except httpx.TransportError as exc:
            if attempt == max_retries:
                msg = f"transport error on {description}: {exc}"
                raise PorscheExceptionError(msg) from exc
            delay = compute_retry_delay(None, attempt)
            _LOGGER.warning(
                "Transient transport error on %s (%s) - retrying in %.1fs (attempt %d/%d)",
                description,
                exc.__class__.__name__,
                delay,
                attempt + 1,
                max_retries,
            )
        else:
            if response.status_code not in RETRY_STATUS_CODES or attempt == max_retries:
                return response
            delay = compute_retry_delay(response, attempt)
            _LOGGER.warning(
                "Transient HTTP %s on %s - retrying in %.1fs (attempt %d/%d)",
                response.status_code,
                description,
                delay,
                attempt + 1,
                max_retries,
            )
        await asyncio.sleep(delay)
    msg = f"retry loop exhausted for {description}"  # pragma: no cover - unreachable
    raise PorscheExceptionError(msg)
