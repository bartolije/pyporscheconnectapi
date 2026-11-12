#!/usr/bin/python

"""Command line interface for Porsche Connect API functions."""

import argparse
import asyncio
import configparser
import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import date, datetime
from getpass import getpass
from pathlib import Path
from typing import Any

try:
    import aiofiles
except ImportError:  # pragma: no cover - the CLI extra is not installed
    sys.exit("The CLI needs its extra dependencies: pip install 'pyporscheconnectapi-bartolije[cli]'")

from pyporscheconnectapi.account import PorscheConnectAccount
from pyporscheconnectapi.connection import Connection
from pyporscheconnectapi.exceptions import PorscheCaptchaRequiredError, PorscheWrongCredentialsError
from pyporscheconnectapi.oauth2 import Captcha
from pyporscheconnectapi.remote_services import RemoteServices
from pyporscheconnectapi.vehicle import PorscheVehicle

vehicle_commands = {
    "battery": "Prints the main battery level (BEV)",
    "capabilities": "Get vehicle capabilities",
    "chargingprofile": "Update parameters in configured charging profile",
    "chargingsettings": "Update charging settings",
    "climatise_off": "Stop remote climatisation",
    "climatise_on": "Start remote climatisation",
    "connected": "Check if vehicle is on-line",
    "currentoverview": "Poll vehicle for current overview",
    "direct_charge_off": "Disable direct charging",
    "direct_charge_on": "Enable direct charging",
    "doors_and_lids": "List status of all doors and lids",
    "flash_indicators": "Flash indicators",
    "honk_and_flash": "Flash indicators and sound the horn",
    "location": "Show location of vehicle",
    "lock_vehicle": "Lock vehicle",
    "pictures": "Get vehicle pictures url",
    "storedoverview": "Get stored overview for vehicle",
    "tire_status": "Check if tire pressure are ok",
    "tire_pressures": "Get tire pressure readings",
    "trip_statistics": "Get trip statistics from backend",
    "unlock_vehicle": "Unlock vehicle",
    "vehicle_closed": "Check if all doors and lids are closed",
}

printc: Callable[..., None] = print
printj: Callable[..., None] = print
try:
    from rich.console import Console
except ImportError:  # pragma: no cover - the CLI extra is not installed
    pass
else:
    console = Console()
    printc = console.print
    printj = console.print_json

logging.basicConfig()
logging.root.setLevel(logging.WARNING)

_LOGGER = logging.getLogger(__name__)


async def load_token(session_file: str | Path) -> dict[str, Any]:
    """Load the cached session token from disk."""
    try:
        async with aiofiles.open(Path(session_file), encoding="utf-8") as json_file:
            return json.loads(await json_file.read())
    except FileNotFoundError:
        return {}
    except json.decoder.JSONDecodeError:
        return {}


async def get_vehicles_with_captcha_retry(controller: PorscheConnectAccount, connection: Connection) -> list[PorscheVehicle]:
    """Fetch vehicles, prompting for a CAPTCHA challenge when required."""
    try:
        return await controller.get_vehicles()
    except PorscheCaptchaRequiredError as captcha_err:
        captcha_file = Path("porsche_captcha.html")
        async with aiofiles.open(captcha_file, "w", encoding="utf-8") as f:
            await f.write(f'<!doctype html><html lang="en"><title>Captcha</title><body><img src="{captcha_err.captcha}"/></body></html>')

        printc(
            f"\n⚠️ CAPTCHA required.\nCAPTCHA image written to: {captcha_file}\nOpen this file in your browser, solve the CAPTCHA, and enter the text below.",
        )

        captcha_code = await async_input("CAPTCHA: ")
        connection.oauth2_client.captcha = Captcha(captcha_code, captcha_err.state)
        return await controller.get_vehicles()


