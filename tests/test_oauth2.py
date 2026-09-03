"""OAuth2 flow tests against a respx-mocked Porsche identity server."""
from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
import respx

from pyporscheconnectapi.connection import Connection
from pyporscheconnectapi.const import (
    AUTHORIZATION_SERVER,
    REDIRECT_URI,
)
from pyporscheconnectapi.exceptions import (
    PorscheCaptchaRequiredError,
    PorscheExceptionError,
    PorscheLoginThrottledError,
    PorscheWrongCredentialsError,
)
from pyporscheconnectapi.oauth2 import Captcha

# A minimal SVG payload — the wire format the lib emits in
# `PorscheCaptchaRequiredError.captcha`.
SAMPLE_SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="150" height="50"/>'
SAMPLE_CAPTCHA_DATA_URI = (
    "data:image/svg+xml;base64," + base64.b64encode(SAMPLE_SVG).decode()
)
SAMPLE_HTML_WITH_CAPTCHA = (
    '<html><body><div class="captcha"><img alt="captcha" '
    f'src="{SAMPLE_CAPTCHA_DATA_URI}"/></div></body></html>'
)

TOKEN_PAYLOAD = {
    "access_token": "fake.access.token",
    "refresh_token": "fake.refresh.token",
    "expires_in": 3600,
    "token_type": "Bearer",
}


def _redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"Location": location})


@pytest.fixture
def routes():
    """Reset respx for every test, intercepting only Porsche identity hosts."""
    with respx.mock(
        base_url=f"https://{AUTHORIZATION_SERVER}", assert_all_called=False,
    ) as router:
        yield router


@pytest.mark.asyncio
async def test_existing_auth0_session_returns_code_directly(
    connection: Connection, routes,
):
    """When /authorize already has a session, no identifier flow runs."""
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?code=AUTHCODE&state=STATE"),
    )
    routes.post("/oauth/token").mock(
        return_value=httpx.Response(200, json=TOKEN_PAYLOAD),
    )

    await connection.get_token()
    assert connection.token["access_token"] == "fake.access.token"

    # identifier-first endpoints must NOT have been called
    assert not any(
        call.request.url.path.startswith("/u/login/")
        for call in routes.calls
    )


@pytest.mark.asyncio
async def test_identifier_first_flow_with_relative_resume_path(
    connection: Connection, routes,
):
    """Full happy path: no session → identifier → password → resume → code."""
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?state=ST"),
    )
    routes.post("/u/login/identifier").mock(
        return_value=httpx.Response(200),
    )
    routes.post("/u/login/password").mock(
        return_value=_redirect("/authorize/resume?state=ST"),
    )
    routes.get("/authorize/resume").mock(
        return_value=_redirect(f"{REDIRECT_URI}?code=AUTHCODE&state=ST"),
    )
    routes.post("/oauth/token").mock(
        return_value=httpx.Response(200, json=TOKEN_PAYLOAD),
    )

    await connection.get_token()
    assert connection.token["refresh_token"] == "fake.refresh.token"


@pytest.mark.asyncio
async def test_identifier_first_flow_with_absolute_resume_url(
    connection: Connection, routes,
):
    """Regression: some accounts get an absolute Location from /u/login/password.

    Previously the f-string concatenation produced
    `https://identity.porsche.comhttps://my.porsche.com/...` and crashed with
    a DNS error. With urljoin(), the absolute URL is followed as-is and the
    `code` parameter is extracted from its redirect.
    """
    absolute_resume = "https://my.porsche.com/?continue=resume&state=ST"
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?state=ST"),
    )
    routes.post("/u/login/identifier").mock(
        return_value=httpx.Response(200),
    )
    routes.post("/u/login/password").mock(
        return_value=_redirect(absolute_resume),
    )
    # respx is base_url-scoped to identity.porsche.com — register the
    # cross-host route via the global router so the redirect is followed.
    with respx.mock(assert_all_called=False) as outer:
        outer.get(absolute_resume).mock(
            return_value=_redirect(f"{REDIRECT_URI}?code=AUTHCODE&state=ST"),
        )
        routes.post("/oauth/token").mock(
            return_value=httpx.Response(200, json=TOKEN_PAYLOAD),
        )
        await connection.get_token()

    assert connection.token["access_token"] == "fake.access.token"


