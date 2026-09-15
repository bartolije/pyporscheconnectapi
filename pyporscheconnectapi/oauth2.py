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
    PorscheWrongCredentialsError,
)
from .retry import send_with_retries

_LOGGER = logging.getLogger(__name__)

# Auth0 needs a brief settle time after the password POST before the resume
# endpoint will mint the authorization code. Poll immediately, then back off —
# a fixed sleep penalised every login even when Auth0 was already ready.
_RESUME_POLL_DELAYS = (0.0, 0.5, 1.0, 2.0, 2.5)

# Auth0 interleaves extra screens into the resume redirect chain (passkey
# enrollment since mid-2026), so the chain is followed hop by hop instead of
# expecting a single redirect straight to the authorization code.
PASSKEY_ENROLLMENT_PATH = "/u/passkey-enrollment"
_MAX_RESUME_REDIRECTS = 10
_REDIRECT_STATUS_CODES = frozenset({302, 303, 307, 308})
_HTTP_OK = 200


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
    :param code_verifier: PKCE verifier of an interrupted login, to resume a captcha
        challenge started by another process
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
        # Auth0 enforces PKCE (RFC 7636) on this client: the verifier is minted
        # with the /authorize request and replayed at the token exchange.
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

        1. Initial request to /authorize to get the code (with a PKCE challenge)
        2. If no code is returned, login with Identifier First flow:
            2a. POST to /u/login/identifier with email
            2b. POST to /u/login/password with password
        3. Resume the /authorize request with the resume path from the Identifier First flow,
           following the redirect chain (and declining the passkey enrollment screen)

        :return: authorization code to be exchanged for an access token
        """
        try:
            # When retrying after a captcha challenge, the caller has already
            # been through /authorize once (the state is carried in
            # self.captcha.state). Skip that round-trip and resume the
            # Identifier First flow directly.
            if self.captcha.captcha_code is not None:
                if self.code_verifier is None:
                    # The verifier is bound to the /authorize request that
                    # issued the challenge — Auth0 rejects the code exchange
                    # without it. A caller resuming from another process must
                    # carry PorscheCaptchaRequiredError.code_verifier over.
                    msg = "PKCE_VERIFIER_MISSING_FOR_CAPTCHA_RESUME"
                    raise PorscheExceptionError(msg)
                state = self.captcha.state
            else:
                _LOGGER.debug("Fetching authorization code.")
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
            authorization_code = await self.resume_authorization_code_flow(
                urljoin(f"https://{AUTHORIZATION_SERVER}", resume_path),
            )

        except httpx.HTTPStatusError as exc:
            raise PorscheExceptionError(exc.response.status_code) from exc

        _LOGGER.debug("Authorization code: %s", authorization_code)
        return authorization_code

    async def resume_authorization_code_flow(self, resume_url: str) -> str:
        """Resume the /authorize request and return the authorization code.

        Polls the resume endpoint — Auth0 sometimes needs a moment before it
        mints the code, and ending the wait as soon as it is available beats
        paying a fixed settle delay on every login. Each attempt follows the
        whole redirect chain, since Auth0 may route through extra screens
        (passkey enrollment) before handing out the code.

        :param resume_url: resume URL returned by the Identifier First flow
        :return: authorization code to be exchanged for an access token
        """
        for attempt, delay in enumerate(_RESUME_POLL_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            code = await self._follow_resume_redirects(resume_url)
            if code is not None:
                return code
            _LOGGER.debug("Resume attempt %d returned no authorization code yet.", attempt + 1)
        msg = f"Auth0 resume returned no authorization code after {len(_RESUME_POLL_DELAYS)} attempts"
        raise PorscheExceptionError(msg)

    async def _follow_resume_redirects(self, resume_url: str) -> str | None:
        """Follow the Auth0 redirect chain until the authorization code shows up.

        :param resume_url: resume URL returned by the Identifier First flow
        :return: the authorization code, or None when Auth0 is not ready yet
            (the caller retries); structural failures raise instead.
        """
        current_url = resume_url

        for _ in range(_MAX_RESUME_REDIRECTS):
            parsed = urlparse(current_url)
            # Checked before fetching: the last hop is the app callback URL,
            # which lives outside Auth0 and must not be requested.
            code = parse_qs(parsed.query).get("code", [None])[0]
            if code is not None:
                return code

            if parsed.scheme not in ("http", "https"):
                # Handed over to the app callback scheme (my-porsche-app://)
                # without a code — nothing fetchable left in the chain.
                _LOGGER.debug("Resume chain ended at %s with no authorization code.", parsed.scheme)
                return None

            resp = await self.client.get(
                current_url,
                timeout=TIMEOUT,
                headers=self.headers,
                follow_redirects=False,
            )

            if resp.status_code in _REDIRECT_STATUS_CODES:
                current_url = urljoin(str(resp.url), resp.headers["Location"])
                continue

            if resp.status_code == _HTTP_OK and PASSKEY_ENROLLMENT_PATH in resp.url.path:
                current_url = await self._skip_passkey_enrollment(str(resp.url), resp.text)
                continue

            _LOGGER.debug(
                "Unexpected response %s at %s while resuming authorization.",
                resp.status_code,
                resp.url,
            )
            return None

        msg = "AUTHORIZATION_CODE_REDIRECT_LOOP"
        raise PorscheExceptionError(msg)

    async def _skip_passkey_enrollment(self, url: str, html: str | None = None) -> str:
        """Decline the optional passkey enrollment screen and return where to continue.

        :param url: URL of the passkey enrollment screen
        :param html: its HTML, when already fetched
        :return: URL to continue the resume chain with
        """
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
        if resp.status_code not in _REDIRECT_STATUS_CODES:
            msg = "PASSKEY_ENROLLMENT_SKIP_FAILED"
            raise PorscheExceptionError(msg)

        return urljoin(url, resp.headers["Location"])

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

    def _extract_universal_login_context(self, html: str) -> dict | None:
        """Extract the Auth0 universal login (ACUL) context from its inline base64 payload."""
        match = re.search(r'atob\("([A-Za-z0-9+/=]+)"', html)
        if not match:
            return None

        try:
            decoded = base64.b64decode(match.group(1)).decode("utf-8")
            return json.loads(decoded)
        except (ValueError, json.JSONDecodeError, binascii.Error) as exc:
            _LOGGER.warning("Failed to parse Auth0 universal login context: %s", exc)
            return None

    def _extract_captcha_image(self, html: str):
        """Extract the captcha image from Auth0 ACUL or legacy login HTML."""
        context_data = self._extract_universal_login_context(html)
        if context_data is not None:
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
            msg = "Wrong credentials"
            raise PorscheWrongCredentialsError(msg)

        # In case captcha verification is required, the response code is 400 and the captcha is provided as a svg image
        if resp.status_code == 400:
            _LOGGER.debug("Captcha required.")
            captcha_img = self._extract_captcha_image(resp.text)
            if not captcha_img:
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

        # In case of wrong password, the response code is 400 (Bad request)
        if resp.status_code == 400:
            _LOGGER.debug("Invalid credentials.")
            msg = "Wrong credentials"
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
            # The body carries Auth0's reason (invalid PKCE verifier, expired
            # code, ...) — "BAD_REQUEST" on its own is undebuggable.
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
            raise PorscheExceptionError(
                exc.response.status_code,
                response_body=exc.response.text[:1000] or None,
            ) from exc
