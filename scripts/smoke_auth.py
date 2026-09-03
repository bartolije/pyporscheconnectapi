#  SPDX-License-Identifier: Apache-2.0
"""End-to-end smoke test against the real Porsche Connect API.

Exercises exactly what the upstream merge changed and what the mocked unit
tests cannot reach:

* the PKCE authorization flow (verifier minted, S256 challenge sent,
  verifier replayed at the token exchange);
* the captcha challenge, including resuming it in a *fresh* client with
  both ``cookies`` and ``code_verifier`` carried over;
* the passkey-enrolment interstitial, if Auth0 shows one;
* ``targetSoC`` precedence on the vehicle overview.

Credentials come from ``.porscheconnect.cfg`` (gitignored). Copy
``.porscheconnect.cfg.example`` and fill it in. Nothing secret is printed:
tokens, passwords and cookies are never echoed, and VINs are masked.

Usage:
    python scripts/smoke_auth.py            # full run
    python scripts/smoke_auth.py --auth-only
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import sys
from pathlib import Path

import aiofiles
import httpx

from pyporscheconnectapi.account import PorscheConnectAccount
from pyporscheconnectapi.connection import Connection
from pyporscheconnectapi.exceptions import (
    PorscheCaptchaRequiredError,
    PorscheExceptionError,
    PorscheWrongCredentialsError,
)

CFG = Path(".porscheconnect.cfg")
OK, KO, INFO = "  \033[32mOK\033[0m  ", "  \033[31mFAIL\033[0m", "  ..  "


def mask(vin: str) -> str:
    """Mask a VIN, keeping just enough to tell vehicles apart."""
    return f"{vin[:4]}…{vin[-4:]}" if vin and len(vin) > 8 else "…"


def load_credentials() -> tuple[str, str]:
    """Read email/password from the gitignored config file."""
    if not CFG.exists():
        sys.exit(
            f"{CFG} not found.\n"
            f"Copy .porscheconnect.cfg.example to {CFG} and fill in your credentials.",
        )
    # interpolation=None: a '%' in the password is a literal, not syntax.
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        cfg.read(CFG)
        email = cfg.get("porsche", "email", fallback="")
        password = cfg.get("porsche", "password", fallback="")
    except configparser.Error as exc:
        # Deliberately not echoing exc: configparser puts the offending
        # value (i.e. the password) straight into its message.
        sys.exit(f"Could not parse {CFG}: {type(exc).__name__}. Check the [porsche] section.")
    if not email or not password:
        sys.exit(f"{CFG} is missing 'email' or 'password' under [porsche].")
    return email, password


async def authenticate(email: str, password: str) -> Connection:
    """Run the full auth flow, resuming through a captcha if one is shown."""
    print(f"{INFO}Authenticating (fresh session, no cached token)…")
    async with httpx.AsyncClient() as client:
        conn = Connection(email=email, password=password, async_client=client)
        try:
            await conn.get_token()
        except PorscheCaptchaRequiredError as err:
            print(f"{INFO}Captcha required — this exercises the multi-process resume path.")
            out = Path("porsche_captcha.html")
            async with aiofiles.open(out, "w", encoding="utf-8") as fh:
                await fh.write(
                    '<!doctype html><html lang="en"><title>Captcha</title>'
                    f'<body><img src="{err.captcha}"/></body></html>',
                )
            print(f"{INFO}Captcha image written to {out} — open it and read the code.")
            if err.code_verifier is None:
                print(f"{KO} exception carried no code_verifier — PKCE resume would fail")
                raise
            print(f"{OK}exception carries state, cookies and code_verifier")
            try:
                code = await asyncio.to_thread(input, "  CAPTCHA code: ")
            except EOFError:
                sys.exit(
                    "  No terminal to read the captcha code from.\n"
                    "  Re-run this script interactively (not piped, not with stdin closed).",
                )
            code = code.strip()

            # A brand-new client on purpose: this is the fresh-process resume.
            async with httpx.AsyncClient() as client_two:
                conn = Connection(
                    email=email,
                    password=password,
                    captcha_code=code,
                    state=err.state,
                    cookies=err.cookies,
                    code_verifier=err.code_verifier,
                    async_client=client_two,
                )
                await conn.get_token()
                print(f"{OK}captcha resumed in a fresh client (PKCE verifier carried over)")
                return conn
        else:
            print(f"{OK}authenticated without a captcha")
            return conn
    return conn


async def run(*, auth_only: bool) -> int:
    """Execute the smoke test, returning a process exit code."""
    email, password = load_credentials()
    try:
        conn = await authenticate(email, password)
    except PorscheWrongCredentialsError:
        print(f"{KO} credentials rejected — check {CFG}")
        return 1
    except PorscheExceptionError as exc:
        print(f"{KO} authentication failed: {exc}")
        return 1

    if conn.token.get("access_token"):
        print(f"{OK}access token obtained (not printed)")
    else:
        print(f"{KO} no access token in the response")
        return 1
    if auth_only:
        return 0

    account = PorscheConnectAccount(connection=conn)
    try:
        vehicles = await account.get_vehicles()
    except PorscheExceptionError as exc:
        print(f"{KO} could not list vehicles: {exc}")
        return 1
    print(f"{OK}{len(vehicles)} vehicle(s): {', '.join(mask(v.vin) for v in vehicles)}")

    for vehicle in vehicles:
        try:
            await vehicle.get_stored_overview()
        except PorscheExceptionError as exc:
            print(f"{KO} overview failed for {mask(vehicle.vin)}: {exc}")
            return 1
        summary = (vehicle.data or {}).get("CHARGING_SUMMARY") or {}
        print(
            f"{OK}{mask(vehicle.vin)} overview parsed "
            f"(minSoC={summary.get('minSoC')}, targetSoC={summary.get('targetSoC')})",
        )
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-only", action="store_true", help="stop after authentication")
    sys.exit(asyncio.run(run(auth_only=parser.parse_args().auth_only)))
