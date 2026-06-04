"""OAuth2 flow tests against a respx-mocked Porsche identity server."""
from __future__ import annotations

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
