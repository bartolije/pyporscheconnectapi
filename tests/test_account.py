"""Tests for pyporscheconnectapi.account.PorscheConnectAccount."""
from __future__ import annotations

import httpx
import pytest
import respx

from pyporscheconnectapi.account import PorscheConnectAccount
from pyporscheconnectapi.connection import Connection
from pyporscheconnectapi.const import API_BASE_URL
from pyporscheconnectapi.exceptions import PorscheExceptionError
from pyporscheconnectapi.vehicle import PorscheVehicle


@pytest.fixture
def api_routes():
    """respx router for the Porsche app API base URL."""
    with respx.mock(base_url=API_BASE_URL, assert_all_called=False) as router:
        yield router


@pytest.mark.asyncio
async def test_get_vehicles_parses_payload_into_porsche_vehicles(
    authed_connection: Connection, api_routes,
):
    """`get_vehicles()` calls /connect/v1/vehicles and parses each entry."""
    payload = [
        {"vin": "WP0ZZZY1ZZS000001", "modelName": "Taycan"},
        {"vin": "WP0ZZZ99ZTS000002", "modelName": "911"},
    ]
    api_routes.get("/connect/v1/vehicles").mock(
        return_value=httpx.Response(200, json=payload),
    )

    account = PorscheConnectAccount(connection=authed_connection)
    vehicles = await account.get_vehicles()

    assert len(vehicles) == 2
    assert all(isinstance(v, PorscheVehicle) for v in vehicles)
    assert {v.vin for v in vehicles} == {"WP0ZZZY1ZZS000001", "WP0ZZZ99ZTS000002"}
    assert vehicles[0].connection is authed_connection


@pytest.mark.asyncio
async def test_get_vehicles_caches_after_first_call(
    authed_connection: Connection, api_routes,
):
    """A second call doesn't refetch unless force_init=True."""
    route = api_routes.get("/connect/v1/vehicles").mock(
        return_value=httpx.Response(200, json=[{"vin": "V1", "modelName": "Taycan"}]),
    )

    account = PorscheConnectAccount(connection=authed_connection)
    await account.get_vehicles()
    await account.get_vehicles()

    assert route.call_count == 1

    await account.get_vehicles(force_init=True)
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_get_vehicle_resolves_specific_vin(
    authed_connection: Connection, api_routes,
):
    """`get_vehicle(vin)` returns the matching PorscheVehicle (or None)."""
    api_routes.get("/connect/v1/vehicles").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"vin": "V1", "modelName": "Taycan"},
                {"vin": "V2", "modelName": "Macan"},
            ],
        ),
    )

    account = PorscheConnectAccount(connection=authed_connection)
    v = await account.get_vehicle("V2")
    assert v is not None
    assert v.vin == "V2"
    assert v.model_name == "Macan"

    missing = await account.get_vehicle("V404")
    assert missing is None


@pytest.mark.asyncio
async def test_get_vehicles_propagates_auth_failure(
    authed_connection: Connection, api_routes,
):
    """A 401 from the API surfaces as PorscheExceptionError."""
    api_routes.get("/connect/v1/vehicles").mock(
        return_value=httpx.Response(401, json={"error": "unauthorized"}),
    )

    account = PorscheConnectAccount(connection=authed_connection)
    with pytest.raises(PorscheExceptionError) as exc_info:
        await account.get_vehicles()
    assert exc_info.value.code == 401
    assert exc_info.value.message == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_account_creates_own_connection_when_none_passed():
    """If no Connection is provided, the account builds one from creds."""
    account = PorscheConnectAccount(username="u@x", password="pw")
    try:
        assert isinstance(account.connection, Connection)
        assert account.vehicles == []
    finally:
        await account.connection.close()
