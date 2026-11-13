# pyporscheconnectapi (bartolije fork)
A python library for Porsche Connect API

> Fork of [CJNE/pyporscheconnectapi](https://github.com/CJNE/pyporscheconnectapi)
> by [Johan Isacsson](https://github.com/CJNE) and its contributors, maintained by
> [bartolije](https://github.com/bartolije). It is published on PyPI as
> **`pyporscheconnectapi-bartolije`** (the module is still imported as
> `pyporscheconnectapi`) and is used by the
> [bartolije/ha-porscheconnect](https://github.com/bartolije/ha-porscheconnect)
> Home Assistant integration. Fixes that also apply upstream are sent back as
> pull requests.

This library will let you access your car equipped with Porsche Connect. It does not work with the predecessor Porsche Car Connect.
Porsche Connect is available for the following Porsche models:

* Boxster & Cayman (718)
* 911 (from 992)
* Taycan
* Panamera (from 2021, G2 PA)
* Macan (EV, from 2024)
* Cayenne (from 2017, E3)

You can also take a look here, select your model and see if your model has support for Porsche Connect:
https://connect-store.porsche.com/

A Porsche Connect subscription also needs to be active for it to work.

*NOTE:* This work is not officially supported by Porsche and functionality can stop working at any time without warning.

## Installation

The easiest method is to install using pip3/pip (venv is also a good idea).
```
pip install pyporscheconnectapi-bartolije
```

The command line tool needs two more packages, installed with the `cli` extra:

```
pip install "pyporscheconnectapi-bartolije[cli]"
```

to update to the latest version

```
pip install pyporscheconnectapi-bartolije -U
```

Setup will add a cli under the name porschecli, see below for usage.


## CLI usage

A simple cli is provided with this library, it will cache tokens to a file to speed up invocations.

If no email or password is supplied as input arguments and no config file with those details is found you will be prompted.
```
usage: porschecli [-h] [-d] [-j] [-e EMAIL] [-p PASSWORD] [-s SESSION_FILE]
                  {list,token,battery,capabilities,chargingprofile,chargingsettings,climatise_off,climatise_on,connected,currentoverview,direct_charge_off,direct_charge_on,doors_and_lids,flash_indicators,honk_and_flash,location,lock_vehicle,pictures,storedoverview,tire_status,tire_pressures,trip_statistics,unlock_vehicle,vehicle_closed} ...

Porsche Connect CLI

positional arguments:
  {list,token,battery,capabilities,chargingprofile,chargingsettings,climatise_off,climatise_on,connected,currentoverview,direct_charge_off,direct_charge_on,doors_and_lids,flash_indicators,honk_and_flash,location,lock_vehicle,pictures,storedoverview,tire_status,tire_pressures,trip_statistics,unlock_vehicle,vehicle_closed}
                        command help
    battery             Prints the main battery level (BEV)
    capabilities        Get vehicle capabilities
    chargingprofile     Update parameters in configured charging profile
    chargingsettings    Update charging settings
    climatise_off       Stop remote climatisation
    climatise_on        Start remote climatisation
    connected           Check if vehicle is on-line
    currentoverview     Poll vehicle for current overview
    direct_charge_off   Disable direct charging
    direct_charge_on    Enable direct charging
    doors_and_lids      List status of all doors and lids
    flash_indicators    Flash indicators
    honk_and_flash      Flash indicators and sound the horn
    location            Show location of vehicle
    lock_vehicle        Lock vehicle
    pictures            Get vehicle pictures url
    storedoverview      Get stored overview for vehicle
    tire_status         Check if tire pressure are ok
    tire_pressures      Get tire pressure readings
    trip_statistics     Get trip statistics from backend
    unlock_vehicle      Unlock vehicle
    vehicle_closed      Check if all doors and lids are closed

options:
  -h, --help            show this help message and exit
  -d, --debug
  -j, --json
  -e, --email EMAIL
  -p, --password PASSWORD
  -s, --sessionfile SESSION_FILE
```

`list` prints the vehicles of the account. `token` checks the session and prints
it with the tokens masked: the tokens themselves stay in the session file.

## Config file (for CLI)

A config file is searched for in ~/.porscheconnect.cfg and ./.porscheconnect.cfg
The format is:

```
[porsche]
email=<your email>
password=<your password>
session_file=<file to store session information>
```

## Library usage

Install pyporscheconnectapi-bartolije using pip (requires python >= 3.12)


### Example client usage

Please refer to the examples provided in the repository.

### Resuming a captcha challenge from another process

Auth0 sometimes requires a captcha during login. The challenge is bound to
the session cookies of the HTTP client that started it — since 0.4.0 the
`PorscheCaptchaRequiredError` carries everything needed to resume the login
elsewhere (another process, after a restart, ...):

```python
from pyporscheconnectapi.connection import Connection
from pyporscheconnectapi.exceptions import PorscheCaptchaRequiredError

try:
    connection = Connection(email, password)
    await connection.get_token()
except PorscheCaptchaRequiredError as err:
    # cookies is JSON-compatible
    persist(err.captcha, err.state, err.cookies, err.code_verifier)

# ... later, in a fresh process, once the captcha is solved:
connection = Connection(
    email,
    password,
    captcha_code=solved_code,
    state=state,
    cookies=cookies,
    code_verifier=code_verifier,
)
await connection.get_token()
```

Treat `err.cookies` and `err.code_verifier` like a password: together they are the
Auth0 session secret.

### Transient error handling

Reads from the Porsche API, and the OAuth2 token endpoints, are retried on
transient failures (HTTP 429/502/503/504 and network errors) with capped
exponential backoff, honouring `Retry-After`.

A command (lock, unlock, climatisation, charging, honk and flash) is sent
**once**: a timeout does not say whether it reached the car, so it is never
replayed. It fails with a `PorscheExceptionError` and the caller decides.

### HTTP client

Pass your own `httpx.AsyncClient` (or the `httpx2` client Home Assistant
2026.11 gives integrations) as `async_client=`. `Connection.close()` leaves a
client you passed open; it only closes one the connection created itself.

### Errors

Every failure the library reports is a `PorscheExceptionError` (or a subclass:
`PorscheWrongCredentialsError`, `PorscheLoginThrottledError`,
`PorscheCaptchaRequiredError`, `PorscheRemoteServiceError`). `str(exc)` is a
short readable message, safe to show to a user: it never contains a token, a
password or a PIN.

### Debug logs

With the `pyporscheconnectapi` logger at `DEBUG`, the payload dumps leave out
the position of the car, its pairing codes and its full VIN, and no log line
holds a token, a password, a PIN or the unlock hash. Other debug lines still
name the VIN: replace it before posting a log.

### Typing

The package is fully annotated and ships a `py.typed` marker, so mypy and
pyright check your calls into it. API payloads stay plain dictionaries
(`vehicle.data`): Porsche changes them without notice.

## Development

```
pip install -r requirements_dev.txt -e ".[cli]"
ruff check && ruff format --check && mypy && pytest
```

CI runs the same four checks on every pull request, on Python 3.12 to 3.15,
with a 95 % coverage floor (97 % today), then builds the wheel and installs it
without the CLI extra. A release is published to PyPI by trusted publishing
after the same checks, only if the tag matches the package version.

### Live smoke test (local only)

`scripts/smoke_auth.py` logs in to the real Porsche API with your own account
and reads your vehicles. It only reads; it sends no command to the car.

```
cp .porscheconnect.cfg.example .porscheconnect.cfg   # then fill it in
python scripts/smoke_auth.py
```

It is never run in CI, on purpose: that would mean storing Porsche credentials
in GitHub. `.porscheconnect.cfg`, `.session` and the captcha files are
gitignored; the script prints no token, password or cookie and masks the VINs.

## License

MIT, see [LICENSE](LICENSE). `connection.py`, `exceptions.py`, `oauth2.py`,
`retry.py` and `cookies.py` carry an `Apache-2.0` header inherited from the code
they derive from; that license is in [LICENSE-APACHE](LICENSE-APACHE). The
package metadata therefore declares `MIT AND Apache-2.0`.
