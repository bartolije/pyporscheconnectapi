"""Tests for pyporscheconnectapi.vehicle.PorscheVehicle."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from pyporscheconnectapi.connection import Connection
from pyporscheconnectapi.const import API_BASE_URL
from pyporscheconnectapi.exceptions import PorscheExceptionError
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


# -- combustion (ICE) vehicle parsing & properties --------------------------

COMBUSTION_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "vehicle_overview_combustion.json"


@pytest.fixture
def combustion_payload() -> dict:
    """Load a real-world combustion (718 Cayman GT4 RS) overview shape."""
    return json.loads(COMBUSTION_FIXTURE_PATH.read_text())


@pytest.mark.asyncio
async def test_combustion_overview_exposes_ice_properties(
    authed_connection: Connection, api_routes, combustion_payload,
):
    """ICE/common measurements are parsed into dedicated properties."""
    api_routes.get(url__regex=r"/connect/v1/vehicles/WP0ZZZ98ZZS000002.*").mock(
        return_value=httpx.Response(200, json=combustion_payload),
    )

    vehicle = PorscheVehicle(
        connection=authed_connection,
        data={"vin": "WP0ZZZ98ZZS000002"},
    )
    await vehicle.get_stored_overview()

    assert vehicle.has_ice_drivetrain is True
    assert vehicle.has_electric_drivetrain is False
    assert vehicle.mileage == 14084
    assert vehicle.remaining_range == 351
    assert vehicle.fuel_level == 64
    assert vehicle.fuel_reserve == 15
    # E_RANGE is NOT_SUPPORTED on a combustion car → dropped → None.
    assert vehicle.electric_range is None
    # BEV-only properties degrade gracefully on an ICE car.
    assert vehicle.main_battery_level == 0
    assert vehicle.charging_target is None


def test_service_intervals(authed_connection: Connection):
    """service_intervals groups range/time per service type, None when absent."""
    vehicle = PorscheVehicle(
        connection=authed_connection,
        data={
            "MAIN_SERVICE_RANGE": {"kilometers": 14900},
            "MAIN_SERVICE_TIME": {"days": 564},
            "OIL_SERVICE_RANGE": {"kilometers": 14900},
            # No OIL_SERVICE_TIME, no INTERMEDIATE_* at all.
        },
    )
    intervals = vehicle.service_intervals
    assert intervals["main"] == {"kilometers": 14900, "days": 564}
    assert intervals["oil"] == {"kilometers": 14900, "days": None}
    assert intervals["intermediate"] == {"kilometers": None, "days": None}


# -- parsing robustness regressions -----------------------------------------


@pytest.mark.asyncio
async def test_overview_with_missing_base_fields_does_not_crash(
    authed_connection: Connection, api_routes,
):
    """A payload lacking systemInfo/timestamp must not KeyError the parse."""
    payload = {
        "vin": "WP0ZZZ98ZZS000002",
        "modelName": "718 Cayman",
        "modelType": {"engine": "COMBUSTION", "year": "2022"},
        # systemInfo and timestamp deliberately absent.
        "measurements": [
            {"key": "MILEAGE", "status": {"isEnabled": True}, "value": {"kilometers": 100}},
        ],
    }
    api_routes.get(url__regex=r"/connect/v1/vehicles/WP0ZZZ98ZZS000002.*").mock(
        return_value=httpx.Response(200, json=payload),
    )

    vehicle = PorscheVehicle(
        connection=authed_connection, data={"vin": "WP0ZZZ98ZZS000002"},
    )
    await vehicle.get_stored_overview()

    assert vehicle.mileage == 100
    assert "systemInfo" not in vehicle.data


@pytest.mark.asyncio
async def test_enabled_measurement_without_value_is_skipped(
    authed_connection: Connection, api_routes,
):
    """An enabled measurement that omits 'value' must not KeyError."""
    payload = {
        "vin": "VIN_NOVALUE",
        "modelName": "Taycan",
        "measurements": [
            {"key": "BATTERY_LEVEL", "status": {"isEnabled": True}},  # no "value"
            {"key": "MILEAGE", "status": {"isEnabled": True}, "value": {"kilometers": 5}},
        ],
    }
    api_routes.get(url__regex=r"/connect/v1/vehicles/VIN_NOVALUE.*").mock(
        return_value=httpx.Response(200, json=payload),
    )

    vehicle = PorscheVehicle(connection=authed_connection, data={"vin": "VIN_NOVALUE"})
    await vehicle.get_stored_overview()

    assert "BATTERY_LEVEL" not in vehicle.data
    assert vehicle.data["MILEAGE"] == {"kilometers": 5}


def test_tire_pressure_status_handles_empty(authed_connection: Connection):
    """tire_pressure_status returns True (OK) when no per-tire readings exist."""
    v_empty = PorscheVehicle(
        connection=authed_connection, data={"TIRE_PRESSURE": {"lastModified": "x"}},
    )
    assert v_empty.tire_pressure_status is True

    v_out = PorscheVehicle(
        connection=authed_connection,
        data={"TIRE_PRESSURE": {"frontLeftTire": {"differenceBar": -0.5}}},
    )
    assert v_out.tire_pressure_status is False

    v_ok = PorscheVehicle(
        connection=authed_connection,
        data={"TIRE_PRESSURE": {"frontLeftTire": {"differenceBar": -0.1}}},
    )
    assert v_ok.tire_pressure_status is True


@pytest.mark.asyncio
async def test_get_stored_overview_propagates_api_error(
    authed_connection: Connection, api_routes,
):
    """get_stored_overview now re-raises PorscheExceptionError (no longer swallowed)."""
    api_routes.get(url__regex=r"/connect/v1/vehicles/VINERR.*").mock(
        return_value=httpx.Response(500, json={}),
    )
    vehicle = PorscheVehicle(connection=authed_connection, data={"vin": "VINERR"})
    with pytest.raises(PorscheExceptionError):
        await vehicle.get_stored_overview()


def test_drivetrain_capabilities_missing_modeltype(authed_connection: Connection):
    """has_* drivetrain helpers return False (no KeyError) when modelType is absent."""
    v = PorscheVehicle(connection=authed_connection, data={})
    assert v.has_electric_drivetrain is False
    assert v.has_ice_drivetrain is False
    assert v.has_direct_charge is False
