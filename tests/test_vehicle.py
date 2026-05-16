"""Tests for pyporscheconnectapi.vehicle.PorscheVehicle."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from pyporscheconnectapi.connection import Connection
from pyporscheconnectapi.const import API_BASE_URL
from pyporscheconnectapi.vehicle import PorscheVehicle

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "vehicle_overview.json"


@pytest.fixture
def overview_payload() -> dict:
    """Load the canonical /connect/v1/vehicles/{vin}?mf=… response shape."""
    return json.loads(FIXTURE_PATH.read_text())


@pytest.fixture
def api_routes():
    """respx router for the Porsche app API base URL."""
    with respx.mock(base_url=API_BASE_URL, assert_all_called=False) as router:
        yield router


# -- get_stored_overview / get_picture_locations ----------------------------


@pytest.mark.asyncio
async def test_get_stored_overview_populates_vehicle_data(
    authed_connection: Connection, api_routes, overview_payload,
):
    """`get_stored_overview()` parses the measurements list into vehicle.data."""
    api_routes.get(url__regex=r"/connect/v1/vehicles/WP0ZZZY1ZZS000001.*").mock(
        return_value=httpx.Response(200, json=overview_payload),
    )

    vehicle = PorscheVehicle(
        connection=authed_connection,
        data={"vin": "WP0ZZZY1ZZS000001"},
    )
    await vehicle.get_stored_overview()

    # Base data carried through to vehicle.data.
    assert vehicle.data["vin"] == "WP0ZZZY1ZZS000001"
    assert vehicle.data["modelName"] == "Taycan"
    assert vehicle.data["name"] == "My Taycan"

    # Measurements flattened into vehicle.data by key.
    assert vehicle.data["BATTERY_LEVEL"] == {"percent": 73}
    assert vehicle.data["CLIMATIZER_STATE"] == {"isOn": False}
    assert vehicle.data["LOCK_STATE_VEHICLE"] == {"isLocked": True}


@pytest.mark.asyncio
async def test_get_picture_locations_populates_dict(
    authed_connection: Connection, api_routes,
):
    """`get_picture_locations()` indexes pictures by view label."""
    pictures = [
        {"view": "exterior_front", "url": "https://img/ext-front.jpg"},
        {"view": "exterior_rear",  "url": "https://img/ext-rear.jpg"},
    ]
    api_routes.get("/connect/v1/vehicles/VIN1/pictures").mock(
        return_value=httpx.Response(200, json=pictures),
    )

    vehicle = PorscheVehicle(connection=authed_connection, data={"vin": "VIN1"})
    await vehicle.get_picture_locations()

    assert vehicle.picture_locations == {
        "exterior_front": "https://img/ext-front.jpg",
        "exterior_rear":  "https://img/ext-rear.jpg",
    }


# -- VIN / model name derivation -------------------------------------------


def test_vin_and_model_name_defaults(authed_connection: Connection):
    """Empty data returns the sentinel 'not available'."""
    v = PorscheVehicle(connection=authed_connection)
    assert v.vin == "not available"
    assert v.model_name == "not available"


def test_vin_and_model_name_from_data(authed_connection: Connection):
    """data.vin and data.modelName feed the properties."""
    v = PorscheVehicle(
        connection=authed_connection,
        data={"vin": "VIN42", "modelName": "Taycan"},
    )
    assert v.vin == "VIN42"
    assert v.model_name == "Taycan"


# -- Capability properties --------------------------------------------------


@pytest.mark.parametrize(
    ("engine", "expected_electric", "expected_ice"),
    [
        ("BEV",        True,  False),
        ("PHEV",       True,  True),
        ("COMBUSTION", False, True),
    ],
)
def test_drivetrain_capabilities(
    authed_connection: Connection,
    engine: str,
    expected_electric: bool,  # noqa: FBT001
    expected_ice: bool,  # noqa: FBT001
):
    """has_electric_drivetrain / has_ice_drivetrain switch on engine type."""
    v = PorscheVehicle(
        connection=authed_connection,
        data={"modelType": {"engine": engine}},
    )
    assert v.has_electric_drivetrain is expected_electric
    assert v.has_ice_drivetrain is expected_ice


def test_has_remote_climatisation(authed_connection: Connection):
    """has_remote_climatisation flips on the presence of the CLIMATIZER_STATE key."""
    no_climate = PorscheVehicle(connection=authed_connection, data={})
    assert no_climate.has_remote_climatisation is False

    with_climate = PorscheVehicle(
        connection=authed_connection,
        data={"CLIMATIZER_STATE": {"isOn": False}},
    )
    assert with_climate.has_remote_climatisation is True


def test_has_remote_services(authed_connection: Connection):
    """has_remote_services reads REMOTE_ACCESS_AUTHORIZATION.isEnabled."""
    v_off = PorscheVehicle(connection=authed_connection, data={})
    assert v_off.has_remote_services is False

    v_on = PorscheVehicle(
        connection=authed_connection,
        data={"REMOTE_ACCESS_AUTHORIZATION": {"isEnabled": True}},
    )
    assert v_on.has_remote_services is True

    v_disabled = PorscheVehicle(
        connection=authed_connection,
        data={"REMOTE_ACCESS_AUTHORIZATION": {"isEnabled": False}},
    )
    assert v_disabled.has_remote_services is False


def test_has_tire_pressure_monitoring(authed_connection: Connection):
    """has_tire_pressure_monitoring is True iff the TIRE_PRESSURE key exists."""
    v_no_tpm = PorscheVehicle(connection=authed_connection, data={})
    assert v_no_tpm.has_tire_pressure_monitoring is False

    v_tpm = PorscheVehicle(
        connection=authed_connection,
        data={"TIRE_PRESSURE": {"frontLeftTire": {"differenceBar": 0.0}}},
    )
    assert v_tpm.has_tire_pressure_monitoring is True


def test_privacy_mode(authed_connection: Connection):
    """privacy_mode reads GLOBAL_PRIVACY_MODE.isEnabled."""
    v_on = PorscheVehicle(
        connection=authed_connection,
        data={"GLOBAL_PRIVACY_MODE": {"isEnabled": True}},
    )
    assert v_on.privacy_mode is True

    v_off = PorscheVehicle(
        connection=authed_connection,
        data={"GLOBAL_PRIVACY_MODE": {"isEnabled": False}},
    )
    assert v_off.privacy_mode is False
