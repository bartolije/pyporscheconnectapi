"""Tests for pyporscheconnectapi.remote_services.RemoteServices."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from pyporscheconnectapi.connection import Connection
from pyporscheconnectapi.const import API_BASE_URL
from pyporscheconnectapi.exceptions import (
    PorscheExceptionError,
    PorscheRemoteServiceError,
)
from pyporscheconnectapi.remote_services import ExecutionState
from pyporscheconnectapi.vehicle import PorscheVehicle

VIN = "WP0ZZZTESTVIN0001"
STATUS_ID = "status-id-1"

# An ACCEPTED response triggers _block_until_done — which then polls
# /commands/{status_id} until it sees a terminal state.
ACCEPTED_RESPONSE = {
    "status": {"id": STATUS_ID, "result": "ACCEPTED"},
}
PERFORMED_STATUS = {
    "status": {"id": STATUS_ID, "result": "PERFORMED"},
}


@pytest.fixture
def api_routes():
    """respx router for the Porsche app API base URL."""
    with respx.mock(base_url=API_BASE_URL, assert_all_called=False) as router:
        # _send_command trails with vehicle.get_stored_overview() —
        # provide a no-op-ish response so the GET doesn't blow up
        # _update_vehicle_data (which needs the full BASE_DATA set).
        router.get(url__regex=rf".*/connect/v1/vehicles/{VIN}\?.*").mock(
            return_value=httpx.Response(
                200,
                json={
                    "vin": VIN,
                    "modelName": "Taycan",
                    "modelType": {"engine": "BEV", "year": "2023"},
                    "systemInfo": {},
                    "timestamp": "2024-01-01T12:00:00Z",
                    "measurements": [],
                },
            ),
        )
        # _block_until_done polls /commands/{status_id} after ACCEPTED.
        router.get(f"/connect/v1/vehicles/{VIN}/commands/{STATUS_ID}").mock(
            return_value=httpx.Response(200, json=PERFORMED_STATUS),
        )
        yield router


def _make_vehicle(connection: Connection, **extra_data) -> PorscheVehicle:
    """Build a PorscheVehicle with the test VIN and any extra data."""
    data = {"vin": VIN, **extra_data}
    return PorscheVehicle(connection=connection, data=data)


async def _drain_polling_sleep(monkeypatch):
    """Make asyncio.sleep instant so PERFORMED tests don't pause 1s each."""
    real_sleep = asyncio.sleep

    async def _fast_sleep(seconds, *a, **kw):
        await real_sleep(0)

    monkeypatch.setattr("pyporscheconnectapi.remote_services.asyncio.sleep", _fast_sleep)


# -- Happy paths -----------------------------------------------------------


@pytest.mark.asyncio
async def test_climatise_on_posts_remote_climatizer_start(
    authed_connection: Connection, api_routes, monkeypatch,
):
    """`climatise_on()` POSTs key=REMOTE_CLIMATIZER_START with climate zones."""
    await _drain_polling_sleep(monkeypatch)
    route = api_routes.post(f"/connect/v1/vehicles/{VIN}/commands").mock(
        return_value=httpx.Response(200, json=ACCEPTED_RESPONSE),
    )

    vehicle = _make_vehicle(authed_connection)
    status = await vehicle.remote_services.climatise_on(target_temperature=294.15)

    assert status.state == ExecutionState.PERFORMED
    body = json.loads(route.calls.last.request.content)
    assert body["key"] == "REMOTE_CLIMATIZER_START"
    assert body["payload"]["targetTemperature"] == 294.15
    assert body["payload"]["climateZonesEnabled"] == {
        "frontLeft": False, "frontRight": False,
        "rearLeft":  False, "rearRight":  False,
    }


@pytest.mark.asyncio
async def test_climatise_off_posts_remote_climatizer_stop(
    authed_connection: Connection, api_routes, monkeypatch,
):
    await _drain_polling_sleep(monkeypatch)
    route = api_routes.post(f"/connect/v1/vehicles/{VIN}/commands").mock(
        return_value=httpx.Response(200, json=ACCEPTED_RESPONSE),
    )

    vehicle = _make_vehicle(authed_connection)
    status = await vehicle.remote_services.climatise_off()

    assert status.state == ExecutionState.PERFORMED
    body = json.loads(route.calls.last.request.content)
    assert body["key"] == "REMOTE_CLIMATIZER_STOP"
    assert body["payload"] == {}


