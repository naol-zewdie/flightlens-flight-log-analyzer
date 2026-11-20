# Changelog

All notable changes to FlightLens. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). A check's own version (shown by
`flightlens checks` and stored in every report) changes whenever its logic or
thresholds do.

## [0.2.0] - unreleased

### Added
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
- False "poor yaw tracking" and single-spike "vibration" Problems on ArduPlane
  logs (13 of 15 real logs were affected).

## [0.1.0] - 2026-11-10

### Added
- PX4 `.ulg` parser with a versioned signal mapping, 10 checks with a verdict and
  a 0–100 health score, calibrated on 28 public PX4 logs.
- CLI (`analyze`, `batch`, `signals`, `checks`, `serve`) with exit codes for CI.
- FastAPI backend with an isolated analysis worker, SQLite and Parquet storage;
  React web app; Docker image.
