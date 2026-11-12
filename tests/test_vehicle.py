"""Tests for pyporscheconnectapi.vehicle.PorscheVehicle."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import httpx
import pytest
import respx

import pyporscheconnectapi
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
    authed_connection: Connection,
    api_routes,
    overview_payload,
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
    assert vehicle.data["HVAC_SUMMARY"]["status"] == "OFF"
    assert vehicle.data["LOCK_STATE_VEHICLE"] == {"isLocked": True}


@pytest.mark.asyncio
async def test_get_picture_locations_populates_dict(
    authed_connection: Connection,
    api_routes,
):
    """`get_picture_locations()` indexes pictures by view label."""
    pictures = [
        {"view": "exterior_front", "url": "https://img/ext-front.jpg"},
        {"view": "exterior_rear", "url": "https://img/ext-rear.jpg"},
    ]
    api_routes.get("/connect/v1/vehicles/VIN1/pictures").mock(
        return_value=httpx.Response(200, json=pictures),
    )

    vehicle = PorscheVehicle(connection=authed_connection, data={"vin": "VIN1"})
    await vehicle.get_picture_locations()

    assert vehicle.picture_locations == {
        "exterior_front": "https://img/ext-front.jpg",
        "exterior_rear": "https://img/ext-rear.jpg",
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
        ("BEV", True, False),
        ("PHEV", True, True),
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
    """Climatisation is read from HVAC_SUMMARY (CLIMATIZER_STATE no longer reports it)."""
    no_climate = PorscheVehicle(connection=authed_connection, data={})
    assert no_climate.has_remote_climatisation is False
    assert no_climate.remote_climatise_on is False
    assert no_climate.seat_heating is None

    legacy = PorscheVehicle(
        connection=authed_connection,
        data={"CLIMATIZER_STATE": {"isOn": True}},
    )
    assert legacy.has_remote_climatisation is False

    zones = {"frontLeft": True, "frontRight": False}
    with_climate = PorscheVehicle(
        connection=authed_connection,
        data={"HVAC_SUMMARY": {"status": "OFF", "climateZonesEnabled": zones}},
    )
    assert with_climate.has_remote_climatisation is True
    assert with_climate.remote_climatise_on is False
    assert with_climate.seat_heating == zones

    running = PorscheVehicle(
        connection=authed_connection,
        data={"HVAC_SUMMARY": {"status": "ON"}},
    )
    assert running.remote_climatise_on is True

    for status, expected in (("HEATING", True), ("COOLING", True), ("ERROR", False)):
        v = PorscheVehicle(connection=authed_connection, data={"HVAC_SUMMARY": {"status": status}})
        assert v.remote_climatise_on is expected, status


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
    authed_connection: Connection,
    api_routes,
    combustion_payload,
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
    authed_connection: Connection,
    api_routes,
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
        connection=authed_connection,
        data={"vin": "WP0ZZZ98ZZS000002"},
    )
    await vehicle.get_stored_overview()

    assert vehicle.mileage == 100
    assert "systemInfo" not in vehicle.data


@pytest.mark.asyncio
async def test_enabled_measurement_without_value_is_skipped(
    authed_connection: Connection,
    api_routes,
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
        connection=authed_connection,
        data={"TIRE_PRESSURE": {"lastModified": "x"}},
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
    authed_connection: Connection,
    api_routes,
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


def test_tire_pressures_from_per_corner_measurements(authed_connection: Connection):
    """Cars that only report TIRE_PRESSURE_* still get readings, status and monitoring."""
    v = PorscheVehicle(
        connection=authed_connection,
        data={
            "TIRE_PRESSURE_FRONT_LEFT": {"differenceBar": -0.1, "actualPressureBar": 2.4},
            "TIRE_PRESSURE_REAR_RIGHT": {"differenceBar": -0.6, "actualPressureBar": 2.0},
        },
    )
    assert v.tire_pressures == {
        "frontLeftTire": {"differenceBar": -0.1, "actualPressureBar": 2.4},
        "rearRightTire": {"differenceBar": -0.6, "actualPressureBar": 2.0},
    }
    assert v.has_tire_pressure_monitoring is True
    assert v.tire_pressure_status is False


def test_tire_pressures_prefer_legacy_readings(authed_connection: Connection):
    """The legacy measurement wins when it has per-tyre readings."""
    legacy = {"frontLeftTire": {"differenceBar": 0.0}, "lastModified": "x"}
    v = PorscheVehicle(
        connection=authed_connection,
        data={"TIRE_PRESSURE": legacy, "TIRE_PRESSURE_FRONT_LEFT": {"differenceBar": -0.5}},
    )
    assert v.tire_pressures == legacy
    assert v.tire_pressure_status is True


def test_climatisation_target_and_remaining_time(authed_connection: Connection):
    """HVAC_SUMMARY reports the target in Kelvin and the remaining minutes."""
    v = PorscheVehicle(
        connection=authed_connection,
        data={"HVAC_SUMMARY": {"status": "ON", "targetTemperature": 294.15, "remainingTime": 12}},
    )
    assert v.climatisation_target_temperature == 21.0
    assert v.climatisation_remaining_time == 12
    empty = PorscheVehicle(connection=authed_connection, data={})
    assert empty.climatisation_target_temperature is None
    assert empty.climatisation_remaining_time is None


def test_last_global_update(authed_connection: Connection):
    """GLOBAL_TIMESTAMP tells when the car last sent its data."""
    v = PorscheVehicle(
        connection=authed_connection,
        data={"GLOBAL_TIMESTAMP": {"timestamp": "2026-11-06T06:30:00Z"}},
    )
    assert v.last_global_update.isoformat() == "2026-11-06T06:30:00+00:00"
    assert PorscheVehicle(connection=authed_connection, data={}).last_global_update is None


@pytest.mark.asyncio
async def test_disabled_measurement_drops_its_last_value(
    authed_connection: Connection,
    api_routes,
):
    """A measurement the API now reports disabled must not keep its old value;
    one merely absent from the payload keeps it.
    """
    payload = {
        "vin": "VIN_PRIV",
        "measurements": [
            {"key": "GPS_LOCATION", "status": {"isEnabled": False}},
            {"key": "BROKEN"},
        ],
    }
    api_routes.get(url__regex=r"/connect/v1/vehicles/VIN_PRIV.*").mock(
        return_value=httpx.Response(200, json=payload),
    )

    vehicle = PorscheVehicle(
        connection=authed_connection,
        vin="VIN_PRIV",
        data={"GPS_LOCATION": {"location": "1.0,2.0"}, "MILEAGE": {"kilometers": 5}},
    )
    await vehicle.get_stored_overview()

    assert vehicle.vin == "VIN_PRIV"
    assert "GPS_LOCATION" not in vehicle.data
    assert vehicle.data["MILEAGE"] == {"kilometers": 5}
    assert vehicle.data["name"] == "not available"


def test_doors_and_lids_tolerate_missing_is_open(authed_connection: Connection):
    vehicle = PorscheVehicle(
        connection=authed_connection,
        data={"OPEN_STATE_LID_REAR": {"isOpen": True}, "OPEN_STATE_TOP": {}},
    )
    assert vehicle.vehicle_closed is False
    assert vehicle.doors_and_lids == {"OPEN_STATE_LID_REAR": "Open", "OPEN_STATE_TOP": "Closed"}
    assert vehicle.connected is None


# -- Charging data normalisation -------------------------------------------


def _measurement(key: str, value: dict) -> dict:
    return {"key": key, "status": {"isEnabled": True}, "value": value}


@pytest.mark.parametrize(
    ("measurements", "expected_min_soc"),
    [
        # The active target SoC wins over everything else.
        ({"CHARGING_SUMMARY": {"mode": "PROFILE", "targetSoC": 85, "chargingProfile": {"minSoC": 40}}}, 85),
        ({"CHARGING_SUMMARY": {"mode": "PROFILE", "chargingProfile": {"minSoC": 40}}}, 40),
        # PROFILE mode without the profile: no value, no KeyError.
        ({"CHARGING_SUMMARY": {"mode": "PROFILE"}}, None),
        ({"CHARGING_SUMMARY": {"mode": "DIRECT"}, "DEPARTURES": {"list": []}, "CHARGING_SETTINGS": {"targetSoc": 70}}, 70),
        # Direct charging reports no minSoC: it charges to full.
        ({"CHARGING_SUMMARY": {"mode": "DIRECT"}}, 100),
    ],
)
async def test_charging_target_precedence(
    authed_connection: Connection,
    api_routes,
    measurements,
    expected_min_soc,
):
    payload = {
        "vin": "VIN_PHEV",
        "modelName": "Cayenne E-Hybrid",
        "modelType": {"engine": "PHEV", "year": "2025"},
        "measurements": [_measurement(k, v) for k, v in measurements.items()],
    }
    api_routes.get(url__regex=r"/connect/v1/vehicles/VIN_PHEV.*").mock(
        return_value=httpx.Response(200, json=payload),
    )
    vehicle = PorscheVehicle(connection=authed_connection, vin="VIN_PHEV")

    await vehicle.get_stored_overview()

    assert vehicle.charging_target == expected_min_soc
    assert vehicle.has_electric_drivetrain
    assert vehicle.has_ice_drivetrain
    assert vehicle.main_battery_level == 0  # only reported for a pure BEV
    assert vehicle.model_year == "2025"
    assert "PHEV" in repr(vehicle)


async def test_charging_rate_and_target_time(authed_connection: Connection, api_routes):
    payload = {
        "vin": "VIN_BEV",
        "modelName": "Taycan",
        "measurements": [
            _measurement("CHARGING_RATE", {"chargingRate": 2.5, "chargingPower": 11}),
            _measurement("CHARGING_SUMMARY", {"mode": "DIRECT", "targetDateTimeWithOffset": "2026-01-02T03:04:00Z"}),
            _measurement("GPS_LOCATION", {"location": "48.85,2.35", "direction": 90, "lastModified": "2026-01-01T00:00:00Z"}),
        ],
    }
    route = api_routes.get(url__regex=r"/connect/v1/vehicles/VIN_BEV.*").mock(
        return_value=httpx.Response(200, json=payload),
    )
    vehicle = PorscheVehicle(connection=authed_connection, vin="VIN_BEV")

    await vehicle.get_current_overview()

    assert "wakeUpJob=" in str(route.calls.last.request.url)
    assert vehicle.data["CHARGING_RATE"]["chargingRate-kph"] == 150
    assert vehicle.data["CHARGING_SUMMARY"]["targetDateTimeWithOffset"].year == 2026
    assert vehicle.direct_charge_on
    assert vehicle.location == (48.85, 2.35, 90)
    assert vehicle.location_updated_at is not None
    assert vehicle.get_data() is vehicle.data


async def test_idle_charging_rate_is_zeroed(authed_connection: Connection, api_routes):
    payload = {
        "vin": "VIN_IDLE",
        "measurements": [
            _measurement("CHARGING_RATE", {"chargingRate": 0}),
            _measurement("CHARGING_SUMMARY", {"mode": "DIRECT"}),
        ],
    }
    api_routes.get(url__regex=r"/connect/v1/vehicles/VIN_IDLE.*").mock(
        return_value=httpx.Response(200, json=payload),
    )
    vehicle = PorscheVehicle(connection=authed_connection, vin="VIN_IDLE")

    await vehicle.get_stored_overview()

    assert vehicle.data["CHARGING_RATE"] == {"chargingRate": 0, "chargingRate-kph": 0, "chargingPower": 0}
    assert vehicle.data["CHARGING_SUMMARY"]["targetDateTimeWithOffset"] is None
    assert vehicle.location == (None, None, None)
    assert vehicle.location_updated_at is None


@pytest.mark.parametrize("payload", [{}, {"vin": "VIN_BARE"}])
async def test_overview_without_base_data_or_measurements_keeps_data(
    authed_connection: Connection,
    api_routes,
    payload,
):
    api_routes.get(url__regex=r"/connect/v1/vehicles/VIN_BARE.*").mock(
        return_value=httpx.Response(200, json=payload),
    )
    vehicle = PorscheVehicle(connection=authed_connection, vin="VIN_BARE", data={"MILEAGE": {"kilometers": 5}})

    await vehicle.get_stored_overview()

    assert vehicle.mileage == 5


async def test_capabilities_and_trip_statistics_are_stored(authed_connection: Connection, api_routes):
    route = api_routes.get(url__regex=r"/connect/v1/vehicles/VIN_CAP.*").mock(
        return_value=httpx.Response(200, json={"vin": "VIN_CAP"}),
    )
    vehicle = PorscheVehicle(connection=authed_connection, vin="VIN_CAP")

    await vehicle.get_capabilities()
    assert "cf=LOCK" in str(route.calls.last.request.url)
    await vehicle.get_trip_statistics()
    assert "mf=TRIP_STATISTICS_CYCLIC" in str(route.calls.last.request.url)

    assert vehicle.capabilities == {"vin": "VIN_CAP"}
    assert vehicle.trip_statistics == {"vin": "VIN_CAP"}


@pytest.mark.parametrize(
    ("contract", "expected"),
    [(None, True), ({"isEnabled": True, "until": "2030-06-03T12:32:04Z"}, True), ({"isEnabled": False}, False)],
)
def test_has_porsche_connect_follows_the_contract(authed_connection: Connection, contract, expected):
    data = {} if contract is None else {"CONNECT_CONTRACT": contract}
    assert PorscheVehicle(connection=authed_connection, data=data).has_porsche_connect is expected


def test_vehicle_locked_and_closed_defaults(authed_connection: Connection):
    vehicle = PorscheVehicle(connection=authed_connection, data={"LOCK_STATE_VEHICLE": {"isLocked": True}})
    assert vehicle.vehicle_locked
    assert vehicle.vehicle_closed


def test_package_version_matches_pyproject():
    project = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text())["project"]
    assert pyporscheconnectapi.__version__ == project["version"]
