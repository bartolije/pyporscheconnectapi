"""Tests for pyporscheconnectapi.connection.Connection."""
from __future__ import annotations

import time

import httpx
import pytest
import respx

from pyporscheconnectapi.connection import Connection
from pyporscheconnectapi.const import (
    API_BASE_URL,
    AUTHORIZATION_SERVER,
    REDIRECT_URI,
    TOKEN_URL,
)

TOKEN_PAYLOAD = {
    "access_token": "fresh.access.token",
    "refresh_token": "fresh.refresh.token",
    "expires_in": 3600,
    "token_type": "Bearer",
}

REFRESHED_TOKEN_PAYLOAD = {
    "access_token": "refreshed.access.token",
    "refresh_token": "refreshed.refresh.token",
    "expires_in": 3600,
    "token_type": "Bearer",
}


def _redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


@pytest.mark.asyncio
async def test_get_token_is_cached_across_calls(connection: Connection):
    """Once authenticated, subsequent get_token() calls return the same token
    without re-hitting the identity server.
    """
    with respx.mock(assert_all_called=False) as router:
        authorize_route = router.get(
            f"https://{AUTHORIZATION_SERVER}/authorize",
        ).mock(return_value=_redirect(f"{REDIRECT_URI}?code=AC&state=ST"))
        token_route = router.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=TOKEN_PAYLOAD),
        )

        first = await connection.get_token()
        second = await connection.get_token()

    assert first is second
    assert first["access_token"] == "fresh.access.token"
    # Only one trip through /authorize + /oauth/token.
    assert authorize_route.call_count == 1
    assert token_route.call_count == 1


@pytest.mark.asyncio
async def test_token_refresh_path_when_expired(email: str, password: str):
    """An expired token triggers a refresh, not a full re-login."""
    expired_token = {
        "access_token": "old.access.token",
        "refresh_token": "old.refresh.token",
        # Expired ~10 minutes ago, well past the 60s leeway.
        "expires_at": int(time.time()) - 600,
        "token_type": "Bearer",
    }

    async with httpx.AsyncClient() as client:
        conn = Connection(
            email=email, password=password, async_client=client, token=expired_token,
        )
        try:
            with respx.mock(assert_all_called=False) as router:
                authorize_route = router.get(
                    f"https://{AUTHORIZATION_SERVER}/authorize",
                )
                token_route = router.post(TOKEN_URL).mock(
                    return_value=httpx.Response(200, json=REFRESHED_TOKEN_PAYLOAD),
                )

                await conn.get_token()

            assert conn.token["access_token"] == "refreshed.access.token"
            # The refresh path uses /oauth/token only — /authorize must NOT be hit.
            assert authorize_route.call_count == 0
            assert token_route.call_count == 1
        finally:
            await conn.close()


@pytest.mark.asyncio
async def test_close_is_idempotent(email: str, password: str):
    """Calling Connection.close() twice doesn't raise."""
    async with httpx.AsyncClient() as client:
        conn = Connection(email=email, password=password, async_client=client)
        await conn.close()
        # Second close must not raise.
        await conn.close()


@pytest.mark.asyncio
async def test_request_attaches_bearer_authorization_header(
    authed_connection: Connection,
):
    """Every API request carries `Authorization: Bearer <access_token>`."""
    with respx.mock(base_url=API_BASE_URL, assert_all_called=False) as router:
        route = router.get("/connect/v1/vehicles").mock(
            return_value=httpx.Response(200, json=[]),
        )

        await authed_connection.get("/connect/v1/vehicles")

    auth_header = route.calls.last.request.headers.get("Authorization")
    assert auth_header == "Bearer test.access.token"
