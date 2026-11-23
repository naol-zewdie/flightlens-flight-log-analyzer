# Changelog

All notable changes to FlightLens. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). A check's own version (shown by
`flightlens checks` and stored in every report) changes whenever its logic or
thresholds do.

## [0.2.0] - unreleased

### Added
- **MAVLink telemetry logs (`.tlog`)** from ArduPilot and PX4, as Mission
  Planner and QGroundControl record them: an own MAVLink v1/v2 decoder
  (signed frames, CRC-checked, damaged bytes skipped; definitions tested
  against pymavlink), vehicle selection among the senders, a versioned
  signal mapping (`mavlink.yaml`), arming, modes, landed state, motors,
  parameters and status texts; radio packet loss is reported. A telemetry
  generator in the simulator puts 5 tlog scenarios in the benchmark (46 logs)
  and one in the demo.
- **Rate-loop tuning** (`control.tuning`): the roll and pitch rate controllers'
  step response, estimated from normal flying by regularised deconvolution
  (as PID-Analyzer and PX4 Flight Review do), with overshoot, falls-short,
  does-not-follow and sluggish findings. Judged only when at least 20 windows
  with a coherence of 0.6 support it; checked against simulated loops of known
  damping (peaks within 0.15). A "Tuning" tab plots the response per axis.
  ArduPlane rate data now comes from `PIDR/PIDP/PIDY`.
- **Autopilot power** (`power.supply`), **battery reserve at landing**
  (`battery.reserve`, ignoring estimates that follow voltage sag), **compass
  interference** (`compass.interference`), **estimator lane switches**
  (`estimator.lanes`) and **RC link** (`rc.link`, PX4).
- The fault benchmark grows to 41 logs (tuning, brown-out, battery reserve,
  compass and lane-switch faults, each with a healthy case near the limit).
- **Printable reports**: a self-contained HTML report per flight (evidence
  charts, provenance, reviews; print to PDF), from the "Report" link or
  `flightlens analyze --html`.
- **Outdated-analysis detection**: flights analyzed by an older check or signal
  mapping are marked with the reason, and can be re-analyzed in one click
  (library) or one by one (flight page).
- **Engineer review**: mark findings confirmed, false alarm or accepted with a
  note; reviews never change the verdict, appear in reports, and export as a
  `flightlens validate` manifest (`flightlens reviews export`).
- **Threshold profiles**: per-aircraft threshold overrides, hand-written (YAML,
  API) or learned from the aircraft's own flights without a Problem
  (vibration, motor balance and battery attention levels; clamped, problem
  levels never learned). Assigned on the Aircraft page, which re-analyzes the
  aircraft's flights; `--profile` on `analyze`, `batch` and `validate`;
  `flightlens profile learn` and `profile show`. Reports record the profile's
  name, source and digest.
- **Flight comparison**: two flights overlaid on their common signals, aligned
  at takeoff, with health-metric changes and a findings diff (new, gone, both).
  Opened from the library (select two), an aircraft's flight list, or a flight.
- **ArduPilot `.bin` logs**: an independent DataFlash reader (no pymavlink) and a
  versioned signal mapping, with the same checks and report as PX4. A 100 MB log
  loads in about 3 s.
- **Fixed-wing support and crash diagnosis**: airborne time from `STAT.isFlying`;
  new checks for crash or hard landing (with earlier findings of the same flight
  linked as likely causes), takeoff that never left the ground, refused arming
  (with the reasons), attitude beyond the configured limit, control surface at
  full travel, and out-of-trim.
- **Gyro noise spectrum and notch-filter advice** (`vibration.spectrum`): Welch
  spectrum from raw gyro (PX4 `sensor_gyro_fifo` / `sensor_combined`, ArduPilot
  batch sampler or `GYR`), motor noise told apart from frame resonance by its
  response to throttle, and concrete `INS_HNTCH_*` / `IMU_GYRO_*` values. A
  "Noise spectrum" tab with the spectrum and a spectrogram.
- **Validation**: `flightlens validate` scores FlightLens against labelled logs
  (per-check recall, precision, false alarms, timing); a 27-log generated fault
  benchmark runs in CI (`docs/validation.md`);
  `scripts/sitl_campaign.py` records ArduPilot SITL fault flights.
- **`flightlens demo`**: generated example flights, one known fault each, opened
  in the web app; `--read-only` (also on `serve`) refuses uploads, edits and
  deletes for a public demo.
- **Aircraft and health over time**: flights are grouped into aircraft by the
  autopilot board serial (PX4 `sys_uuid`, ArduPilot board UID) or a user-given
  name, which new flights from the same board inherit. Each flight stores trend
  metrics (vibration, battery internal resistance, lowest cell voltage, motor
  output and imbalance, estimator innovations, gyro noise, trim, score), and the
  Aircraft page flags drift against the aircraft's own history ("maintenance
  due") with small-multiple trend charts.
- Time in the air and sorties in the summary; "never armed" instead of a
  misleading duration.
- The wheel bundles the web UI, so `pip install flightlens` then
  `flightlens serve` needs no Node; release workflow for PyPI, a multi-arch GHCR
  image and GitHub releases; documentation site.

### Changed
- Checks judge only time in the air (armed, not landed, and the altitude really
  changed); short impacts are reported by the crash check, not as vibration,
  clipping or battery sag.
- Yaw is never tracked on fixed wing, and an axis whose setpoint never changes is
  skipped.
- The evidence chart opens zoomed on the finding, with one chart per unit.
- Flight modes in the summary include a mode selected before arming.
- The board name is read from ArduPilot banners that write the UID in groups
  (`CUAVv5-bdshot 00270035 3156500F 20333543`); it was missing before.
- `flightlens serve` in a source checkout prefers `web/dist` over a bundled copy.

### Fixed
- From 71 real Mission Planner telemetry logs: a ground run-up whose noisy
  barometric altitude crossed 1 m counted as a flight (the climb is now
  measured after a running median); a flight that never left the ground could
  be called healthy (*healthy* now needs 10 s in the air); GPS findings came
  from time on the ground (GPS is now judged in the air); Betaflight's
  `Arming disabled:` reasons are recognised.
- Damaged or hostile logs can no longer hang or crash the parsers (found by
  fuzzing): a PX4 log that sent the ULog reader (pyulog 1.3.0) into an endless
  loop is now refused with a clear error; ULog topics without a timestamp,
  non-numeric parameters and damaged version/time fields are reported as data
  quality issues instead of crashing; DataFlash message types whose FMT
  definition is damaged (wrong column types, no timestamp) are skipped.

### Security
- Parsers are fuzzed in CI (Hypothesis, `tests/test_fuzz.py`) and weekly with a
  long mutation campaign (`scripts/fuzz.py`).
- CI audits the Python runtime dependencies (pip-audit) and the web UI's bundled
  packages (npm audit); releases attach CycloneDX SBOMs for both.
- `react-router-dom` 6.28.0 → 6.30.6 (high-severity open-redirect advisories);
  `vite` 5.4.11 → 5.4.21 (development server only).
- False "poor yaw tracking" and single-spike "vibration" Problems on ArduPlane
  logs (13 of 15 real logs were affected).

## [0.1.0] - 2026-11-10

### Added
- PX4 `.ulg` parser with a versioned signal mapping, 10 checks with a verdict and
  a 0–100 health score, calibrated on 28 public PX4 logs.
- CLI (`analyze`, `batch`, `signals`, `checks`, `serve`) with exit codes for CI.
- FastAPI backend with an isolated analysis worker, SQLite and Parquet storage;
  React web app; Docker image.
