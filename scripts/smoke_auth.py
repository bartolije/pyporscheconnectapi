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
    python scripts/smoke_auth.py             # full run; stops if a captcha appears
    python scripts/smoke_auth.py --resume ABC12   # finish it, in a fresh client
    python scripts/smoke_auth.py --auth-only
"""

from __future__ import annotations

import argparse
import asyncio
import configparser
import json
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
# Holds the Auth0 transaction between --start-captcha and --resume: state,
# serialised session cookies and the PKCE verifier. Session secrets -> the
# file is gitignored and deleted as soon as the resume succeeds.
RESUME_STATE = Path(".captcha_resume.json")
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


async def _persist_captcha(err: PorscheCaptchaRequiredError) -> None:
    """Write the captcha image and the transaction needed to resume it."""
    out = Path("porsche_captcha.html")
    async with aiofiles.open(out, "w", encoding="utf-8") as fh:
        await fh.write(
            '<!doctype html><html lang="en"><title>Captcha</title>'
            f'<body><img src="{err.captcha}"/></body></html>',
        )
    checks = {
        "state": err.state,
        "cookies": err.cookies,
        "code_verifier": err.code_verifier,
    }
    for name, value in checks.items():
        print(f"{OK if value else KO}exception carries {name}" if value else f"{KO} exception is missing {name}")
    if not all(checks.values()):
        sys.exit("The captcha exception is incomplete — the resume would fail.")
    async with aiofiles.open(RESUME_STATE, "w", encoding="utf-8") as fh:
        await fh.write(json.dumps(checks))
    print(f"{INFO}Captcha image written to {out} — open it and read the code.")
    print(f"{INFO}Transaction saved to {RESUME_STATE} (session secrets, gitignored).")
    print(f"{INFO}Then run:  python scripts/smoke_auth.py --resume <CODE>")


async def authenticate(email: str, password: str) -> Connection:
    """Run the full auth flow; stop and persist the transaction on a captcha."""
    print(f"{INFO}Authenticating (fresh session, no cached token)…")
    client = httpx.AsyncClient()
    conn = Connection(email=email, password=password, async_client=client)
    try:
        await conn.get_token()
    except PorscheCaptchaRequiredError as err:
        await _persist_captcha(err)
        sys.exit(0)
    print(f"{OK}authenticated without a captcha")
    return conn


async def resume(email: str, password: str, code: str) -> Connection:
    """Resume a captcha in a FRESH client, as a separate process would."""
    if not await asyncio.to_thread(RESUME_STATE.exists):
        sys.exit(f"{RESUME_STATE} not found — start a run without --resume first.")
    async with aiofiles.open(RESUME_STATE, encoding="utf-8") as fh:
        saved = json.loads(await fh.read())
    print(f"{INFO}Resuming in a brand-new client (nothing shared but the saved transaction)…")
    conn = Connection(
        email=email,
        password=password,
        captcha_code=code,
        state=saved["state"],
        cookies=saved["cookies"],
        code_verifier=saved["code_verifier"],
        async_client=httpx.AsyncClient(),
    )
    await conn.get_token()
    print(f"{OK}captcha resumed in a fresh client (cookies + PKCE verifier carried over)")
    await asyncio.to_thread(RESUME_STATE.unlink, missing_ok=True)
    print(f"{INFO}{RESUME_STATE} deleted (it held session secrets).")
    return conn


async def run(*, auth_only: bool, resume_code: str | None) -> int:
    """Execute the smoke test, returning a process exit code."""
    email, password = load_credentials()
    try:
        conn = await resume(email, password, resume_code) if resume_code else await authenticate(email, password)
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
        data = vehicle.data or {}
        summary = data.get("CHARGING_SUMMARY") or {}
        print(f"{OK}{mask(vehicle.vin)} overview parsed, {len(data)} measurement group(s)")
        print(f"{INFO}groups: {', '.join(sorted(data)) or '(none)'}")
        if summary:
            print(
                f"{INFO}charging: minSoC={summary.get('minSoC')} "
                f"targetSoC={summary.get('targetSoC')} mode={summary.get('mode')}",
            )
        else:
            print(f"{INFO}no CHARGING_SUMMARY — targetSoC precedence not exercised by this vehicle")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-only", action="store_true", help="stop after authentication")
    parser.add_argument(
        "--resume",
        metavar="CODE",
        default=None,
        help="resume a captcha started by a previous run, using the code you read from it",
    )
    parsed = parser.parse_args()
    sys.exit(asyncio.run(run(auth_only=parsed.auth_only, resume_code=parsed.resume)))
