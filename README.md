# FlightLens

FlightLens reads a drone flight log after landing and tells you whether the aircraft is healthy, what went wrong, when it happened and what to fix first. Everything runs on your own machine; logs are never sent anywhere.

```
$ flightlens analyze 9c90f86e.ulg
FlightLens 0.1.0 · PX4 v1.14.4 · Hexarotor · 22:12
0.31 km · max alt 4 m · max speed 3.1 m/s · 24367 mAh used · Stabilized, Position
Verdict: PROBLEM   (health score 10/100)

✖ Problem    Accelerometer clipping          00:54–21:32  27,935 events > 100
✖ Problem    Vibration too high (worst IMU)  09:46–09:50  43.4 m/s² > 30
✖ Problem    Battery voltage sagged          19:44–22:11  3.20 V/cell < 3.30
✔ OK         Attitude tracking, Estimator health, Errors and failsafes, GPS quality, ...
```

**Status:** PX4 `.ulg` logs are supported end to end (CLI, web app, Docker). ArduPilot `.bin` and MAVLink `.tlog` parsers are next.

## Run it

**With Docker** (recommended):

```bash
docker compose up -d --build
```

Open http://localhost:8000 and drop `.ulg` files onto the page. Flights are stored in the `flightlens-data` Docker volume and survive restarts. The port is bound to this computer only, and the server only answers to `localhost`. To share it on your network, see `docker-compose.yml` (or run `flightlens serve --host 0.0.0.0 --allow-host <your-ip>`).

**Without Docker** (Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 20+):

```bash
uv sync
cd web && npm ci && npm run build && cd ..
uv run flightlens serve          # http://localhost:8000, data in ./data
```

## Commands

| Command | What it does |
|---|---|
| `flightlens analyze <log> [--json out.json]` | Prints the verdict and findings |
| `flightlens batch <folder> [--csv out.csv]` | Analyzes every log in a folder and summarizes the results |
| `flightlens signals <log>` | Lists every signal found, with its source topic and rate |
| `flightlens serve` | Starts the web app and API |
| `flightlens checks` | Lists the checks, their signals and default thresholds |

Exit codes for `analyze` (for CI): `0` healthy · `1` attention · `2` problem · `3` insufficient data or unreadable log · `64` bad arguments.

## What it checks

| Check | Looks at | Notes |
|---|---|---|
| Vibration level | PX4 `accel_vibration_metric` (worst IMU) | 20 / 30 m/s² held 3 s, set from real logs |
| IMU clipping | accelerometer clipping counters | any / > 100 events while armed |
| Attitude tracking | attitude vs setpoint | only in the air and when the attitude controller is active |
| GPS quality | satellites, HDOP, fix | not evaluated if GPS never had a fix (indoor, vision) |
| Battery voltage | volts per cell under load | not evaluated if cell count or calibration is implausible |
| Motor saturation | lift motors above 95% | VTOL pushers excluded |
| Motor balance | mean motor output in steady hover | needs 10 s of hover |
| Estimator health | EKF innovation test ratios | heading ignored 10 s after takeoff (in-flight yaw alignment) |
| Errors and failsafes | failsafes, failure detector, error messages | only in flight |

Thresholds are community starting values, not limits approved for your aircraft.

## How it works

```
.ulg  →  parser + versioned mapping table  →  common data model  →  checks  →  findings  →  verdict
```

- **Common data model** ([model.py](src/flightlens/model.py)): signals named in one vocabulary (`vibe.metric`, `bat.volt`, …), events, parameters, armed segments and data-quality issues. Firmware-specific topic names live only in [mappings/px4.yaml](src/flightlens/parsers/mappings/px4.yaml).
- **Honest results:** a check without data is *not evaluated*, never OK. A flight is called *healthy* only if at least 3 checks actually ran and it was armed for at least 10 s; otherwise the verdict is *insufficient data*.
- **Isolation:** the web server parses each upload in a separate process with a time limit, so a broken file cannot take it down.
- **Traceability:** reports record the FlightLens version, each check's version, the mapping version and the log's SHA-256. Original files are kept unchanged.

See [docs/real-log-results.md](docs/real-log-results.md) for how the checks behave on 28 real public PX4 logs.

## Writing a check

```python
from flightlens.checks import register
from flightlens.model import CheckConfig, Finding, Flight, Severity


@register
class MyCheck:
    id = "my.check"
    title = "My check"
    version = "1.0.0"
    required_signals = ("bat.volt",)
    defaults = {"attention": 3.5, "problem": 3.3}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]: ...
```

Drop the file in `src/flightlens/checks/`; it is discovered automatically. Add synthetic tests like [test_checks_m2.py](tests/test_checks_m2.py).

## Development

```bash
uv run pytest                                   # 90 tests
uv run ruff check . && uv run ruff format --check . && uv run mypy
cd web && npm run dev                           # UI with hot reload; run `uv run flightlens serve` alongside
uv run python scripts/fetch_px4_logs.py --count 20   # public test logs into testdata/real/
uv run flightlens batch testdata/real
```

## Licence

MIT. Sample logs in `testdata/logs/` come from [PX4/pyulog](https://github.com/PX4/pyulog) (BSD-3-Clause).