@pytest.mark.asyncio
async def test_captcha_required_raises_with_payload_and_state(
    connection: Connection, routes,
):
    """A 400 on /u/login/identifier with a captcha SVG bubbles up cleanly."""
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?state=ST"),
    )
    routes.post("/u/login/identifier").mock(
        return_value=httpx.Response(400, text=SAMPLE_HTML_WITH_CAPTCHA),
    )

    with pytest.raises(PorscheCaptchaRequiredError) as exc_info:
        await connection.get_token()

    err = exc_info.value
    assert err.state == "ST"
    assert err.captcha == SAMPLE_CAPTCHA_DATA_URI


@pytest.mark.asyncio
async def test_wrong_password_raises(connection: Connection, routes):
    """A 400 on /u/login/password is reported as PorscheWrongCredentialsError."""
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?state=ST"),
    )
    routes.post("/u/login/identifier").mock(
        return_value=httpx.Response(200),
    )
    routes.post("/u/login/password").mock(
        return_value=httpx.Response(400),
    )

    with pytest.raises(PorscheWrongCredentialsError):
        await connection.get_token()


@pytest.mark.asyncio
async def test_wrong_email_raises(connection: Connection, routes):
    """A 401 on /u/login/identifier is reported as PorscheWrongCredentialsError."""
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?state=ST"),
    )
    routes.post("/u/login/identifier").mock(
        return_value=httpx.Response(401),
    )

    with pytest.raises(PorscheWrongCredentialsError):
        await connection.get_token()


@pytest.mark.asyncio
async def test_captcha_retry_completes_login(connection: Connection, routes):
    """After a captcha challenge, setting captcha_code on the same Connection
    and retrying get_token() must complete the flow without re-running /authorize.

    This guards the path the HA integration uses: captch error → user reads code
    → caller mutates oauth2_client.captcha → caller retries.
    """
    # First call triggers the captcha (responds to /authorize then /u/login/identifier).
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?state=ST"),
    )
    identifier_route = routes.post("/u/login/identifier")
    identifier_route.mock(
        side_effect=[
            httpx.Response(400, text=SAMPLE_HTML_WITH_CAPTCHA),  # first call → captcha
            httpx.Response(200),  # retry with captcha_code → OK
        ],
    )
    routes.post("/u/login/password").mock(
        return_value=_redirect("/authorize/resume?state=ST"),
    )
    routes.get("/authorize/resume").mock(
        return_value=_redirect(f"{REDIRECT_URI}?code=AUTHCODE&state=ST"),
    )
    routes.post("/oauth/token").mock(
        return_value=httpx.Response(200, json=TOKEN_PAYLOAD),
    )

    with pytest.raises(PorscheCaptchaRequiredError) as exc_info:
        await connection.get_token()

    state = exc_info.value.state
    connection.oauth2_client.captcha = Captcha(captcha_code="ABC123", state=state)

    await connection.get_token()
    assert connection.token["access_token"] == "fake.access.token"

    # The identifier endpoint was hit twice — once without captcha, once with.
    assert identifier_route.call_count == 2
    last_body = identifier_route.calls[-1].request.content.decode()
    assert "captcha=ABC123" in last_body


@pytest.mark.asyncio
async def test_password_step_without_location_raises(
    connection: Connection, routes,
):
    """A non-redirect password response (e.g. an MFA interstitial) raises a
    clean PorscheExceptionError instead of KeyError-ing on a missing Location.
    """
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?state=ST"),
    )
    routes.post("/u/login/identifier").mock(
        return_value=httpx.Response(200),
    )
    # 200 with no Location header — not the expected redirect to a resume URL.
    routes.post("/u/login/password").mock(
        return_value=httpx.Response(200),
    )

    with pytest.raises(PorscheExceptionError):
        await connection.get_token()


