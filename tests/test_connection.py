"""Tests for pyporscheconnectapi.connection.Connection."""
from __future__ import annotations

import asyncio
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
from pyporscheconnectapi.exceptions import PorscheExceptionError

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


# -- Transient-error retry (issues #61 and #63) ----------------------------


@pytest.fixture
def _fast_retries(monkeypatch):
    """Make asyncio.sleep instant so retry-tests don't burn real seconds."""
    real_sleep = asyncio.sleep

    async def _instant(_seconds, *a, **kw):
        await real_sleep(0)

    monkeypatch.setattr("pyporscheconnectapi.connection.asyncio.sleep", _instant)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_retries")
async def test_request_retries_on_504_then_succeeds(
    authed_connection: Connection,
):
    """A 504 Gateway Timeout should be retried, not propagated. Regression
    for upstream issue #61: polling for a remote service status timed out
    and bubbled up as PorscheExceptionError, marking every entity
    Unavailable in HA.
    """
    with respx.mock(base_url=API_BASE_URL, assert_all_called=False) as router:
        route = router.get("/connect/v1/vehicles/V/commands/X").mock(
            side_effect=[
                httpx.Response(504, json={}),
                httpx.Response(504, json={}),
                httpx.Response(200, json={"status": {"result": "PERFORMED"}}),
            ],
        )

        result = await authed_connection.get("/connect/v1/vehicles/V/commands/X")

    assert result == {"status": {"result": "PERFORMED"}}
    assert route.call_count == 3


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_retries")
async def test_request_retries_on_429_then_succeeds(
    authed_connection: Connection,
):
    """A 429 Too Many Requests should be retried. Regression for upstream
    issue #63: every Porsche Connect entity went Unavailable until HA was
    restarted manually.
    """
    with respx.mock(base_url=API_BASE_URL, assert_all_called=False) as router:
        route = router.get("/connect/v1/vehicles/V").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "1"}),
                httpx.Response(200, json={"vin": "V"}),
            ],
        )

        result = await authed_connection.get("/connect/v1/vehicles/V")

    assert result == {"vin": "V"}
    assert route.call_count == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_retries")
async def test_request_does_not_retry_on_400(
    authed_connection: Connection,
):
    """A 400 Bad Request is a client error — raise immediately, don't retry."""
    with respx.mock(base_url=API_BASE_URL, assert_all_called=False) as router:
        route = router.get("/connect/v1/vehicles/V").mock(
            return_value=httpx.Response(400, json={}),
        )

        with pytest.raises(PorscheExceptionError) as exc_info:
            await authed_connection.get("/connect/v1/vehicles/V")

    assert exc_info.value.code == 400
    assert route.call_count == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_retries")
async def test_request_gives_up_after_max_retries(
    authed_connection: Connection,
):
    """If the server keeps returning 504, eventually raise PorscheExceptionError."""
    with respx.mock(base_url=API_BASE_URL, assert_all_called=False) as router:
        route = router.get("/connect/v1/vehicles/V").mock(
            return_value=httpx.Response(504, json={}),
        )

        with pytest.raises(PorscheExceptionError) as exc_info:
            await authed_connection.get("/connect/v1/vehicles/V")

    assert exc_info.value.code == 504
    # 1 initial attempt + 3 retries = 4 calls.
    assert route.call_count == 4


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_retries")
async def test_request_retries_on_transport_error_then_succeeds(
    authed_connection: Connection,
):
    """A transport error (e.g. connection reset) is retried, then succeeds."""
    with respx.mock(base_url=API_BASE_URL, assert_all_called=False) as router:
        route = router.get("/connect/v1/vehicles/V").mock(
            side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={"vin": "V"})],
        )

        result = await authed_connection.get("/connect/v1/vehicles/V")

    assert result == {"vin": "V"}
    assert route.call_count == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_retries")
async def test_request_wraps_persistent_transport_error(
    authed_connection: Connection,
):
    """A persistent transport error is wrapped in PorscheExceptionError after retries."""
    with respx.mock(base_url=API_BASE_URL, assert_all_called=False) as router:
        route = router.get("/connect/v1/vehicles/V").mock(
            side_effect=httpx.ConnectError("down"),
        )

        with pytest.raises(PorscheExceptionError):
            await authed_connection.get("/connect/v1/vehicles/V")

    assert route.call_count == 4


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_retries")
async def test_request_reauths_once_on_401_then_succeeds(
    authed_connection: Connection,
):
    """A 401 forces a single token refresh then retries the request successfully."""
    with respx.mock(assert_all_called=False) as router:
        token_route = router.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=REFRESHED_TOKEN_PAYLOAD),
        )
        api_route = router.get(f"{API_BASE_URL}/connect/v1/vehicles/V").mock(
            side_effect=[httpx.Response(401, json={}), httpx.Response(200, json={"vin": "V"})],
        )

        result = await authed_connection.get("/connect/v1/vehicles/V")

    assert result == {"vin": "V"}
    assert token_route.call_count == 1
    assert api_route.call_count == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("_fast_retries")
async def test_request_raises_after_second_401(
    authed_connection: Connection,
):
    """If a 401 persists after the forced refresh, raise PorscheExceptionError(401)."""
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(
            return_value=httpx.Response(200, json=REFRESHED_TOKEN_PAYLOAD),
        )
        api_route = router.get(f"{API_BASE_URL}/connect/v1/vehicles/V").mock(
            return_value=httpx.Response(401, json={}),
        )

        with pytest.raises(PorscheExceptionError) as exc_info:
            await authed_connection.get("/connect/v1/vehicles/V")

    assert exc_info.value.code == 401
    assert api_route.call_count == 2
