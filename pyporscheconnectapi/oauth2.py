"""Authentication token management for Porsche Connect API."""

#  SPDX-License-Identifier: Apache-2.0
import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
import secrets
import time
from functools import partial
from typing import NamedTuple
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from .const import (
    AUDIENCE,
    AUTHORIZATION_SERVER,
    AUTHORIZATION_URL,
    CLIENT_ID,
    REDIRECT_URI,
    SCOPE,
    TIMEOUT,
    TOKEN_URL,
    USER_AGENT,
    X_CLIENT_ID,
)
from .cookies import serialize_cookies
from .exceptions import (
    PorscheCaptchaRequiredError,
    PorscheExceptionError,
    PorscheLoginThrottledError,
    PorscheWrongCredentialsError,
)
from .retry import send_with_retries

_LOGGER = logging.getLogger(__name__)

# Wording Auth0 uses when it refuses a login for reasons other than a bad
# password: rate limiting, brute-force protection, or a blocked account.
_THROTTLE_PATTERN = re.compile(
    r"too many|blocked|rate.?limit|try again later|suspicious|temporarily",
    re.IGNORECASE,
)

# Auth0 needs a brief settle time after the password POST before the resume
# endpoint will mint the authorization code. Poll immediately, then back off —
# a fixed sleep penalised every login even when Auth0 was already ready.
_RESUME_POLL_DELAYS = (0.0, 0.5, 1.0, 2.0, 2.5)
# Auth0 screen that may be interleaved into the resume redirect chain
PASSKEY_ENROLLMENT_PATH = "/u/passkey-enrollment"
_MAX_RESUME_REDIRECTS = 10


class Credentials(NamedTuple):
    """Store credentials for the Porsche Connect API."""

    email: str
    password: str


class Captcha(NamedTuple):
    """Store captcha data for the Porsche Connect API."""

    captcha_code: str
    state: str


class OAuth2Token(dict):
    """A simple wrapper around a dict to handle OAuth2 tokens.

    Provides a helper method to check if the token is expired.
    Originally based on: https://github.com/lepture/authlib/blob/master/authlib/oauth2/rfc6749/wrappers.py
    """

    def __init__(self, params: dict):
        """Initialise the oauth2 token."""
        if params.get("expires_at"):
            self["expires_at"] = int(params["expires_at"])
        elif params.get("expires_in"):
            self.expires_at = params["expires_in"]
        super().__init__(params)

    def is_expired(self, leeway=60):
        """Return true if the access token has expired."""
        expires_at = self.get("expires_at")
        if not expires_at:
            return None
        # small timedelta to consider token as expired before it actually expires
        expiration_threshold = expires_at - leeway
        return expiration_threshold < time.time()

    @property
    def expires_at(self):
        """Return the expiration time stamp of the access token."""
        return self.get("expires_at")

    @property
    def access_token(self):
        """Return the access token."""
        return self.get("access_token")

    @property
    def refresh_token(self):
        """Return the refresh token."""
        return self.get("refresh_token")

    @expires_at.setter
    def expires_at(self, expires_in):
        self["expires_at"] = int(time.time()) + int(expires_in)


