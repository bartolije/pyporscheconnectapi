# Changelog

## Unreleased

### Changed

- The requests carry the user agent of the current app (21.26.46, build
  195601). Porsche refuses app builds older than 183720; the previous one,
  186611, was still accepted.

## 1.0.1

### Fixed

- `Connection.close()` closed an HTTP client the caller had passed in. It now
  only closes a client the connection created itself.
- An empty answer to a read (vehicle list, overview, pictures) raised
  `TypeError`. It is now a `PorscheExceptionError`, like every other failure.
- A login that Porsche refuses before the login page raised `KeyError('state')`.
  It is now a `PorscheExceptionError` with Porsche's reason.
- `unlock_vehicle` raised `AttributeError` on an empty answer to the PIN
  challenge.

### Changed

- Debug logs no longer hold the position of the car, its pairing codes or its
  full VIN in the payload dumps, and error messages name the request path
  without the list of measurements.

## 1.0.0

First stable release of this fork. One release for everything below.

### Breaking

- `rich` and `aiofiles` are no longer installed by default. Install
  `pyporscheconnectapi-bartolije[cli]` to use `porschecli`.
- A command the API answers with `ERROR` straight away raises
  `PorscheRemoteServiceError`, as an `ERROR` reached through polling already
  did. It used to return a status whose `state` was `ERROR`.
- `unlock_vehicle` raises `PorscheRemoteServiceError` when the car returns no
  PIN challenge or the PIN is malformed. It used to return `None` or raise
  `ValueError`.
- `PorscheVehicle.connected` returns `None`, not the string `"not available"`,
  when the API does not report it.
- `porschecli list` prints a list of vehicles instead of one merged dictionary;
  the unused `--profileactive` option is gone.
- `PorscheVehicle.vehicle_locked` returns `None`, not `False`, when the car
  does not report its lock state.
- `connection.log_request` (unused) is removed. `PorscheExceptionError` no
  longer accepts arbitrary keyword arguments.

### Fixed

- A command (lock, unlock, climatisation, charging, honk and flash) was sent
  again, up to three more times, after a 429, 502, 503, 504 or a network error,
  although the first one may have reached the car. Only reads are retried now.
- The debug log of an unlock held a hash a 4-digit PIN can be recovered from.
  It now only names the command.
- `main_battery_level` was always 0 for a plug-in hybrid.
- An enabled measurement with a `null` value raised `AttributeError` and failed
  the whole update.
- `porschecli token` printed `None`.
- `str()` of a `PorscheExceptionError` was empty; it is now the message.
- With an `httpx2` client, which Home Assistant 2026.11 and later pass to the
  library, HTTP errors, network failures and a rejected refresh token escaped as
  raw `httpx2` exceptions: no retry, no fresh login, no `PorscheExceptionError`.
  The library no longer depends on the exception classes of the client it is
  given, and accepts both `httpx.AsyncClient` and `httpx2.AsyncClient` in its
  annotations.
- A token endpoint answer that is not JSON, or has no `expires_in`, raised
  `JSONDecodeError` or `KeyError`.
- An empty or non-JSON API response raised a raw `JSONDecodeError`.
- A network failure during login raised a raw `httpx` exception.
- A used captcha code stayed on the client and broke the next full login.
- A measurement the API reports as disabled kept its last value forever.
- Incomplete payloads raised `KeyError` (`modelName`, `status`,
  `chargingProfile`, `isOpen`).
- `update_charging_profile` raised `KeyError` or `RuntimeError` without a
  profile list or an active profile.
- A token without a refresh token POSTed `None` to the token endpoint instead
  of logging in again.
- The `vin` argument of `PorscheVehicle` was ignored.
- CLI: the session file is written owner-only, and the token is saved when a
  command fails.

### Changed

- `has_porsche_connect` reads the `CONNECT_CONTRACT` measurement instead of
  always returning `True`. It is `False` only when the contract is reported
  disabled.
- Concurrent requests that hit a 401 refresh the token once.
- Command polling waits 1, 2, 3, 4 then 5 seconds between checks instead of
  1 second every time.
- Debug payloads are only serialised when the debug log is enabled.

### Added

- Type annotations throughout and a `py.typed` marker.
- `pyporscheconnectapi.__version__`.
- Packaging moved to `pyproject.toml`; the license metadata is
  `MIT AND Apache-2.0` and the Apache text ships as `LICENSE-APACHE`.
