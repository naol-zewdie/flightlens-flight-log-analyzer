# Building the labelled test log library

FlightLens is validated against logs where the expected outcome is known: healthy simulated flights, which should produce no Problem findings, and simulated flights with an injected fault, which should produce one specific finding.

## Layout

```
testdata/
  logs/        small logs committed to the repo (keep each under ~5 MB)
  expected/    one YAML file per log, same base name
```

Logs larger than a few MB should not go in git; keep them in a release asset or Git LFS later.

**Confidentiality:** only simulated logs, public logs whose terms allow reuse, and logs you own. Never commit company logs, aircraft identifiers or real locations.

## Expected findings file

`testdata/expected/<log-name>.yaml`:

```yaml
log: copter-vibration-z.bin
source: ArduPilot SITL, Copter 4.x            # how it was produced
injection: SIM_VIB_MOT_MAX=... from 120 s      # what was injected
verdict: problem                               # healthy | attention | problem
findings:
  - check: vibration.level
    severity: problem
    window: [120, 180]                         # seconds, approximate
    tolerance_s: 10
must_not_contain:                              # checks that must stay quiet
  - gps.quality
```

For a healthy baseline, use `verdict: healthy` with an empty `findings` list.

## Recording ArduPilot SITL logs

1. Install the ArduPilot SITL environment (see the ArduPilot developer docs, "Setting up SITL").
2. Start a copter: `sim_vehicle.py -v ArduCopter --console --map`
3. Optional: log while disarmed too, with `param set LOG_DISARMED 1`.
4. Fly the profile: arm, take off, hover, run a short waypoint mission, then RTL.
5. Inject the fault mid-flight with `param set <SIM_PARAM> <value>`, and note the time.
6. Copy the `.BIN` file from the `logs/` folder of the SITL working directory.

Fault parameters to look for (names change between versions, so confirm them with `param show SIM_*` in your build):

| Fault | Parameters to look for |
|---|---|
| Vibration | `SIM_VIB_MOT_MAX`, `SIM_VIB_FREQ_X/Y/Z`, accelerometer noise `SIM_ACC*_RND` |
| GPS degradation or loss | `SIM_GPS*_ENABLE` / `SIM_GPS_DISABLE`, `SIM_GPS*_NUMSATS`, `SIM_GPS*_GLITCH_*` |
| Motor failure | `SIM_ENGINE_FAIL`, `SIM_ENGINE_MUL` |
| Battery | `SIM_BATT_VOLTAGE`, `SIM_BATT_CAP_AH` |

## Recording PX4 SITL logs

1. Build and run PX4 SITL (see the PX4 docs, "Simulation"), for example `make px4_sitl gz_x500`.
2. Enable failure injection: `param set SYS_FAILURE_EN 1`.
3. During flight, inject faults from the PX4 shell, for example `failure gps off` or `failure motor off -i 1`.
4. Copy the `.ulg` file from the log directory of the build.

## Minimum library for the MVP gate

| Log | Expected |
|---|---|
| Copter hover + mission, healthy | healthy |
| Copter, high vibration | vibration.level problem |
| Copter, GPS loss | gps.quality problem |
| Copter, motor degradation | motor checks attention or problem |
| Copter, low battery | battery.sag attention or problem |
| PX4 x500, healthy | healthy |
| Truncated copy of any log | analyzes what it can, reports the truncation |
