"""Round-trip tests for the Auth0 session cookie serialisation."""
from __future__ import annotations

import json
from http.cookiejar import Cookie

import httpx

from pyporscheconnectapi.cookies import _COOKIE_FIELDS, deserialize_cookies, serialize_cookies


def _make_cookie(**overrides) -> Cookie:
    """A realistic Auth0 transaction cookie (session, Secure, HttpOnly)."""
    defaults = {
        "version": 0,
        "name": "auth0",
        "value": "tx-secret-123",
        "port": None,
        "port_specified": False,
        "domain": "identity.porsche.com",
        "domain_specified": True,
        "domain_initial_dot": False,
        "path": "/",
        "path_specified": True,
        "secure": True,
        "expires": None,
        "discard": True,
        "comment": None,
        "comment_url": None,
        "rest": {"HttpOnly": None},
        "rfc2109": False,
    }
    defaults.update(overrides)
    return Cookie(**defaults)


def _jar_with(*cookies: Cookie) -> httpx.Cookies:
    jar = httpx.Cookies()
    for cookie in cookies:
        jar.jar.set_cookie(cookie)
    return jar


def test_round_trip_preserves_every_field_through_json():
    session_cookie = _make_cookie()
    persistent_cookie = _make_cookie(
        name="did",
        value="device-42",
        domain=".porsche.com",
        domain_initial_dot=True,
        expires=4102444800,
        discard=False,
        rest={},
    )
    source = _jar_with(session_cookie, persistent_cookie)

    # The serialised form must survive a real JSON round-trip (that's how
    # callers persist it between processes).
    payload = json.loads(json.dumps(serialize_cookies(source)))

    target = httpx.Cookies()
    deserialize_cookies(target, payload)

    restored = {c.name: c for c in target.jar}
    assert set(restored) == {"auth0", "did"}
    for original in (session_cookie, persistent_cookie):
        clone = restored[original.name]
        for field in _COOKIE_FIELDS:
            assert getattr(clone, field) == getattr(original, field), field
        assert clone._rest == original._rest  # noqa: SLF001


def test_deserialize_is_idempotent():
    source = _jar_with(_make_cookie())
    payload = serialize_cookies(source)

    target = httpx.Cookies()
    deserialize_cookies(target, payload)
    deserialize_cookies(target, payload)

    assert len(list(target.jar)) == 1


def test_restored_session_cookie_is_scoped_to_its_domain():
    """The restored cookie must NOT leak to other hosts (domain='' would)."""
    target = httpx.Cookies()
    deserialize_cookies(target, serialize_cookies(_jar_with(_make_cookie())))

    identity_request = httpx.Request("GET", "https://identity.porsche.com/u/login/identifier")
    target.set_cookie_header(identity_request)
    assert "auth0=tx-secret-123" in identity_request.headers.get("Cookie", "")

    api_request = httpx.Request("GET", "https://api.ppa.porsche.com/app/connect")
    target.set_cookie_header(api_request)
    assert "auth0" not in api_request.headers.get("Cookie", "")


def test_json_float_expires_and_inconsistent_port_are_tolerated():
    payload = [
        {
            "name": "auth0",
            "value": "tx",
            "domain": "identity.porsche.com",
            "domain_specified": True,
            "expires": 4102444800.0,  # JSON numbers may come back as floats
            "port": None,
            "port_specified": True,  # inconsistent flag must not raise
            "rest": None,
        },
    ]
    target = httpx.Cookies()
    deserialize_cookies(target, payload)

    (cookie,) = target.jar
    assert cookie.expires == 4102444800
    assert cookie.port_specified is False
