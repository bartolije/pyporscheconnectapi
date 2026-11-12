#  SPDX-License-Identifier: Apache-2.0
"""Python Package for controlling Porsche Connect API."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx

from .const import API_BASE_URL, TIMEOUT, USER_AGENT, X_CLIENT_ID
from .cookies import deserialize_cookies
from .exceptions import PorscheExceptionError
from .oauth2 import Captcha, Credentials, OAuth2Client, OAuth2Token
from .retry import MAX_RETRIES, send_with_retries

if TYPE_CHECKING:
    from .retry import AsyncClient, Response

_LOGGER = logging.getLogger(__name__)

HTTP_UNAUTHORIZED = 401
HTTP_ERROR = 400


class Connection:
    """Handles authentication and connecting to the Porsche Connect API.

    :param email: Porsche Connect email
    :param password: Porsche Connect password
    :param async_client: httpx.AsyncClient or httpx2.AsyncClient (Home Assistant), or None to create one
    :param token: token dict - should be a dict with access_token, refresh_token, expires_at, etc as root params
    :param leeway: time in seconds to consider token as expired before it actually expires
    :param cookies: serialised Auth0 session (PorscheCaptchaRequiredError.cookies)
        to resume a captcha challenge from another process
    """

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        captcha_code: str | None = None,
        state: str | None = None,
        async_client: AsyncClient | None = None,
        token: dict[str, Any] | None = None,
        leeway: int = 60,
        cookies: list[dict] | None = None,
        code_verifier: str | None = None,
    ) -> None:
        """Initialise the connection to the Porsche Connect API."""
        if token is None:
            token = {}
        # Create a client lazily when none is supplied. A module-level default
        # (httpx.AsyncClient()) would be evaluated once at import and shared by
        # every Connection instance, breaking test isolation and CLI reuse.
        self.asyncClient: AsyncClient = async_client if async_client is not None else httpx.AsyncClient()
        if cookies:
            # Restore the Auth0 transaction (PorscheCaptchaRequiredError.cookies)
            # so the captcha retry can resume the Identifier First flow started
            # by another process.
            deserialize_cookies(self.asyncClient.cookies, cookies)
        self.token_lock = asyncio.Lock()

        self.token = OAuth2Token(token)

        self.headers = {"User-Agent": USER_AGENT, "X-Client-ID": X_CLIENT_ID}

        self.oauth2_client = OAuth2Client(
            self.asyncClient,
            Credentials(email, password),
            Captcha(captcha_code, state),
            leeway,
            code_verifier=code_verifier,
        )

    async def get_token(self) -> OAuth2Token:
        """Return the authentication token."""
        async with self.token_lock:
            await self.oauth2_client.ensure_valid_token(self.token)
        return self.token

    async def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """Make a GET request to the Porsche Connect API."""
        return await self.request("GET", url, params=params)

    async def post(self, url: str, data: Any = None, json: Any = None) -> Any:
        """Make a POST request to the Porsche Connect API."""
        return await self.request("POST", url, data=data, json=json)

    async def put(self, url: str, data: Any = None, json: Any = None) -> Any:
        """Make a PUT request to the Porsche Connect API."""
        return await self.request("PUT", url, data=data, json=json)

    async def delete(self, url: str, data: Any = None, json: Any = None) -> Any:
        """Make a DELETE request to the Porsche Connect API."""
        return await self.request("DELETE", url, data=data, json=json)

    async def request(self, method: str, url: str, **kwargs: Any) -> Any:
        """Create a request to the Porsche Connect API.

        Transient errors (429/502/503/504, transport hiccups) on a GET are retried
        by :func:`send_with_retries` - these are server-side issues the
        Porsche API surfaces regularly and that previously caused the whole
        integration to report SETUP_RETRY or mark every entity Unavailable
        (issues #61 and #63). Non-transient HTTP errors (4xx other than
        429) are raised immediately as before.
        """
        async with self.token_lock:
            await self.oauth2_client.ensure_valid_token(self.token)

        sent_token = None

        async def _send() -> Response:
            # Headers rebuilt on every attempt: the access token may have
            # been refreshed by the 401 path below.
            nonlocal sent_token
            sent_token = self.token.access_token
            return await self.asyncClient.request(
                method,
                f"{API_BASE_URL}{url}",
                headers=self.headers | {"Authorization": f"Bearer {sent_token}"},
                timeout=TIMEOUT,
                **kwargs,
            )

        reauthed = False
        while True:
            # Only reads are replayed: a 504 or a timeout does not say whether a
            # command reached the car, and sending it again could run it twice.
            resp = await send_with_retries(_send, description=url, max_retries=MAX_RETRIES if method == "GET" else 0)
            # A 401 means the access token was rejected server-side (revoked,
            # clock skew, ...). Force one re-authentication and retry before
            # giving up - avoids a spurious reauth/captcha prompt in HA for a
            # token a refresh can still recover.
            if resp.status_code == HTTP_UNAUTHORIZED and not reauthed:
                reauthed = True
                _LOGGER.warning("401 on %s - forcing token refresh and retrying once", url)
                async with self.token_lock:
                    # Non-zero past timestamp on purpose: is_expired() treats
                    # 0 as "no expiry info" (→ full re-login), whereas 1
                    # forces the cheaper refresh path first and only
                    # escalates to a full login if the refresh itself fails.
                    # Concurrent requests all get the same 401: only the
                    # first one refreshes, the others reuse its new token.
                    if self.token.access_token == sent_token:
                        self.token["expires_at"] = 1
                    await self.oauth2_client.ensure_valid_token(self.token)
                continue
            # Not raise_for_status(): its exception class depends on the client library.
            if resp.status_code >= HTTP_ERROR:
                raise PorscheExceptionError(resp.status_code, request_url=url)
            if not resp.content:
                return None
            try:
                return resp.json()
            except ValueError as exc:
                # e.g. an HTML maintenance page served with a 200.
                msg = f"Non-JSON response (HTTP {resp.status_code}) on {url}"
                raise PorscheExceptionError(msg, request_url=url) from exc

    async def close(self) -> None:
        """Close the asyncClient connection."""
        await self.asyncClient.aclose()
