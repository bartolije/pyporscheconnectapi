"""Models the state of a Porsche Connect vehicle."""

from __future__ import annotations

import datetime
import json  # only for formatting debug output
import logging
import re
import uuid

from pyporscheconnectapi.connection import Connection
from pyporscheconnectapi.remote_services import RemoteServices

from .const import COMMANDS, MEASUREMENTS, TIRE_PRESSURE_TOLERANCE, TRIP_STATISTICS

_LOGGER = logging.getLogger(__name__)

BASE_DATA = ["vin", "modelName", "modelType", "systemInfo", "timestamp"]

# Per-corner tyre measurements of the current API, mapped to the keys of the
# legacy TIRE_PRESSURE measurement that some cars still report.
_TIRE_CORNERS = {
    "TIRE_PRESSURE_FRONT_LEFT": "frontLeftTire",
    "TIRE_PRESSURE_FRONT_RIGHT": "frontRightTire",
    "TIRE_PRESSURE_REAR_LEFT": "rearLeftTire",
    "TIRE_PRESSURE_REAR_RIGHT": "rearRightTire",
}


def _parse_porsche_datetime(datetime_str: str) -> datetime.datetime:
    """Parse Porsche API timestamps, including UTC timestamps with a trailing Z."""
    if datetime_str.endswith("Z"):
        datetime_str = datetime_str[:-1] + "+00:00"
    return datetime.datetime.fromisoformat(datetime_str)