# -- Token endpoint retry (transient failures) ------------------------------


@pytest.fixture
def _instant_retry_sleep(monkeypatch):
    """Make the retry backoff instant for transport-error tests."""
    real_sleep = asyncio.sleep

    async def _instant(_delay):
        await real_sleep(0)

    monkeypatch.setattr("pyporscheconnectapi.retry.asyncio.sleep", _instant)


def _expired_token() -> dict:
    return {
        "access_token": "old.access.token",
        "refresh_token": "old.refresh.token",
        "expires_at": 1,
        "token_type": "Bearer",
    }


@pytest.mark.asyncio
async def test_fetch_access_token_retries_transient_503(
    connection: Connection, routes,
):
    """A transient 503 on the code→token exchange is retried, not fatal."""
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?code=AUTHCODE&state=ST"),
    )
    token_route = routes.post("/oauth/token")
    token_route.mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "0"}),
            httpx.Response(200, json=TOKEN_PAYLOAD),
        ],
    )

    await connection.get_token()

    assert connection.token["access_token"] == "fake.access.token"
    assert token_route.call_count == 2


@pytest.mark.asyncio
async def test_refresh_token_retries_on_429(email: str, password: str, routes):
    """A rate-limited refresh is retried instead of bubbling up an error."""
    token_route = routes.post("/oauth/token")
    token_route.mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json=TOKEN_PAYLOAD),
        ],
    )

    async with httpx.AsyncClient() as client:
        conn = Connection(
            email=email, password=password, async_client=client, token=_expired_token(),
        )
        await conn.get_token()

    assert conn.token["access_token"] == "fake.access.token"
    assert token_route.call_count == 2
    # The refresh path must not have fallen back to a full re-login.
    assert not any(call.request.url.path == "/authorize" for call in routes.calls)


@pytest.mark.asyncio
async def test_fetch_access_token_gives_up_after_retries(
    connection: Connection, routes,
):
    """A persistent 503 on the token endpoint fails after the retry budget."""
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?code=AUTHCODE&state=ST"),
    )
    token_route = routes.post("/oauth/token")
    token_route.mock(
        return_value=httpx.Response(503, headers={"Retry-After": "0"}),
    )

    with pytest.raises(PorscheExceptionError) as exc_info:
        await connection.get_token()

    assert exc_info.value.code == 503
    assert token_route.call_count == 4  # initial + 3 retries


@pytest.mark.asyncio
async def test_refresh_token_403_is_not_retried(email: str, password: str, routes):
    """Regression: a 403 (invalid refresh token) is NOT transient — it must
    trigger the full re-login exactly as before, without extra token POSTs.
    """
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?code=AUTHCODE&state=ST"),
    )
    token_route = routes.post("/oauth/token")
    token_route.mock(
        side_effect=[
            httpx.Response(403),  # refresh rejected → full login
            httpx.Response(200, json=TOKEN_PAYLOAD),  # code→token exchange
        ],
    )

    async with httpx.AsyncClient() as client:
        conn = Connection(
            email=email, password=password, async_client=client, token=_expired_token(),
        )
        await conn.get_token()

    assert conn.token["access_token"] == "fake.access.token"
    assert token_route.call_count == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("_instant_retry_sleep")
async def test_token_endpoint_retries_transport_error(
    connection: Connection, routes,
):
    """A network hiccup on the token endpoint is retried with backoff."""
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?code=AUTHCODE&state=ST"),
    )
    token_route = routes.post("/oauth/token")
    token_route.mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.Response(200, json=TOKEN_PAYLOAD),
        ],
    )

    await connection.get_token()

    assert connection.token["access_token"] == "fake.access.token"
    assert token_route.call_count == 2


# -- Resume polling (replaces the fixed 2.5s settle delay) ------------------