@pytest.mark.asyncio
async def test_lock_vehicle_posts_lock(
    authed_connection: Connection, api_routes, monkeypatch,
):
    await _drain_polling_sleep(monkeypatch)
    route = api_routes.post(f"/connect/v1/vehicles/{VIN}/commands").mock(
        return_value=httpx.Response(200, json=ACCEPTED_RESPONSE),
    )

    vehicle = _make_vehicle(authed_connection)
    status = await vehicle.remote_services.lock_vehicle()

    assert status.state == ExecutionState.PERFORMED
    body = json.loads(route.calls.last.request.content)
    assert body["key"] == "LOCK"


@pytest.mark.asyncio
async def test_unlock_vehicle_round_trips_challenge_then_unlock(
    authed_connection: Connection, api_routes, monkeypatch,
):
    """unlock_vehicle posts SPIN_CHALLENGE, then UNLOCK with the hashed pin."""
    await _drain_polling_sleep(monkeypatch)

    # The route fires twice — first for SPIN_CHALLENGE, then for UNLOCK.
    route = api_routes.post(f"/connect/v1/vehicles/{VIN}/commands").mock(
        side_effect=[
            httpx.Response(200, json={"data": {"challenge": "abcdef"}}),
            httpx.Response(200, json=ACCEPTED_RESPONSE),
        ],
    )

    vehicle = _make_vehicle(authed_connection)
    status = await vehicle.remote_services.unlock_vehicle(pin="1234")

    assert status.state == ExecutionState.PERFORMED
    assert route.call_count == 2

    challenge_body = json.loads(route.calls[0].request.content)
    unlock_body = json.loads(route.calls[1].request.content)

    assert challenge_body["key"] == "SPIN_CHALLENGE"
    assert unlock_body["key"] == "UNLOCK"
    spin = unlock_body["payload"]["spin"]
    assert spin["challenge"] == "abcdef"
    # SHA-512 of bytes.fromhex("1234abcdef") — 128 hex chars, uppercased.
    assert len(spin["hash"]) == 128
    assert spin["hash"].isupper()


@pytest.mark.asyncio
async def test_unlock_vehicle_missing_pin_raises(
    authed_connection: Connection,
):
    """Calling unlock_vehicle() with no pin must raise — TypeError under the hood."""
    vehicle = _make_vehicle(authed_connection)
    with pytest.raises(TypeError):
        await vehicle.remote_services.unlock_vehicle()  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_flash_indicators_posts_honk_flash(
    authed_connection: Connection, api_routes, monkeypatch,
):
    await _drain_polling_sleep(monkeypatch)
    route = api_routes.post(f"/connect/v1/vehicles/{VIN}/commands").mock(
        return_value=httpx.Response(200, json=ACCEPTED_RESPONSE),
    )

    vehicle = _make_vehicle(authed_connection)
    await vehicle.remote_services.flash_indicators()

    body = json.loads(route.calls.last.request.content)
    assert body["key"] == "HONK_FLASH"
    assert body["payload"]["mode"] == "FLASH"


@pytest.mark.asyncio
async def test_honk_and_flash_indicators_posts_honk_and_flash(
    authed_connection: Connection, api_routes, monkeypatch,
):
    await _drain_polling_sleep(monkeypatch)
    route = api_routes.post(f"/connect/v1/vehicles/{VIN}/commands").mock(
        return_value=httpx.Response(200, json=ACCEPTED_RESPONSE),
    )

    vehicle = _make_vehicle(authed_connection)
    await vehicle.remote_services.honk_and_flash_indicators()

    body = json.loads(route.calls.last.request.content)
    assert body["key"] == "HONK_FLASH"
    assert body["payload"]["mode"] == "HONK_AND_FLASH"


# -- set_target_soc --------------------------------------------------------