class PorscheVehicle:
    """Representation of a Porsche Connect vehicle."""

    def __init__(
        self,
        connection: Connection,
        vin: str | None = None,
        data: dict | None = None,
        status: dict | None = None,
        capabilities: dict | None = None,
        trip_statistics: dict | None = None,
        picture_locations: dict | None = None,
    ) -> None:
        """Initialise the Porsche Vehicle."""
        if picture_locations is None:
            picture_locations = {}
        if trip_statistics is None:
            trip_statistics = {}
        if capabilities is None:
            capabilities = {}
        if status is None:
            status = {}
        if data is None:
            data = {}
        if vin:
            data.setdefault("vin", vin)
        self.connection = connection
        self.data = data
        self.remote_services = RemoteServices(self)
        self.status = status
        self.capabilities = capabilities
        self.trip_statistics = trip_statistics
        self.picture_locations = picture_locations

    def get_data(self) -> dict:
        """Get all vechicle data."""
        return self.data

    @property
    def vin(self) -> str:
        """Get the VIN (vehicle identification number) of the vehicle."""
        return self.data.get("vin", "not available")

    @property
    def model_name(self) -> str:
        """Get the model name of the vehicle."""
        return self.data.get("modelName", "not available")

    @property
    def model_year(self) -> str:
        """Get the model year of the vehicle."""
        return self.data.get("modelType", {}).get("year", "not available")

    @property
    def has_porsche_connect(self) -> bool:
        """Should return true if porsche connect subscription is active. Todo."""
        return True

    @property
    def connected(self) -> bool | None:
        """Return True if the vehicle is on-line, None when the API does not say."""
        return self.data.get("connect")

    @property
    def has_remote_services(self) -> bool:
        """Return true if remote services are available."""
        return self.data.get("REMOTE_ACCESS_AUTHORIZATION", {}).get("isEnabled") is True

    @property
    def has_electric_drivetrain(self) -> bool:
        """Return True if vehicle is equipped with a high voltage battery."""
        engine = self.data.get("modelType", {}).get("engine")
        return engine in ("BEV", "PHEV")

    @property
    def main_battery_level(self) -> int:
        """Return the main battery level (TODO: for non-BEV it should use the 12V battery level if available?)."""
        level = 0
        if not self.has_ice_drivetrain:
            level = self.data.get("BATTERY_LEVEL", {}).get("percent", 0)
        return level

    @property
    def has_ice_drivetrain(self) -> bool:
        """Return True if vehicle has an internal combustion engine."""
        engine = self.data.get("modelType", {}).get("engine")
        return engine in ("PHEV", "COMBUSTION")

    @property
    def has_remote_climatisation(self) -> bool:
        """Return True if vehicle has remote climatisation ability."""
        return isinstance(self.data.get("HVAC_SUMMARY"), dict)

    @property
    def has_direct_charge(self) -> bool:
        """Return True if vehicle has direct charge ability."""
        engine = self.data.get("modelType", {}).get("engine")
        return engine in ("BEV", "PHEV")

    @property
    def direct_charge_on(self) -> bool:
        """Return True if direct charging is enabled."""
        return self.data.get("CHARGING_SUMMARY", {}).get("mode") == "DIRECT"

    @property
    def privacy_mode(self) -> bool | None:
        """Return True if privacy mode is on."""
        return self.data.get("GLOBAL_PRIVACY_MODE", {}).get("isEnabled")

    @property
    def remote_climatise_on(self) -> bool | None:
        """Return True if remote climatisation is on."""
        status = (self.data.get("HVAC_SUMMARY") or {}).get("status")
        _LOGGER.debug("Remote climatisation status is: %s", status)
        # ERROR (e.g. battery too low) is not running: CJNE/pyporscheconnectapi#99.
        return status in ("HEATING", "COOLING", "ON")

    @property
    def climatisation_target_temperature(self) -> float | None:
        """Return the climatisation target temperature in °C, if reported."""
        kelvin = (self.data.get("HVAC_SUMMARY") or {}).get("targetTemperature")
        return round(kelvin - 273.15, 1) if kelvin is not None else None

    @property
    def climatisation_remaining_time(self) -> int | None:
        """Return the remaining climatisation time in minutes while it runs."""
        return (self.data.get("HVAC_SUMMARY") or {}).get("remainingTime")

    @property
    def last_global_update(self) -> datetime.datetime | None:
        """Return when the car last sent its data, if reported."""
        timestamp = (self.data.get("GLOBAL_TIMESTAMP") or {}).get("timestamp")
        return _parse_porsche_datetime(timestamp) if timestamp else None

    @property
    def seat_heating(self) -> dict | None:
        """Return the climate zones the vehicle reports, with their state."""
        zones = (self.data.get("HVAC_SUMMARY") or {}).get("climateZonesEnabled")
        return zones if isinstance(zones, dict) else None

    @property
    def vehicle_locked(self) -> bool:
        """Return True if vehicle is locked."""
        return self.data.get("LOCK_STATE_VEHICLE", {}).get("isLocked") is True

    @property
    def vehicle_closed(self) -> bool:
        """Return True if all doors and lids are closed."""
        return not any(value.get("isOpen") for key, value in self.data.items() if key.startswith("OPEN_STATE_") and isinstance(value, dict))

    @property
    def doors_and_lids(self) -> dict:
        """Return list of all doors and lids and their status."""
        return {
            key: "Open" if value.get("isOpen") is True else "Closed"
            for key, value in self.data.items()
            if key.startswith("OPEN_STATE_") and isinstance(value, dict)
        }

    @property
    def tire_pressure_status(self) -> bool:
        """Return true if tire pressure is within the tolerances."""
        readings = self.tire_pressures or {}
        differences = [
            abs(value["differenceBar"])
            for key, value in readings.items()
            if key.endswith("Tire") and isinstance(value, dict) and value.get("differenceBar") is not None
        ]
        # No per-tire readings (or no TIRE_PRESSURE at all): nothing out of
        # tolerance, so report OK rather than IndexError on an empty list.
        if not differences:
            return True
        return max(differences) <= TIRE_PRESSURE_TOLERANCE

    @property
    def tire_pressures(self) -> dict | None:
        """Return the tyre readings keyed frontLeftTire, frontRightTire, ...

        Taken from the legacy TIRE_PRESSURE measurement when it carries per-tyre
        readings, else from the per-corner TIRE_PRESSURE_* measurements.
        """
        legacy = self.data.get("TIRE_PRESSURE")
        if isinstance(legacy, dict) and any(key.endswith("Tire") for key in legacy):
            return legacy
        corners = {
            name: self.data[key]
            for key, name in _TIRE_CORNERS.items()
            if isinstance(self.data.get(key), dict)
        }
        return corners or legacy

    @property
    def has_tire_pressure_monitoring(self) -> bool:
        """Return True if vehicle has tire pressure monitoring."""
        return self.tire_pressures is not None

    @property
    def charging_target(self) -> int | None:
        """Return target state of charge (SoC) for high voltage battery."""
        return self.data.get("CHARGING_SUMMARY", {}).get("minSoC")

    @property
    def location_updated_at(self) -> datetime.datetime | None:
        """Return time stamp of latest location update."""
        datetime_str = self.data.get("GPS_LOCATION", {}).get("lastModified")
        if datetime_str:
            return _parse_porsche_datetime(datetime_str)
        return None

    @property
    def location(self) -> tuple[float | None, float | None, int | None]:
        """Get the location of the vehicle."""
        loc = self.data.get("GPS_LOCATION", {}).get("location")
        heading = self.data.get("GPS_LOCATION", {}).get("direction")
        if loc and re.match(r"[\-\.0-9]+,[\-\.0-9]+", loc):
            lat, lon = map(float, loc.split(","))
        else:
            lat, lon = None, None

        return (lat, lon, heading)

    @property
    def mileage(self) -> int | None:
        """Return the odometer reading in kilometers."""
        return self.data.get("MILEAGE", {}).get("kilometers")

    @property
    def remaining_range(self) -> int | None:
        """Return the total remaining driving range in kilometers.

        For combustion vehicles this is the fuel range; for BEV/PHEV the API
        reports the combined range under the same RANGE measurement.
        """
        return self.data.get("RANGE", {}).get("kilometers")

    @property
    def electric_range(self) -> int | None:
        """Return the electric-only range in kilometers (BEV/PHEV), if reported."""
        return self.data.get("E_RANGE", {}).get("kilometers")

    @property
    def fuel_level(self) -> int | None:
        """Return the fuel tank level as a percentage (combustion/PHEV)."""
        return self.data.get("FUEL_LEVEL", {}).get("percent")

    @property
    def fuel_reserve(self) -> int | None:
        """Return the fuel reserve threshold as a percentage (combustion/PHEV)."""
        return self.data.get("FUEL_RESERVE", {}).get("percent")

    @property
    def service_intervals(self) -> dict[str, dict[str, int | None]]:
        """Return upcoming service intervals (distance in km, time in days).

        Keys mirror the API measurements: ``main``, ``oil`` and
        ``intermediate``. Each maps to ``{"kilometers": …, "days": …}`` with
        ``None`` for whichever value the vehicle does not report.
        """
        return {
            name: {
                "kilometers": self.data.get(f"{prefix}_SERVICE_RANGE", {}).get("kilometers"),
                "days": self.data.get(f"{prefix}_SERVICE_TIME", {}).get("days"),
            }
            for name, prefix in (("main", "MAIN"), ("oil", "OIL"), ("intermediate", "INTERMEDIATE"))
        }

    async def get_stored_overview(self) -> None:
        """Return stored vechicle status overview."""
        measurements = "mf=" + "&mf=".join(MEASUREMENTS)

        _LOGGER.debug("Getting stored status for vehicle %s", self.vin)
        self.status = await self.connection.get(
            f"/connect/v1/vehicles/{self.vin}?{measurements}",
        )
        self._update_vehicle_data()

    async def get_current_overview(self) -> None:
        """Return vehicle current status overview."""
        measurements = "mf=" + "&mf=".join(MEASUREMENTS)
        wakeup = "&wakeUpJob=" + str(uuid.uuid4())

        _LOGGER.debug("Getting current status for vehicle %s", self.vin)
        self.status = await self.connection.get(
            f"/connect/v1/vehicles/{self.vin}?{measurements + wakeup}",
        )
        self._update_vehicle_data()

    async def get_capabilities(self) -> None:
        """Return vehicle capabilities."""
        measurements = "mf=" + "&mf=".join(MEASUREMENTS)
        commands = "&cf=" + "&cf=".join(COMMANDS)

        _LOGGER.debug("Getting capabilities for vehicle %s", self.vin)
        self.capabilities = await self.connection.get(
            f"/connect/v1/vehicles/{self.vin}?{measurements + commands}",
        )

    async def get_trip_statistics(self) -> None:
        """Return trip statistics for vehicle as a dict."""
        measurements = "mf=" + "&mf=".join(TRIP_STATISTICS)

        _LOGGER.debug("Getting trip statistics for vehicle %s", self.vin)
        self.trip_statistics = await self.connection.get(
            f"/connect/v1/vehicles/{self.vin}?{measurements}",
        )

    async def get_picture_locations(self) -> None:
        """Return list of uri's to vechicle pictures."""
        _LOGGER.debug("Getting picture urls for vehicle %s", self.vin)
        resp = await self.connection.get(
            f"/connect/v1/vehicles/{self.vin}/pictures",
        )
        for p in resp:
            self.picture_locations[p["view"]] = p["url"]

    def __repr__(self) -> str:
        """Return a printable representation of the Porsche Connect Vehicle object."""
        return f"Vehicle({self.vin!r}, drivetrain={self.data.get('modelType', {}).get('engine')!r}, has_porsche_connect={self.has_porsche_connect!r})"

    def _update_vehicle_data(self) -> None:
        vin = self.vin
        bdata = {}
        mdata = {}
        disabled = set()
        # The payloads are dumped only when someone reads them: this runs on every poll.
        debug = _LOGGER.isEnabledFor(logging.DEBUG)

        if "vin" in self.status:
            if debug:
                _LOGGER.debug("Vehicle status dict for %s is now: %s", vin, json.dumps(self.status, indent=2))

            # Use .get via membership test: not every vehicle reports every
            # base field (e.g. systemInfo/timestamp can be absent), and a
            # missing key here used to KeyError the whole parse.
            bdata = {k: self.status[k] for k in BASE_DATA if k in self.status}

            bdata["name"] = self.status.get("customName") or bdata.get("modelName") or self.model_name

            if debug:
                _LOGGER.debug("Got base data for vehicle '%s': %s", vin, json.dumps(bdata, indent=2))

            if "measurements" in self.status:
                for m in self.status["measurements"]:
                    if "key" not in m:
                        continue
                    if not (m.get("status") or {}).get("isEnabled"):
                        # Explicitly disabled (privacy mode, expired contract):
                        # its last value must not live on. A measurement merely
                        # absent from one payload keeps its value.
                        disabled.add(m["key"])
                    # An enabled measurement can still omit "value" (e.g.
                    # transient API states); skip it rather than KeyError.
                    elif "value" in m:
                        mdata[m["key"]] = m["value"]

                if "CHARGING_RATE" in mdata and not mdata.get("CHARGING_RATE", {}).get("chargingRate"):
                    # Charging is currently not ongoing, but we should still feed some data to the sensor
                    mdata["CHARGING_RATE"]["chargingRate-kph"] = 0
                    mdata["CHARGING_RATE"]["chargingPower"] = 0

                if "CHARGING_RATE" in mdata and mdata.get("CHARGING_RATE", {}).get("chargingRate"):
                    # This attribute gives the chargingRate in the odd unit kilometers per minute. We add a km/h attribute.
                    mdata["CHARGING_RATE"]["chargingRate-kph"] = mdata["CHARGING_RATE"]["chargingRate"] * 60

                if "CHARGING_SUMMARY" in mdata and mdata.get("CHARGING_SUMMARY", {}).get("targetSoC") is not None:
                    # Newer payloads expose the active target SoC directly.
                    mdata["CHARGING_SUMMARY"]["minSoC"] = mdata["CHARGING_SUMMARY"]["targetSoC"]

                elif "CHARGING_SUMMARY" in mdata and mdata.get("CHARGING_SUMMARY", {}).get("mode") == "PROFILE":
                    # If charging profiles are enabled, get minSoC from this dict.
                    mdata["CHARGING_SUMMARY"]["minSoC"] = (mdata["CHARGING_SUMMARY"].get("chargingProfile") or {}).get("minSoC")

                if (
                    "DEPARTURES" in mdata
                    and "CHARGING_SUMMARY" in mdata
                    and mdata.get("CHARGING_SETTINGS", {}).get("targetSoc")
                    and mdata.get("CHARGING_SUMMARY", {}).get("targetSoC") is None
                ):
                    # If charging on departures are enabled, get minSoC from the CHARGING_SETTINGS dict.
                    mdata["CHARGING_SUMMARY"]["minSoC"] = mdata["CHARGING_SETTINGS"]["targetSoc"]

                if (
                    "DEPARTURES" not in mdata
                    and "CHARGING_SUMMARY" in mdata
                    and mdata.get("CHARGING_SUMMARY", {}).get("mode") == "DIRECT"
                    and mdata.get("CHARGING_SUMMARY", {}).get("minSoC") is None
                ):
                    # If direct charging is ongoing, minSoC is set to None in the API. We set it till 100 instead.
                    mdata["CHARGING_SUMMARY"]["minSoC"] = 100

                if "CHARGING_SUMMARY" in mdata and mdata.get("CHARGING_SUMMARY", {}).get("targetDateTimeWithOffset"):
                    # If charging is ongoing we convert the targetDateTime string to a datetime object. If not we set it to None.
                    mdata["CHARGING_SUMMARY"]["targetDateTimeWithOffset"] = _parse_porsche_datetime(
                        mdata["CHARGING_SUMMARY"]["targetDateTimeWithOffset"],
                    )
                elif "CHARGING_SUMMARY" in mdata:
                    mdata["CHARGING_SUMMARY"]["targetDateTimeWithOffset"] = None

                if debug:
                    _LOGGER.debug("Got measurement data for vehicle '%s': %s", vin, json.dumps(mdata, indent=2, default=str))

            else:
                _LOGGER.debug("Measurement data missing for vehicle '%s'", vin)

        else:
            _LOGGER.debug("Base data missing for vehicle '%s'", vin)

        self.data = {k: v for k, v in self.data.items() if k not in disabled} | bdata | mdata
