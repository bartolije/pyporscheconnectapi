"""CLI tests: token cache, captcha prompt, command dispatch, arg parsing."""
from __future__ import annotations

import argparse
import json
import sys
from types import SimpleNamespace
from typing import ClassVar

import pytest

from pyporscheconnectapi import cli
from pyporscheconnectapi.exceptions import (
    PorscheCaptchaRequiredError,
    PorscheWrongCredentialsError,
)
from pyporscheconnectapi.oauth2 import Captcha

# -- Session token cache ----------------------------------------------------


async def test_load_token_missing_file_returns_empty_dict(tmp_path):
    assert await cli.load_token(tmp_path / "absent.json") == {}


async def test_load_token_invalid_json_returns_empty_dict(tmp_path):
    session_file = tmp_path / "session.json"
    session_file.write_text("{not json")
    assert await cli.load_token(session_file) == {}


async def test_save_and_load_token_round_trip(tmp_path):
    session_file = tmp_path / "session.json"
    token = {"access_token": "A", "refresh_token": "R", "expires_at": 123}

    await cli.save_token(session_file, token)

    assert await cli.load_token(session_file) == token


# -- Captcha prompt ---------------------------------------------------------


class _CaptchaThenVehiclesController:
    def __init__(self):
        self.calls = 0

    async def get_vehicles(self):
        self.calls += 1
        if self.calls == 1:
            raise PorscheCaptchaRequiredError(captcha="data:image/svg+xml;base64,x", state="ST")
        return ["vehicle"]


async def test_get_vehicles_with_captcha_retry_prompts_and_retries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the captcha html lands in the CWD

    async def fake_input(_prompt: str, **_kwargs) -> str:
        return "CODE42"

    monkeypatch.setattr(cli, "async_input", fake_input)
    controller = _CaptchaThenVehiclesController()
    connection = SimpleNamespace(oauth2_client=SimpleNamespace(captcha=None))

    vehicles = await cli.get_vehicles_with_captcha_retry(controller, connection)

    assert vehicles == ["vehicle"]
    assert controller.calls == 2
    assert connection.oauth2_client.captcha == Captcha("CODE42", "ST")
    assert (tmp_path / "porsche_captcha.html").exists()


# -- main() dispatch --------------------------------------------------------


class _FakeVehicle:
    def __init__(self, vin: str = "WP0TEST", battery: int = 88):
        self.vin = vin
        self.data = {vin: {"modelName": "718"}}
        self.main_battery_level = battery

    async def get_stored_overview(self):
        return None


class _FakeConnection:
    def __init__(self, *_args, token=None, **_kwargs):
        self.token = token or {"access_token": "A"}
        self.closed = False

    async def close(self):
        self.closed = True


class _FakeAccount:
    vehicles: ClassVar[list] = []
    fail_with: ClassVar[Exception | None] = None

    def __init__(self, connection=None):
        self.connection = connection
        self.token = connection.token

    async def get_vehicles(self):
        if self.fail_with is not None:
            raise self.fail_with
        return self.vehicles

    async def get_vehicle(self, vin: str):
        return next((v for v in self.vehicles if v.vin == vin), None)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Patch the CLI's Connection/Account and provide ready-to-use args."""
    monkeypatch.setattr(cli, "Connection", _FakeConnection)
    monkeypatch.setattr(cli, "PorscheConnectAccount", _FakeAccount)
    _FakeAccount.vehicles = [_FakeVehicle()]
    _FakeAccount.fail_with = None

    def make_args(**overrides) -> argparse.Namespace:
        defaults = {
            "session_file": str(tmp_path / "session.json"),
            "debug": False,
            "json": False,
            "email": "user@example.com",
            "password": "s3cret",
            "command": None,
        }
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    return make_args


async def test_main_token_command_prints_and_saves_token(cli_env, tmp_path, capsys):
    await cli.main(cli_env(command="token"))

    assert "access_token" in capsys.readouterr().out
    saved = json.loads((tmp_path / "session.json").read_text())
    assert saved == {"access_token": "A"}


async def test_main_list_command_merges_vehicle_data(cli_env, capsys):
    _FakeAccount.vehicles = [_FakeVehicle("VIN1"), _FakeVehicle("VIN2")]

    await cli.main(cli_env(command="list"))

    out = capsys.readouterr().out
    assert "VIN1" in out
    assert "VIN2" in out


async def test_main_dispatches_vehicle_command_by_vin(cli_env, capsys):
    await cli.main(cli_env(command="battery", func="battery", vin="WP0TEST", json=True))

    assert "88" in capsys.readouterr().out


async def test_main_without_vin_exits(cli_env):
    with pytest.raises(SystemExit, match="--vin"):
        await cli.main(cli_env(command="battery", func="battery", vin=None))


async def test_main_wrong_credentials_exits_cleanly(cli_env):
    _FakeAccount.fail_with = PorscheWrongCredentialsError("Wrong credentials")

    with pytest.raises(SystemExit, match="Wrong credentials"):
        await cli.main(cli_env(command="battery", func="battery", vin="WP0TEST"))


# -- Argument parser --------------------------------------------------------


def test_cli_without_command_prints_help(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # don't pick up a real .porscheconnect.cfg
    monkeypatch.setattr(sys, "argv", ["porschecli"])

    cli.cli()

    assert "Porsche Connect CLI" in capsys.readouterr().err


def test_cli_unlock_requires_pin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["porschecli", "unlock_vehicle", "-v", "WP0TEST"])

    with pytest.raises(SystemExit):
        cli.cli()


def test_cli_parses_command_into_main_args(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["porschecli", "-j", "battery", "-v", "WP0TEST"])
    captured = {}

    async def fake_main(args):
        captured["args"] = args

    monkeypatch.setattr(cli, "main", fake_main)

    cli.cli()

    args = captured["args"]
    assert args.command == "battery"
    assert args.func == "battery"
    assert args.vin == "WP0TEST"
    assert args.json is True


def test_cli_reads_password_containing_percent(tmp_path, monkeypatch):
    """A '%' in the password is a literal, not configparser interpolation.

    The default ConfigParser uses BasicInterpolation, which raises
    InterpolationSyntaxError on a bare '%' - and echoes the offending
    fragment (the password) into the traceback. Passwords routinely
    contain '%', so the config must be read with interpolation disabled.
    """
    monkeypatch.chdir(tmp_path)
    password = "100%-not-a-real-password"
    (tmp_path / ".porscheconnect.cfg").write_text(
        f"[porsche]\nemail = user@example.com\npassword = {password}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["porschecli", "-j", "battery", "-v", "WP0TEST"])
    captured = {}

    async def fake_main(args):
        captured["args"] = args

    monkeypatch.setattr(cli, "main", fake_main)

    cli.cli()

    assert captured["args"].password == password
    assert captured["args"].email == "user@example.com"
