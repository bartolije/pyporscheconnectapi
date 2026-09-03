#  SPDX-License-Identifier: Apache-2.0
"""Exceptions used for Porsche Connect API."""

from __future__ import annotations

import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)


class PorscheExceptionError(Exception):
    """Class of Porsche API exceptions."""

    def __init__(self, code=None, *args, response_body=None, request_url=None, **kwargs) -> None:
        """Initialize exceptions for the Porsche API."""
        self.message = ""
        self.response_body = response_body
        self.request_url = request_url
        super().__init__(*args, **kwargs)
        if code is not None:
            self.code = code
            if isinstance(code, str):
                self.message = self.code
                return
            if self.code == 400:
                self.message = "BAD_REQUEST"
            elif self.code == 401:
                self.message = "UNAUTHORIZED"
            elif self.code == 404:
                self.message = "NOT_FOUND"
            elif self.code == 405:
                self.message = "MOBILE_ACCESS_DISABLED"
            elif self.code == 408:
                self.message = "VEHICLE_UNAVAILABLE"
            elif self.code == 423:
                self.message = "ACCOUNT_LOCKED"
            elif self.code == 429:
                self.message = "TOO_MANY_REQUESTS"
            elif self.code == 500:
                self.message = "SERVER_ERROR"
            elif self.code == 503:
                self.message = "SERVICE_MAINTENANCE"
            elif self.code == 504:
                self.message = "UPSTREAM_TIMEOUT"
            elif self.code > 299:
                self.message = f"UNKNOWN_ERROR_{self.code}"

        if self.response_body:
            self.message = f"{self.message}: {self.response_body}"


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

    def __init__(self, captcha=None, state=None, code_verifier=None, cookies=None):
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


class PorscheRemoteServiceError(PorscheExceptionError):
    """Error when executing remote services."""
