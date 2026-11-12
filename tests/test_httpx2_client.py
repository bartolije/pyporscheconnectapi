"""The library driven by an httpx2 client, as Home Assistant 2026.11+ does.

httpx2 has the same API as httpx but its own classes: its exceptions do not
inherit from the httpx ones. Every error path must therefore work without
relying on the httpx exception hierarchy.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs

import httpx2
import pytest

from pyporscheconnectapi.connection import Connection
from pyporscheconnectapi.const import REDIRECT_URI
from pyporscheconnectapi.exceptions import PorscheCaptchaRequiredError, PorscheExceptionError

TOKEN = {"access_token": "fresh", "refresh_token": "fresh-r", "expires_in": 3600}


@pytest.fixture(autouse=True)
def _instant_retries(monkeypatch):
    async def _instant(_seconds):
        return None

    monkeypatch.setattr("pyporscheconnectapi.retry.asyncio.sleep", _instant)


def _connection(handler, **token) -> Connection:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    valid = {"access_token": "old", "refresh_token": "old-r", "expires_at": int(time.time()) + 3600}
    return Connection("user@example.com", "s3cret", async_client=client, token=valid | token)


async def test_success_returns_the_json_body():
    conn = _connection(lambda _request: httpx2.Response(200, json={"vin": "V"}))
    assert await conn.get("/x") == {"vin": "V"}
    await conn.close()


@pytest.mark.parametrize(("status", "message"), [(404, "NOT_FOUND"), (500, "SERVER_ERROR")])
async def test_http_error_is_a_library_error(status, message):
    conn = _connection(lambda _request: httpx2.Response(status, json={}))

    with pytest.raises(PorscheExceptionError) as exc_info:
        await conn.get("/x")

    assert (exc_info.value.code, str(exc_info.value)) == (status, message)


async def test_transient_status_is_retried():
    responses = iter([httpx2.Response(503), httpx2.Response(200, json={"ok": True})])
    conn = _connection(lambda _request: next(responses))

    assert await conn.get("/x") == {"ok": True}


async def test_transport_error_is_retried_then_wrapped():
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        msg = "slow"
        raise httpx2.ConnectTimeout(msg)

    conn = _connection(handler)

    with pytest.raises(PorscheExceptionError, match="transport error"):
        await conn.get("/x")
    assert calls == 4  # the first attempt and three retries


async def test_dead_refresh_token_falls_back_to_a_full_login():
    """A 403 on the refresh must reach the login flow, not escape as an httpx2 error."""
    seen: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/oauth/token":
            grant = parse_qs(request.content.decode())["grant_type"][0]
            seen.append(grant)
            return httpx2.Response(403) if grant == "refresh_token" else httpx2.Response(200, json=TOKEN)
        if request.url.path == "/authorize":
            return httpx2.Response(302, headers={"Location": f"{REDIRECT_URI}?code=CODE&state=ST"})
        return httpx2.Response(200, json={"vin": "V"})

    conn = _connection(handler, expires_at=1)

    assert await conn.get("/x") == {"vin": "V"}
    assert seen == ["refresh_token", "authorization_code"]
    assert conn.token.access_token == "fresh"


async def test_login_network_failure_is_a_library_error():
    def handler(_request):
        msg = "down"
        raise httpx2.ConnectError(msg)

    conn = _connection(handler, access_token=None, expires_at=0)

    with pytest.raises(PorscheExceptionError, match="transport error during login"):
        await conn.get_token()


async def test_captcha_cookies_round_trip_between_httpx2_clients():
    """The captcha resume serialises the session cookies of an httpx2 jar too."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/authorize":
            return httpx2.Response(
                302,
                headers={"Location": "/u/login/identifier?state=ST", "Set-Cookie": "auth0=session; Path=/; Secure"},
            )
        return httpx2.Response(400, text='<img alt="captcha" src="data:image/svg+xml;base64,AAA">')

    conn = _connection(handler, access_token=None, expires_at=0)

    with pytest.raises(PorscheCaptchaRequiredError) as exc_info:
        await conn.get_token()

    cookies = exc_info.value.cookies
    assert cookies
    assert cookies[0]["name"] == "auth0"
    resumed = Connection(
        "user@example.com",
        "s3cret",
        async_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        captcha_code="ABC",
        state="ST",
        cookies=cookies,
        code_verifier=exc_info.value.code_verifier,
    )
    assert resumed.asyncClient.cookies.get("auth0") == "session"


async def test_token_endpoint_garbage_is_a_library_error():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, text="<html>maintenance</html>")

    conn = _connection(handler, expires_at=1)

    with pytest.raises(PorscheExceptionError, match="Non-JSON response"):
        await conn.get_token()