@pytest.mark.asyncio
async def test_resume_polls_until_code_is_ready(
    connection: Connection, routes, monkeypatch,
):
    """Auth0 not ready on the first resume attempts → poll until the 302."""
    monkeypatch.setattr(
        "pyporscheconnectapi.oauth2._RESUME_POLL_DELAYS", (0.0, 0.0, 0.0),
    )
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?state=ST"),
    )
    routes.post("/u/login/identifier").mock(return_value=httpx.Response(200))
    routes.post("/u/login/password").mock(
        return_value=_redirect("/authorize/resume?state=ST"),
    )
    resume_route = routes.get("/authorize/resume")
    resume_route.mock(
        side_effect=[
            httpx.Response(200),  # not ready: no redirect yet
            httpx.Response(200),
            _redirect(f"{REDIRECT_URI}?code=AUTHCODE&state=ST"),
        ],
    )
    routes.post("/oauth/token").mock(
        return_value=httpx.Response(200, json=TOKEN_PAYLOAD),
    )

    await connection.get_token()

    assert connection.token["access_token"] == "fake.access.token"
    assert resume_route.call_count == 3


@pytest.mark.asyncio
async def test_resume_without_code_raises_explicit_error(
    connection: Connection, routes, monkeypatch,
):
    """The resume never yields a code → explicit error, not a silent None."""
    monkeypatch.setattr(
        "pyporscheconnectapi.oauth2._RESUME_POLL_DELAYS", (0.0, 0.0),
    )
    routes.get("/authorize").mock(
        return_value=_redirect(f"{REDIRECT_URI}?state=ST"),
    )
    routes.post("/u/login/identifier").mock(return_value=httpx.Response(200))
    routes.post("/u/login/password").mock(
        return_value=_redirect("/authorize/resume?state=ST"),
    )
    resume_route = routes.get("/authorize/resume")
    # A 302 without a code parameter — the resume "succeeds" but never
    # delivers what we came for.
    resume_route.mock(
        return_value=_redirect(f"{REDIRECT_URI}?state=ST"),
    )

    with pytest.raises(PorscheExceptionError) as exc_info:
        await connection.get_token()

    assert "no authorization code" in exc_info.value.message
    assert resume_route.call_count == 2


# -- Multi-process captcha resume (serialised Auth0 session) ----------------


@pytest.mark.asyncio
async def test_captcha_error_carries_cookies(connection: Connection, routes):
    """The captcha error must embed the Auth0 transaction cookies."""
    routes.get("/authorize").mock(
        return_value=httpx.Response(
            302,
            headers=[
                ("Location", f"{REDIRECT_URI}?state=ST"),
                ("Set-Cookie", "auth0=tx123; Path=/; Secure; HttpOnly"),
            ],
        ),
    )
    routes.post("/u/login/identifier").mock(
        return_value=httpx.Response(400, text=SAMPLE_HTML_WITH_CAPTCHA),
    )

    with pytest.raises(PorscheCaptchaRequiredError) as exc_info:
        await connection.get_token()

    err = exc_info.value
    assert err.cookies is not None
    auth0_cookie = next(c for c in err.cookies if c["name"] == "auth0")
    assert auth0_cookie["value"] == "tx123"
    assert auth0_cookie["domain"] == "identity.porsche.com"
    assert auth0_cookie["secure"] is True
    # The session secret must not leak into Exception.args (→ logs).
    assert all("tx123" not in str(arg) for arg in err.args)


