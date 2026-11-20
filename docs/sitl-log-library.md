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

## Labels: the validation manifest

One manifest per set of logs, for example `testdata/expected/sitl-copter.yaml`:

```yaml
source: ArduPilot SITL, Copter 4.6
logs_dir: ../sitl                              # relative to this file
defaults:
  exhaustive: true                             # any other Attention/Problem is a false alarm
logs:
  copter-vibration-z.bin:
    fault: vibration                           # groups the results table
    verdict: problem                           # healthy | attention | problem | insufficient data
    expect:
      - {check: vibration.level, severity: problem, window: [120, 180], tolerance_s: 10}
    allow: [imu.clipping]                      # may fire, never counted
    must_not: [gps.quality]                    # must stay quiet
  copter-healthy.bin:
    fault: healthy
    verdict: healthy
```

`expect` entries can be short (`vibration.level:problem`, or `vibration.level`
for any Attention or Problem). Score a manifest or a folder of them with
`flightlens validate <path> [--markdown out.md] [--json out.json]`; it exits 1
if any log is not fully correct.

**Automated:** `scripts/sitl_campaign.py --ardupilot ~/ardupilot --out sitl-logs`
flies ArduCopter SITL once per fault (healthy, vibration, accelerometer noise,
GPS loss, few satellites, weak motor, low battery, radio failsafe) and writes
the logs plus `campaign.yaml`. It needs Linux or WSL, a built SITL
(`./waf configure --board sitl && ./waf copter`) and `pip install pymavlink`.

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