@pytest.mark.asyncio
async def test_set_target_soc_uses_charging_settings_when_departures_set(
    authed_connection: Connection, api_routes, monkeypatch,
):
    """If DEPARTURES is present in data, set_target_soc → CHARGING_SETTINGS_EDIT."""
    await _drain_polling_sleep(monkeypatch)
    route = api_routes.post(f"/connect/v1/vehicles/{VIN}/commands").mock(
        return_value=httpx.Response(200, json=ACCEPTED_RESPONSE),
    )

    vehicle = _make_vehicle(authed_connection, DEPARTURES={"list": []})
    await vehicle.remote_services.set_target_soc(80)

    body = json.loads(route.calls.last.request.content)
    assert body["key"] == "CHARGING_SETTINGS_EDIT"
    assert body["payload"]["targetSoc"] == 80


@pytest.mark.asyncio
async def test_set_target_soc_clamps_low_and_high_values(
    authed_connection: Connection, api_routes, monkeypatch,
):
    """update_charging_setting clamps targetSoc to [25, 100]."""
    await _drain_polling_sleep(monkeypatch)
    route = api_routes.post(f"/connect/v1/vehicles/{VIN}/commands").mock(
        return_value=httpx.Response(200, json=ACCEPTED_RESPONSE),
    )

    vehicle = _make_vehicle(authed_connection, DEPARTURES={"list": []})
    await vehicle.remote_services.set_target_soc(5)
    body = json.loads(route.calls.last.request.content)
    assert body["payload"]["targetSoc"] == 25

    await vehicle.remote_services.set_target_soc(999)
    body = json.loads(route.calls.last.request.content)
    assert body["payload"]["targetSoc"] == 100


@pytest.mark.asyncio
async def test_set_target_soc_uses_charging_profiles_when_no_departures(
    authed_connection: Connection, api_routes, monkeypatch,
):
    """If DEPARTURES is absent, set_target_soc → CHARGING_PROFILES_EDIT.

    Note: this exercises the path called out in known issue #319 (charging
    profiles). The current behavior mutates the active profile's minSoc and
    re-POSTs the whole list — we lock in *that* contract, not the eventual fix.
    """
    await _drain_polling_sleep(monkeypatch)
    route = api_routes.post(f"/connect/v1/vehicles/{VIN}/commands").mock(
        return_value=httpx.Response(200, json=ACCEPTED_RESPONSE),
    )

    vehicle = _make_vehicle(
        authed_connection,
        CHARGING_PROFILES={
            "list": [
                {"id": 1, "isEnabled": False, "minSoc": 40},
                {"id": 2, "isEnabled": True,  "minSoc": 50},
            ],
        },
    )
    await vehicle.remote_services.set_target_soc(75)

    body = json.loads(route.calls.last.request.content)
    assert body["key"] == "CHARGING_PROFILES_EDIT"
    # The active profile (id=2) got its minSoc rewritten.
    active = next(p for p in body["payload"]["list"] if p["id"] == 2)
    assert active["minSoc"] == 75
    # The inactive profile is untouched.
    inactive = next(p for p in body["payload"]["list"] if p["id"] == 1)
    assert inactive["minSoc"] == 40


# -- Failure paths ---------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_service_raises_when_response_is_null(
    authed_connection: Connection, api_routes, monkeypatch,
):
    """A JSON-null response body from /commands → PorscheRemoteServiceError.

    `resp.json()` returns Python `None`, which the lib's `if response:` guard
    treats as a failure and raises PorscheRemoteServiceError.
    """
    await _drain_polling_sleep(monkeypatch)
    api_routes.post(f"/connect/v1/vehicles/{VIN}/commands").mock(
        return_value=httpx.Response(200, content=b"null"),
    )

    vehicle = _make_vehicle(authed_connection)
    with pytest.raises(PorscheRemoteServiceError):
        await vehicle.remote_services.flash_indicators()


@pytest.mark.asyncio
async def test_remote_service_propagates_http_error(
    authed_connection: Connection, api_routes,
):
    """A 500 from the API surfaces as PorscheExceptionError."""
    api_routes.post(f"/connect/v1/vehicles/{VIN}/commands").mock(
        return_value=httpx.Response(500, json={"err": "boom"}),
    )

    vehicle = _make_vehicle(authed_connection)
    with pytest.raises(PorscheExceptionError) as exc_info:
        await vehicle.remote_services.flash_indicators()
    assert exc_info.value.code == 500