class OAuth2Client:
    """Utility class to handle OAuth2 authentication with Porsche Connect.

    :param client: httpx.AsyncClient
    :param credentials: tuple of email, password
    :param leeway: time in seconds to consider token as expired before it actually expires
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        credentials: Credentials,
        captcha: Captcha,
        leeway: int = 60,
        *,
        code_verifier: str | None = None,
    ):
        """Initialise the oauth2 client."""
        self.client = client
        self.credentials = credentials
        self.captcha = captcha
        self.leeway = leeway
        self.headers = {"User-Agent": USER_AGENT, "X-Client-ID": X_CLIENT_ID}
        self.code_verifier: str | None = code_verifier

    def _generate_pkce_verifier(self) -> str:
        """Generate a PKCE code verifier (RFC 7636 section 4.1)."""
        return secrets.token_urlsafe(64)

    def _build_pkce_challenge(self, verifier: str) -> str:
        """Derive the S256 code challenge from a verifier (RFC 7636 section 4.2)."""
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    async def ensure_valid_token(self, token: OAuth2Token):
        """Ensure the access_token is valid, logging in or refreshing if necessary."""
        token_is_expired = token.is_expired(self.leeway)
        if token_is_expired:
            token_data = await self.refresh_token(token.refresh_token)
            token.update(token_data)
            token.expires_at = token_data["expires_in"]
            _LOGGER.debug("Refreshed Access Token: %s", token.access_token)
        if token.access_token is None or token_is_expired is None:  # no token, get a new one
            auth_code = await self.fetch_authorization_code()
            token_data = await self.fetch_access_token(auth_code)
            token.update(token_data)
            token.expires_at = token_data["expires_in"]
            _LOGGER.debug("New Access Token: %s", token.access_token)

    async def fetch_authorization_code(self):
        """Fetch the authorization code from Porsche Connect.

        Requires 1-4 requests (1 if already logged in, 4 if not):

        1. Initial request to /authorize to get the code
        2. If no code is returned, login with Identifier First flow:
            2a. POST to /u/login/identifier with email
            2b. POST to /u/login/password with password
        3. Resume the /authorize request with the resume path from the Identifier First flow

        :return: authorization code to be exchanged for an access token
        """
        try:
            # When retrying after a captcha challenge, the caller has already
            # been through /authorize once (the state is carried in
            # self.captcha.state). Skip that round-trip and resume the
            # Identifier First flow directly.
            if self.captcha.captcha_code is not None:
                # The verifier is minted on the first /authorize call; resuming
                # after a captcha in a NEW process must carry it across
                # (Connection(..., code_verifier=)) or the token exchange fails.
                if self.code_verifier is None:
                    msg = "PKCE_VERIFIER_MISSING_FOR_CAPTCHA_RESUME"
                    raise PorscheExceptionError(msg)
                state = self.captcha.state
            else:
                _LOGGER.debug("Fetching authorization code.")

                # first request to get the code
                self.code_verifier = self._generate_pkce_verifier()
                params = await self.get_and_extract_location_params(
                    AUTHORIZATION_URL,
                    params={
                        "response_type": "code",
                        "client_id": CLIENT_ID,
                        "redirect_uri": REDIRECT_URI,
                        "audience": AUDIENCE,
                        "scope": SCOPE,
                        "code_challenge": self._build_pkce_challenge(self.code_verifier),
                        "code_challenge_method": "S256",
                        # Anti-CSRF token, regenerated per request.
                        # RFC 6749 §10.12 recommends a non-guessable value.
                        "state": secrets.token_urlsafe(16),
                    },
                )
                # If Auth0 already has a session, /authorize returns the code
                # directly — no identifier flow needed.
                if (code := params.get("code", [None])[0]) is not None:
                    _LOGGER.debug("Got authorization code from existing session.")
                    return code
                _LOGGER.debug(
                    "No existing auth0 session, running through identifier first flow.",
                )
                state = params["state"][0]

            resume_path = await self.login_with_identifier(state)
            authorization_code = await self._poll_resume_for_code(
                urljoin(f"https://{AUTHORIZATION_SERVER}", resume_path),
            )

        except httpx.HTTPStatusError as exc:
            raise PorscheExceptionError(exc.response.status_code) from exc

        _LOGGER.debug("Authorization code: %s", authorization_code)
        return authorization_code

    async def _poll_resume_for_code(self, resume_url: str) -> str:
        """Poll the resume endpoint until Auth0 mints the authorization code.

        Ends the wait as soon as the code is available instead of always
        paying a fixed settle delay, and surfaces an explicit error when
        the code never shows up (previously a silent None that blew up
        later in the code→token exchange with a misleading message).
        """
        last_error: PorscheExceptionError | None = None
        for attempt, delay in enumerate(_RESUME_POLL_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            try:
                # Delegates to the redirect-chain walker so the passkey
                # enrolment interstitial (upstream #96) is dismissed on
                # every attempt, not just the first.
                return await self.resume_authorization_code_flow(resume_url)
            except PorscheExceptionError as exc:  # Auth0 not ready yet
                last_error = exc
                _LOGGER.debug("Resume attempt %d returned no authorization code yet.", attempt + 1)
        msg = f"Auth0 resume returned no authorization code after {len(_RESUME_POLL_DELAYS)} attempts"
        raise PorscheExceptionError(msg) from last_error

    async def get_and_extract_location_params(self, url, params=None):
        """GET the URL and extract the params from the Location header.

        :param url: URL to GET
        :param params: dict of query parameters
        :return: dict of query parameters from the Location header
        """
        if params is None:
            params = {}
        resp = await self.client.get(
            url,
            params=self._merge_query_params(url, params),
            timeout=TIMEOUT,
            headers=self.headers,
        )
        if resp.status_code != 302:
            msg = "Could not fetch authorization code"
            raise PorscheExceptionError(msg)

        location = resp.headers["Location"]
        return self._extract_params_from_url(location)

    def _extract_params_from_url(self, url):
        """Extract the query parameters from a URL.

        :param url: URL to extract the query parameters from
        :return: dict of query parameters
        """
        return parse_qs(urlparse(url).query)

    def _merge_query_params(self, url: str, params: dict[str, str]) -> dict[str, str]:
        """Merge query parameters into a new dictionary with the existing query parameters of a URL."""
        parsed_url = urlparse(url)
        query = parse_qs(parsed_url.query)
        new_query = {k: v[0] for k, v in query.items()}
        new_query.update(params)
        return new_query

    def _extract_auth0_error(self, html: str) -> str | None:
        """Pull the human-readable failure reason out of an Auth0 error page.

        Auth0's Universal Login answers a wrong password, a throttled login
        and a blocked account all with HTTP 400 and an HTML body -- there is
        no JSON error code to switch on. The reason is rendered into the page,
        so surfacing it is the only way for a caller to tell "you typed the
        wrong password" apart from "you are being rate limited".
        """
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:  # noqa: BLE001 - a parser failure must never mask the auth error
            return None
        candidates = (
            soup.find(attrs={"role": "alert"}),
            soup.find(class_=re.compile(r"ulp-error|error-info|error-message")),
            soup.find(id=re.compile(r"^error-element-")),
        )
        for element in candidates:
            if element is not None and (text := element.get_text(" ", strip=True)):
                return text[:200]
        return None

    def _extract_captcha_image(self, html: str):
        """Extract the captcha image from Auth0 ACUL or legacy login HTML."""
        script_match = re.search(r'atob\("([A-Za-z0-9+/=]+)"', html)
        if script_match:
            try:
                decoded_json = base64.b64decode(script_match.group(1)).decode("utf-8")
                context_data = json.loads(decoded_json)
            except (ValueError, json.JSONDecodeError, binascii.Error) as exc:
                _LOGGER.warning("Failed to parse Auth0 ACUL context: %s", exc)
            else:
                captcha_img = context_data.get("screen", {}).get("captcha", {}).get("image")
                if captcha_img:
                    _LOGGER.debug(
                        "Parsed captcha from Auth0 ACUL context (length: %d)",
                        len(captcha_img),
                    )
                    return captcha_img

        soup = BeautifulSoup(html, "html.parser")
        img_tag = soup.find("img", {"alt": "captcha"})
        if img_tag:
            return img_tag.get("src")

        svg_match = re.search(r"(data:image/svg[^ ]+)", html)
        if svg_match:
            return svg_match.group(1)

        return None

    def _extract_universal_login_context(self, html: str) -> dict | None:
        """Extract the Auth0 universal login context from the inline base64 payload."""
        match = re.search(r'atob\("([A-Za-z0-9+/=]+)"', html)
        if not match:
            return None

        try:
            decoded = base64.b64decode(match.group(1)).decode("utf-8")
            return json.loads(decoded)
        except (ValueError, json.JSONDecodeError, binascii.Error) as exc:
            _LOGGER.warning("Failed to parse Auth0 universal login context: %s", exc)
            return None

    async def _skip_passkey_enrollment(self, url: str, html: str | None = None) -> str:
        """Decline the optional passkey enrollment screen and return where to continue."""
        if html is None:
            resp = await self.client.get(
                url,
                timeout=TIMEOUT,
                headers=self.headers,
                follow_redirects=False,
            )
            resp.raise_for_status()
            html = resp.text

        context = self._extract_universal_login_context(html)
        if context is None:
            msg = "PASSKEY_ENROLLMENT_CONTEXT_MISSING"
            raise PorscheExceptionError(msg)

        transaction_state = context.get("transaction", {}).get("state")
        if not transaction_state:
            msg = "PASSKEY_ENROLLMENT_STATE_MISSING"
            raise PorscheExceptionError(msg)

        data = dict(context.get("untrustedData", {}).get("submittedFormData") or {})
        data.update(
            {
                "state": transaction_state,
                "action": "abort-passkey-enrollment",
                "acul-sdk": "@auth0/auth0-acul-js@1.2.0",
            },
        )

        _LOGGER.debug("Declining passkey enrollment.")
        resp = await self.client.post(
            url,
            data=data,
            timeout=TIMEOUT,
            headers=self.headers,
            follow_redirects=False,
        )
        if resp.status_code not in (302, 303):
            msg = "PASSKEY_ENROLLMENT_SKIP_FAILED"
            raise PorscheExceptionError(msg)

        return urljoin(url, resp.headers["Location"])

    async def resume_authorization_code_flow(self, url: str) -> str:
        """Follow the Auth0 redirect chain until the authorization code is returned.

        :param url: resume URL returned by the Identifier First flow
        :return: authorization code to be exchanged for an access token
        """
        current_url = url

        for _ in range(_MAX_RESUME_REDIRECTS):
            code = parse_qs(urlparse(current_url).query).get("code", [None])[0]
            if code is not None:
                return code

            # Reaching the callback URI without a code means Auth0 has not
            # minted it yet. Stop here rather than GETting a custom-scheme
            # URL that no transport can serve; the caller's poll retries.
            if current_url.startswith(REDIRECT_URI):
                msg = "Resume reached the redirect URI without an authorization code"
                raise PorscheExceptionError(msg)

            resp = await self.client.get(
                current_url,
                timeout=TIMEOUT,
                headers=self.headers,
                follow_redirects=False,
            )

            if resp.status_code in (302, 303, 307, 308):
                current_url = urljoin(str(resp.url), resp.headers["Location"])
                continue

            if resp.status_code == 200 and PASSKEY_ENROLLMENT_PATH in resp.url.path:
                current_url = await self._skip_passkey_enrollment(str(resp.url), resp.text)
                continue

            _LOGGER.error(
                "Unexpected response %s at %s while resuming authorization.",
                resp.status_code,
                resp.url,
            )
            msg = "Could not fetch authorization code"
            raise PorscheExceptionError(msg)

        msg = "AUTHORIZATION_CODE_REDIRECT_LOOP"
        raise PorscheExceptionError(msg)

    async def login_with_identifier(self, state: str):
        """Log into the Identifier First flow.

        Takes 2 steps:

        1. POST to /u/login/identifier with email
        2. POST to /u/login/password with password

        :param state: state parameter from the initial authorize request
        :return: URL to resume the auth code request
        """
        # 1. /u/login/identifier w/ email (and captcha code)

        data = {
            "state": state,
            "username": self.credentials.email,
            "js-available": True,
            "webauthn-available": False,
            "is-brave": False,
            "webauthn-platform-available": False,
            "action": "default",
        }

        if self.captcha.captcha_code is None:
            _LOGGER.debug("Submitting e-mail address to auth endpoint.")
        else:
            data.update({"captcha": self.captcha.captcha_code})
            # Do not log the captcha code itself — it is a single-use secret
            # and ends up in user-shared logs otherwise.
            _LOGGER.debug("Submitting e-mail address and captcha code to auth endpoint.")

        url = f"https://{AUTHORIZATION_SERVER}/u/login/identifier"
        resp = await self.client.post(
            url,
            data=data,
            params={"state": state},
            timeout=TIMEOUT,
            headers=self.headers,
        )

        if resp.status_code == 401:
            reason = self._extract_auth0_error(resp.text)
            _LOGGER.debug("Identifier step rejected: %s", reason or "(no reason in response)")
            if reason and _THROTTLE_PATTERN.search(reason):
                msg = f"Login throttled by Auth0 ({reason})"
                raise PorscheLoginThrottledError(msg)
            msg = f"Wrong credentials ({reason})" if reason else "Wrong credentials"
            raise PorscheWrongCredentialsError(msg)

        # In case captcha verification is required, the response code is 400 and the captcha is provided as a svg image
        if resp.status_code == 400:
            _LOGGER.debug("Captcha required.")
            captcha_img = self._extract_captcha_image(resp.text)
            if not captcha_img:
                # A 400 with no captcha in it is usually not a captcha at all:
                # Auth0 answers throttled and blocked logins the same way.
                reason = self._extract_auth0_error(resp.text)
                if reason:
                    _LOGGER.error("Identifier step refused by Auth0: %s", reason)
                    msg = f"Login refused by Auth0: {reason}"
                    raise PorscheExceptionError(msg)
                _LOGGER.error("Could not find captcha in response. HTML: %s", resp.text[:2000])
                msg = "Captcha required but could not parse captcha image"
                raise PorscheExceptionError(msg)

            _LOGGER.debug("Parsed captcha image: %s...", str(captcha_img)[:100])
            raise PorscheCaptchaRequiredError(
                captcha=captcha_img,
                state=state,
                cookies=serialize_cookies(self.client.cookies),
                code_verifier=self.code_verifier,
            )

        # 2. /u/login/password w/ password

        _LOGGER.debug("Submitting password to auth endpoint.")

        data = {
            "state": state,
            "username": self.credentials.email,
            "password": self.credentials.password,
            "action": "default",
        }

        url = f"https://{AUTHORIZATION_SERVER}/u/login/password"
        resp = await self.client.post(
            url,
            data=data,
            params={"state": state},
            timeout=TIMEOUT,
            headers=self.headers,
        )

        # Auth0 answers a wrong password, a throttled login and a blocked
        # account all with 400, so the page's own wording is the only signal.
        if resp.status_code == 400:
            reason = self._extract_auth0_error(resp.text)
            _LOGGER.debug("Password step rejected: %s", reason or "(no reason in response)")
            if reason and _THROTTLE_PATTERN.search(reason):
                msg = f"Login throttled by Auth0 ({reason})"
                raise PorscheLoginThrottledError(msg)
            msg = f"Wrong credentials ({reason})" if reason else "Wrong credentials"
            raise PorscheWrongCredentialsError(msg)

        # A successful password step replies with a 302 whose Location is the
        # resume URL. Anything else (MFA interstitial, error page) has no
        # Location header — surface it instead of KeyError-ing.
        resume_url = resp.headers.get("Location")
        if not resume_url:
            msg = f"Unexpected password-step response (HTTP {resp.status_code}); no resume URL"
            raise PorscheExceptionError(msg)
        _LOGGER.debug("Resume at %s:", resume_url)

        return resume_url

    async def fetch_access_token(self, authorization_code):
        """Exchanges the authorization code for an access token.

        :param authorization_code: authorization code from the /authorize request
        :return: access token
        """
        data = {
            "client_id": CLIENT_ID,
            "grant_type": "authorization_code",
            "code": authorization_code,
            "redirect_uri": REDIRECT_URI,
        }
        if self.code_verifier is not None:
            data["code_verifier"] = self.code_verifier

        try:
            _LOGGER.debug("Exchanging the authorization code for an access token.")

            resp = await send_with_retries(
                partial(self.client.post, TOKEN_URL, data=data, timeout=TIMEOUT, headers=self.headers),
                description="token endpoint (authorization_code)",
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise PorscheExceptionError(
                exc.response.status_code,
                response_body=exc.response.text[:1000] or None,
            ) from exc

    async def refresh_token(self, refresh_token):
        """Use the provided refresh token to get a new access token.

        :param refresh_token: refresh token
        :return: access token
        """
        data = {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        try:
            _LOGGER.debug("Using the refresh token to get a new access token.")

            resp = await send_with_retries(
                partial(self.client.post, TOKEN_URL, data=data, timeout=TIMEOUT, headers=self.headers),
                description="token endpoint (refresh_token)",
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            # 403 usually means the refresh token is invalid
            # clear the access token so the full login flow can happen again
            if exc.response.status_code == 403:
                return {"access_token": None, "expires_in": 0}
            raise PorscheExceptionError(exc.response.status_code) from exc
