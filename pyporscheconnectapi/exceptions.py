#  SPDX-License-Identifier: Apache-2.0
"""Exceptions used for Porsche Connect API."""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


class PorscheExceptionError(Exception):
    """Class of Porsche API exceptions."""

    def __init__(
        self,
        code: int | str | None = None,
        *args: object,
        response_body: str | None = None,
        request_url: str | None = None,
    ) -> None:
        """Initialize exceptions for the Porsche API."""
        self.message = ""
        self.response_body = response_body
        self.request_url = request_url
        super().__init__(*args)
        if code is not None:
            self.code: int | str = code
            if isinstance(code, str):
                self.message = code
                return
            if code == 400:
                self.message = "BAD_REQUEST"
            elif code == 401:
                self.message = "UNAUTHORIZED"
            elif code == 404:
                self.message = "NOT_FOUND"
            elif code == 405:
                self.message = "MOBILE_ACCESS_DISABLED"
            elif code == 408:
                self.message = "VEHICLE_UNAVAILABLE"
            elif code == 423:
                self.message = "ACCOUNT_LOCKED"
            elif code == 429:
                self.message = "TOO_MANY_REQUESTS"
            elif code == 500:
                self.message = "SERVER_ERROR"
            elif code == 503:
                self.message = "SERVICE_MAINTENANCE"
            elif code == 504:
                self.message = "UPSTREAM_TIMEOUT"
            elif code > 299:
                self.message = f"UNKNOWN_ERROR_{code}"

        if self.response_body:
            self.message = f"{self.message}: {self.response_body}"

    def __str__(self) -> str:
        """Return the message: callers format the exception straight into user-facing text."""
        return self.message or super().__str__()


class PorscheWrongCredentialsError(PorscheExceptionError):
    """Class of exceptions for incomplete credentials."""


class PorscheLoginThrottledError(PorscheWrongCredentialsError):
    """Auth0 refused the login because it is throttled or the account is blocked.

    Auth0 returns the same HTTP 400 for a wrong password and for a rate-limited
    or blocked account, so the two are only distinguishable by the wording of
    the returned page. Subclasses PorscheWrongCredentialsError on purpose:
    existing ``except PorscheWrongCredentialsError`` handlers keep catching it,
    while callers that care can catch this first and back off instead of
    prompting the user to re-enter a password that is actually correct.
    """


class PorscheCaptchaRequiredError(PorscheExceptionError):
    """Class of exception when captcha verification is required."""

    captcha: str | None = None
    state: str | None = None
    code_verifier: str | None = None
    cookies: list[dict[str, Any]] | None = None

    def __init__(
        self,
        captcha: str | None = None,
        state: str | None = None,
        code_verifier: str | None = None,
        cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        """Initialize the captcha exception."""
        if captcha is not None and state is not None:
            # Don't log the captcha payload itself — it's a ~14 KB base64
            # data URI and floods INFO-level logs / HA logbook otherwise.
            _LOGGER.debug(
                "Captcha required (state=%s, payload_bytes=%d)",
                state,
                len(captcha),
            )
            self.captcha = captcha
            self.state = state
            self.code_verifier = code_verifier

        # The serialised Auth0 session, so a NEW process can resume the
        # captcha challenge: Connection(..., captcha_code=, state=, cookies=).
        # Session secret: kept OUT of Exception.args (args end up in logs).
        self.cookies = cookies

        super().__init__(captcha, state)
        # Not the ~14 KB captcha data URI that args carries.
        self.message = "CAPTCHA_REQUIRED"


class PorscheRemoteServiceError(PorscheExceptionError):
    """Error when executing remote services."""