async def battery(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Get vehicle battery state of charge (%)."""
    await vehicle.get_stored_overview()
    return vehicle.main_battery_level


async def capabilities(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Get vehicle capabilities."""
    await vehicle.get_capabilities()
    return vehicle.capabilities


async def chargingprofile(vehicle: PorscheVehicle, args: argparse.Namespace) -> Any:
    """Manipulate charging profile."""
    await vehicle.get_stored_overview()
    service = RemoteServices(vehicle)
    result = await service.update_charging_profile(
        profile_id=args.profileid,
        minimum_charge_level=args.minimumchargelevel,
    )
    return result.status


async def chargingsettings(vehicle: PorscheVehicle, args: argparse.Namespace) -> Any:
    """Edit charging settings."""
    await vehicle.get_stored_overview()
    service = RemoteServices(vehicle)
    result = await service.update_charging_setting(
        target_soc=args.target_soc,
    )
    return result.status


async def climatise_off(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Stop climatisation."""
    service = RemoteServices(vehicle)
    result = await service.climatise_off()
    return result.status


async def climatise_on(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Start climatisation."""
    service = RemoteServices(vehicle)
    result = await service.climatise_on()
    return result.status


async def connected(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Get vehicle on-line status."""
    await vehicle.get_current_overview()
    return vehicle.connected


async def currentoverview(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Get current overview from vehicle."""
    await vehicle.get_current_overview()
    return vehicle.data


async def direct_charge_off(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Disable direct charging."""
    service = RemoteServices(vehicle)
    result = await service.direct_charge_off()
    return result.status


async def direct_charge_on(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Enable direct charging."""
    service = RemoteServices(vehicle)
    result = await service.direct_charge_on()
    return result.status


async def doors_and_lids(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Get stored status of doors and lids from backend."""
    await vehicle.get_stored_overview()
    return vehicle.doors_and_lids


async def flash_indicators(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Flash the indicators briefly."""
    service = RemoteServices(vehicle)
    result = await service.flash_indicators()
    return result.status


async def honk_and_flash(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Honk and flash the indicators briefly."""
    service = RemoteServices(vehicle)
    result = await service.honk_and_flash_indicators()
    return result.status


async def location(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Get the location of the vehicle."""
    await vehicle.get_stored_overview()
    return vehicle.location


async def lock_vehicle(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Lock the vehicle."""
    service = RemoteServices(vehicle)
    result = await service.lock_vehicle()
    return result.status


async def pictures(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Get pictures (uri) of the vehicle."""
    await vehicle.get_picture_locations()
    return vehicle.picture_locations


async def storedoverview(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Get stored overview from back-end."""
    await vehicle.get_stored_overview()
    return vehicle.data


async def tire_status(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Get tire pressure status from back-end."""
    await vehicle.get_stored_overview()
    return vehicle.tire_pressure_status


async def tire_pressures(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Get tire pressures from back-end."""
    await vehicle.get_stored_overview()
    return vehicle.tire_pressures


async def trip_statistics(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Get pictures (uri) of the vehicle."""
    await vehicle.get_trip_statistics()
    return vehicle.trip_statistics


async def unlock_vehicle(vehicle: PorscheVehicle, args: argparse.Namespace) -> Any:
    """Unock the vehicle."""
    service = RemoteServices(vehicle)
    result = await service.unlock_vehicle(args.pin)
    return result.status


async def vehicle_closed(vehicle: PorscheVehicle, _args: argparse.Namespace) -> Any:
    """Check with backend if doors and lids are closed."""
    await vehicle.get_stored_overview()
    return vehicle.vehicle_closed


async def async_input(prompt: str, *, strip: bool = True) -> str:
    """Async-friendly input() wrapper."""
    value = await asyncio.to_thread(input, prompt)
    return value.strip() if strip else value


async def save_token(session_file: str | Path, token: dict[str, Any]) -> None:
    """Save token to file."""
    # The session file holds the tokens: owner-only, also when it already exists.
    async with aiofiles.open(session_file, "w", encoding="utf-8", opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
        await f.write(json.dumps(token, ensure_ascii=False, indent=2))
    await asyncio.to_thread(os.chmod, session_file, 0o600)


def obj_to_json(obj: object) -> str:
    """Convert Python objects of type 'datetime' or 'date' to json."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    msg = f"Can't convert type {type(obj)}"
    raise TypeError(msg)


async def main(args: argparse.Namespace) -> None:
    """Get arguments from parser and run command."""
    token = await load_token(args.session_file)

    if args.debug:
        logging.root.setLevel(logging.DEBUG)

    email = args.email or await async_input("Please enter Porsche Connect email: ")
    password = args.password or getpass()

    connection = Connection(email, password, token=token)
    controller = PorscheConnectAccount(connection=connection)

    response: Any = {}
    try:
        if args.command == "token":
            # Checks the session; its secrets stay in the session file.
            response = {key: "***" if key.endswith("token") else value for key, value in (await connection.get_token()).items()}
        else:
            vehicles = await get_vehicles_with_captcha_retry(controller, connection)
            if not vehicles:
                printc("No vehicles found.")

            if args.command == "list":
                response = [vehicle.data for vehicle in vehicles]
            elif args.vin is not None:
                vehicle = await controller.get_vehicle(args.vin)
                if vehicle is not None:
                    response = await globals()[args.func](vehicle, args)
            else:
                sys.exit("--vin is required")
    except PorscheWrongCredentialsError as e:
        sys.exit(e.message)
    else:
        if args.json:
            printj(json.dumps(response, indent=2, sort_keys=True, default=obj_to_json))
        else:
            printc(response)
    finally:
        # Also on failure: a token refreshed during the run must not be lost.
        await connection.close()
        await save_token(args.session_file, connection.token)


def add_arg_vin(parser: argparse.ArgumentParser) -> None:
    """Add vin to the argument parser."""
    group = parser.add_mutually_exclusive_group(
        required=True,
    )
    group.add_argument("-v", "--vin", dest="vin", default=None)


def cli() -> None:
    """Get configuration parameters and command line arguments and run main loop."""
    # interpolation=None: passwords routinely contain '%', which the default
    # BasicInterpolation treats as syntax and dies on (InterpolationSyntaxError,
    # with the offending fragment echoed into the traceback).
    config = configparser.ConfigParser(interpolation=None)
    config["porsche"] = {
        "email": "",
        "password": "",
        "session_file": ".session",
    }
    config.read([".porscheconnect.cfg", Path("~/.porscheconnect.cfg").expanduser()])
    parser = argparse.ArgumentParser(description="Porsche Connect CLI")
    subparsers = parser.add_subparsers(help="command help", dest="command")

    parser.add_argument("-d", "--debug", dest="debug", action="store_true")
    parser.add_argument("-j", "--json", dest="json", action="store_true")
    parser.add_argument(
        "-e",
        "--email",
        dest="email",
        default=config.get("porsche", "email"),
    )
    parser.add_argument(
        "-p",
        "--password",
        dest="password",
        default=config.get("porsche", "password"),
    )
    parser.add_argument(
        "-s",
        "--sessionfile",
        dest="session_file",
        default=config.get("porsche", "session_file"),
    )

    subparsers.add_parser("list")
    subparsers.add_parser("token")

    for vcmd, vdesc in vehicle_commands.items():
        parser_command = subparsers.add_parser(vcmd, help=vdesc)
        parser_command.set_defaults(func=vcmd)
        add_arg_vin(parser_command)
        if vcmd == "unlock_vehicle":
            parser_command.add_argument(
                "-n",
                "--pin",
                required=True,
                dest="pin",
                default=None,
            )
        if vcmd == "chargingprofile":
            parser_command.add_argument(
                "--profileid",
                dest="profileid",
                type=int,
                required=False,
                help="Profile id",
            )
            parser_command.add_argument(
                "--chargelevel",
                dest="minimumchargelevel",
                type=int,
                required=False,
                default=None,
                help="Minimum charge level",
            )
        if vcmd == "chargingsettings":
            parser_command.add_argument(
                "--targetsoc",
                dest="target_soc",
                type=int,
                required=False,
                default=None,
                help="Target state of charge",
            )

    args = parser.parse_args()

    if args.command:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main(args))
    else:
        parser.print_help(sys.stderr)