@pytest.mark.asyncio
async def test_captcha_resume_in_fresh_client(email: str, password: str, routes):
    """THE multi-process scenario: process 1 hits the captcha, process 2
    resumes it with a brand-new httpx client seeded from err.cookies/state.
    """
    routes.get("/authorize").mock(
        return_value=httpx.Response(
            302,
            headers=[
                ("Location", f"{REDIRECT_URI}?state=ST"),
                ("Set-Cookie", "auth0=tx123; Path=/; Secure; HttpOnly"),
            ],
        ),
    )
    identifier_route = routes.post("/u/login/identifier")
    identifier_route.mock(
        side_effect=[
            httpx.Response(400, text=SAMPLE_HTML_WITH_CAPTCHA),  # process 1
            httpx.Response(200),  # process 2, captcha code supplied
        ],
    )
    routes.post("/u/login/password").mock(
        return_value=_redirect("/authorize/resume?state=ST"),
    )
    routes.get("/authorize/resume").mock(
        return_value=_redirect(f"{REDIRECT_URI}?code=AUTHCODE&state=ST"),
    )
    routes.post("/oauth/token").mock(
        return_value=httpx.Response(200, json=TOKEN_PAYLOAD),
    )

    # Process 1: the captcha challenge interrupts the login.
    async with httpx.AsyncClient() as client_one:
        conn_one = Connection(email=email, password=password, async_client=client_one)
        with pytest.raises(PorscheCaptchaRequiredError) as exc_info:
            await conn_one.get_token()
    err = exc_info.value

    # Process 2: a FRESH client — only captcha_code/state/cookies carry over.
    async with httpx.AsyncClient() as client_two:
        conn_two = Connection(
            email=email,
            password=password,
            captcha_code="ABC123",
            state=err.state,
            async_client=client_two,
            cookies=err.cookies,
            # PKCE (upstream #96): the verifier minted in process 1 must
            # travel with the captcha state, or the token exchange fails.
            code_verifier=err.code_verifier,
        )
        await conn_two.get_token()

    assert conn_two.token["access_token"] == "fake.access.token"

    # The resumed flow reused the Auth0 transaction: /authorize was hit only
    # once (by process 1), and the identifier POST carried the session cookie.
    authorize_calls = [c for c in routes.calls if c.request.url.path == "/authorize"]
    assert len(authorize_calls) == 1
    assert identifier_route.call_count == 2
    resumed_request = identifier_route.calls[-1].request
    assert "auth0=tx123" in resumed_request.headers.get("Cookie", "")
    assert "captcha=ABC123" in resumed_request.content.decode()


# Auth0 renders the refusal reason into the returned HTML; a wrong password,
# a throttled login and a blocked account all come back as HTTP 400.
WRONG_PASSWORD_HTML = (
    '<html><body><span id="error-element-password">'
    "Wrong email or password.</span></body></html>"
)
THROTTLED_HTML = (
    '<html><body><div role="alert">Your account has been blocked after '
    "multiple consecutive login attempts.</div></body></html>"
)


async def _password_step_response(connection: Connection, routes, html: str):
    """Drive the flow to the password step and return whatever it raises."""
    routes.get("/authorize").mock(return_value=_redirect(f"{REDIRECT_URI}?state=ST"))
    routes.post("/u/login/identifier").mock(return_value=httpx.Response(200))
    routes.post("/u/login/password").mock(
        return_value=httpx.Response(400, text=html),
    )
    with pytest.raises(PorscheWrongCredentialsError) as exc_info:
        await connection.get_token()
    return exc_info.value


@pytest.mark.asyncio
async def test_wrong_password_surfaces_auth0_reason(connection: Connection, routes):
    """A bad password keeps its exception type but now says why."""
    err = await _password_step_response(connection, routes, WRONG_PASSWORD_HTML)
    assert not isinstance(err, PorscheLoginThrottledError)
    assert "Wrong email or password" in err.message


@pytest.mark.asyncio
async def test_throttled_login_is_distinguishable(connection: Connection, routes):
    """A rate-limited login must not read as 'you typed the wrong password'."""
    err = await _password_step_response(connection, routes, THROTTLED_HTML)
    # Still a PorscheWrongCredentialsError, so existing handlers keep working.
    assert isinstance(err, PorscheLoginThrottledError)
    assert "blocked after multiple consecutive login attempts" in err.message


@pytest.mark.asyncio
async def test_unparseable_400_falls_back_to_generic_message(
    connection: Connection, routes,
):
    """No reason in the page → the original generic message, not a crash."""
    err = await _password_step_response(connection, routes, "<html><body/></html>")
    assert not isinstance(err, PorscheLoginThrottledError)
    assert err.message == "Wrong credentials"
