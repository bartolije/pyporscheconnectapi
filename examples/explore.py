"""Interactive exploration script for the Porsche Connect API.

Goal: connect to the live API and dump the *raw* JSON responses so we can
compare them against what the library currently parses and improve it.

Single process on purpose: the Auth0 captcha transaction (state + session
cookies) does not survive a restart, so the captcha handoff happens via a
file while the same httpx client stays alive.

Run:  python ./examples/explore.py
Reads EMAIL / PASSWORD from the gitignored .env at the repo root.

When a captcha is required the script:
  1. writes the captcha image to examples/_explore_out/captcha.<ext>
  2. prints CAPTCHA_REQUIRED and polls examples/_explore_out/captcha_code.txt
  3. drop the solved code into that file and it resumes.
"""

import asyncio
import base64
import binascii
import json
import pathlib
import urllib.parse

import httpx

from pyporscheconnectapi.account import PorscheConnectAccount
from pyporscheconnectapi.connection import Connection
from pyporscheconnectapi.const import MEASUREMENTS
from pyporscheconnectapi.exceptions import PorscheCaptchaRequiredError

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = pathlib.Path(__file__).resolve().parent / "_explore_out"
OUT.mkdir(exist_ok=True)


def load_env() -> dict[str, str]:
    """Parse the repo-root .env into a dict (no external dependency)."""
    env: dict[str, str] = {}
    env_file = ROOT / ".env"
    if not env_file.exists():
        msg = f".env introuvable: {env_file}"
        raise SystemExit(msg)
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def dump(name: str, payload: object) -> None:
    """Write a payload as pretty JSON and echo a one-line summary."""
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    print(f"  -> {path.relative_to(ROOT)}")


def save_captcha_image(captcha: str) -> pathlib.Path:
    """Decode the captcha (data URI or raw svg) and write it to disk."""
    if captcha.startswith("data:"):
        header, _, body = captcha.partition(",")
        ext = "svg" if "svg" in header else (header.split("/")[1].split(";")[0] if "/" in header else "bin")
        if ";base64" in header:
            try:
                data = base64.b64decode(body)
            except (binascii.Error, ValueError):
                data = body.encode()
        else:
            data = urllib.parse.unquote(body).encode()
    else:
        # Raw markup or bare base64.
        ext = "svg"
        data = captcha.encode()
    path = OUT / f"captcha.{ext}"
    path.write_bytes(data)
    return path


async def wait_for_captcha_code(captcha: str, state: str) -> str:
    """Persist the captcha image and block until the code file appears."""
    img_path = save_captcha_image(captcha)
    (OUT / "captcha_state.txt").write_text(state or "")
    code_file = OUT / "captcha_code.txt"
    if code_file.exists():
        code_file.unlink()
    print(f"CAPTCHA_REQUIRED image={img_path.relative_to(ROOT)} state_len={len(state or '')}", flush=True)
    while not code_file.exists():  # noqa: ASYNC110 - polling a file written by an external process
        await asyncio.sleep(2)
    code = code_file.read_text().strip()
    print(f"Captcha code reçu ({len(code)} caractères), reprise du login…", flush=True)
    return code


async def fetch_vehicles(env: dict[str, str], client: httpx.AsyncClient):
    """Authenticate (solving a captcha if needed) and return the vehicle list."""
    conn = Connection(env["EMAIL"], env["PASSWORD"], async_client=client)
    account = PorscheConnectAccount(connection=conn)
    try:
        return conn, await account.get_vehicles()
    except PorscheCaptchaRequiredError as exc:
        code = await wait_for_captcha_code(exc.captcha, exc.state)
        conn = Connection(
            env["EMAIL"],
            env["PASSWORD"],
            captcha_code=code,
            state=exc.state,
            async_client=client,
        )
        account = PorscheConnectAccount(connection=conn)
        return conn, await account.get_vehicles()


async def main() -> None:
    """Connect, list vehicles, and dump raw overview + measurement payloads."""
    env = load_env()
    client = httpx.AsyncClient()
    try:
        conn, vehicles = await fetch_vehicles(env, client)
        print(f"\n{len(vehicles)} véhicule(s) trouvé(s).")

        overview = [v.data for v in vehicles]
        dump("00_vehicles_overview", overview)

        all_mf = "mf=" + "&mf=".join(MEASUREMENTS)
        for i, vehicle in enumerate(vehicles):
            vin = vehicle.data["vin"]
            print(f"\n[{vin}] {vehicle.data.get('modelName')}")

            # Raw, unparsed measurement payload — the whole point of this run.
            raw = await conn.get(f"/connect/v1/vehicles/{vin}?{all_mf}")
            dump(f"{i:02d}_{vin}_measurements_raw", raw)

            # Show which requested keys actually came back vs. were dropped.
            returned = {m["key"] for m in raw.get("measurements", [])}
            missing = [k for k in MEASUREMENTS if k not in returned]
            unexpected = [k for k in returned if k not in MEASUREMENTS]
            print(f"    measurements renvoyées: {len(returned)}/{len(MEASUREMENTS)}")
            if missing:
                print(f"    absentes: {', '.join(missing)}")
            if unexpected:
                print(f"    inattendues (à ajouter à la lib ?): {', '.join(unexpected)}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
