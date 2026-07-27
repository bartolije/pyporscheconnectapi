#  SPDX-License-Identifier: Apache-2.0
"""Serialisation helpers for httpx cookie jars.

The Auth0 "Identifier First" login transaction lives entirely in the
cookies of the httpx client that started it. To resume a captcha
challenge from a different process, callers persist the ``cookies``
attribute of :class:`~.exceptions.PorscheCaptchaRequiredError` (a
JSON-compatible list of dicts) and pass it back to ``Connection``.

These cookies are the Auth0 session secret — treat the serialised form
like a password: never log it, store it with the same care as a token.
"""

from __future__ import annotations

from http.cookiejar import Cookie
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import httpx

# Every constructor field of http.cookiejar.Cookie except ``rest``, which is
# stored in the private ``_rest`` attribute and handled separately. The
# domain/path/secure flags matter: a naive name/value copy would produce
# domain="" cookies that the cookiejar policy sends to EVERY host.
_COOKIE_FIELDS = (
    "version",
    "name",
    "value",
    "port",
    "port_specified",
    "domain",
    "domain_specified",
    "domain_initial_dot",
    "path",
    "path_specified",
    "secure",
    "expires",
    "discard",
    "comment",
    "comment_url",
    "rfc2109",
)


def serialize_cookies(cookies: httpx.Cookies) -> list[dict[str, Any]]:
    """Serialise every cookie in the jar to a JSON-compatible list of dicts."""
    return [
        {
            **{field: getattr(cookie, field) for field in _COOKIE_FIELDS},
            # No public API enumerates non-standard attributes (HttpOnly,
            # SameSite, ...) — they only live in the private ``_rest`` dict.
            "rest": dict(cookie._rest),  # noqa: SLF001
        }
        for cookie in cookies.jar
    ]


def deserialize_cookies(cookies: httpx.Cookies, data: list[dict[str, Any]]) -> None:
    """Restore cookies serialised by :func:`serialize_cookies` into the jar.

    ``jar.set_cookie`` replaces any cookie with the same (domain, path,
    name), so restoring is idempotent. When the target jar belongs to a
    shared client, the restored session is visible to every user of that
    client — intended for the captcha-resume flow.
    """
    for item in data:
        port = item.get("port")
        expires = item.get("expires")
        cookie = Cookie(
            version=item.get("version"),
            name=item["name"],
            value=item["value"],
            port=port,
            # Cookie() raises ValueError when port is None but port_specified is True.
            port_specified=bool(item.get("port_specified")) and port is not None,
            domain=item.get("domain") or "",
            domain_specified=bool(item.get("domain_specified")),
            domain_initial_dot=bool(item.get("domain_initial_dot")),
            path=item.get("path") or "/",
            path_specified=bool(item.get("path_specified", True)),
            secure=bool(item.get("secure")),
            expires=int(expires) if expires is not None else None,
            discard=bool(item.get("discard", True)),
            comment=item.get("comment"),
            comment_url=item.get("comment_url"),
            rest=dict(item.get("rest") or {}),
            rfc2109=bool(item.get("rfc2109")),
        )
        cookies.jar.set_cookie(cookie)
