# Changelog

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
- `connection.log_request` (unused) is removed. `PorscheExceptionError` no
  longer accepts arbitrary keyword arguments.

### Fixed

- `str()` of a `PorscheExceptionError` was empty; it is now the message.
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
