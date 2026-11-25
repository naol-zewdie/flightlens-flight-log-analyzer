# Changelog

All notable changes to FlightLens. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). A check's own version (shown by
`flightlens checks` and stored in every report) changes whenever its logic or
thresholds do.

## [1.2.0] - unreleased

### Added
- **Video** in the Replay tab (`docs/replay.md`): attach MP4 or WebM videos
  to a flight (up to 4 GB, several per flight, stored as uploaded with their
  SHA-256), sync once (sync points with a fitted offset and camera-clock
  drift, nudges by 1 s / 0.1 s / one frame, the camera's clock as a first
  guess), then the video follows the replay: speed, pause, scrubbing, drift
  corrected over 0.1 s; the timeline shows the part of the flight with video.
- `POST/GET /api/flights/{id}/videos`, `GET /api/videos/{id}/file` (HTTP
  Range), `PATCH`, `DELETE`; database schema 6.
- Backups include videos (format 2; `--no-video` leaves them out); prune
  counts their size.
- Validated with a generated video (offset 12.3 s, camera clock 0.1% fast):
  after a two-point sync an unmarked flash shows exactly inside its 0.2 s.

### Fixed
- The replay's time readout rounded up to the next second (40.7 s showed
  as 00:41.7).
- On a busy machine the replay played slower than real time: frame gaps over
  1 s were dropped as if the tab were hidden. A hidden tab is now detected
  directly (Page Visibility API).

## [1.1.0] - 2026-11-24

The first public release. FlightLens reads PX4 `.ulg`, ArduPilot `.bin` and
MAVLink `.tlog` logs and gives a health verdict, ranked findings and the
evidence behind them, from the command line, a local web app or Docker, and
replays every flight in 3D with flight instruments and synced charts.
25 checks, validated on real public logs and a 54-log fault benchmark;
per-aircraft trends and threshold profiles; printable reports; engineer
review; backup and restore; metrics and logs for running it as a service.
(1.0.0 was prepared but never published; its changes are part of this release.)

### Added
- **Replay** (`docs/replay.md`): a Replay tab on every
  flight. A 3D aircraft flies the logged path with the logged attitude, with
  flight instruments, a map, synced charts (follow mode) and the timeline on
  one clock; chase, orbit, top and cockpit cameras; play, speed (0.25x-16x),
  loop a range (shift-drag), keyboard control; "Replay from here" on every
  finding and event; `?t=` links. Works for PX4, ArduPilot and telemetry
  logs, offline; three.js loads only when the tab is opened. A browser
  without WebGL gets an explanation in the 3D panel; the rest still works.
- The replay is built with every analysis (`replay.parquet`, `replay.json`):
  slerp-interpolated attitude, never interpolated across gaps over 1 s;
  served as `/api/flights/{id}/replay` and `/replay.bin` (about 3 MB per hour
  of flight). Flights analyzed before 1.1 show as outdated.
- PX4 logs carry the attitude quaternion (`att.q0`-`q3`; mapping
  `px4-2026.11.6`).
- `flightlens replay-info <log>`; a scripted known-manoeuvre flight
  (`flightlens.sim.manoeuvre`, demo flight `quad-12-manoeuvres.bin`) against
  which the replay is checked: exact for onboard logs, within 0.2° and 0.17 m
  for telemetry logs (see `docs/validation.md`).
- Browser tests of the Replay tab (Playwright) and web unit tests (vitest),
  both in CI.
- **ESC, barometer and wind checks** (`esc.health`, `baro.health`,
  `wind.speed`) with new signals `esc.N.rpm/temp/curr`, `baro.alt`,
  `baro.alt2`, `wind.n/e` and `airspeed` (mappings `px4-2026.11.5`,
  `ardupilot-2026.11.5`). The barometer is judged by its drift between takeoff
  and landing against GPS *at rest*, never against GPS in flight: on real
  crashes GPS altitude lagged by 24 m while the barometer was right. Wind
  leaves impacts out (the estimator read 13.5 m/s for 0.7 s at a real crash).
  Calibrated on 20 real ArduPilot and 28 public PX4 logs: no Problem on any
  of them. 8 benchmark scenarios (54 logs).
- **Operations**: `/api/health` reports the database, job workers and disk
  (503 when down); Prometheus metrics at `/metrics`; request and analysis logs,
  as text or JSON (`--log-format json`), with an `X-Request-ID` on every
  response; `--workers N` parallel analysis; uploads refused with 503 when the
  queue is full and 507 when the disk is nearly full; graceful shutdown that
  lets running analyses finish and resumes unfinished ones at the next start
  (database schema 5: job attempts). Opening a library from the CLI no longer
  fails the jobs of a server that is running on it.
- **Data safety**: the database schema is versioned (`PRAGMA user_version`)
  with ordered, transactional migrations; an upgrade keeps a copy of the old
  database and a newer library is refused. `flightlens backup` (consistent
  while the server runs) and `flightlens restore` (into a new folder only,
  SHA-256 checked, path-safe); `flightlens prune` for retention (lists first,
  keeps reviewed flights, deletes only with `--yes`).
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
- Restore refuses crafted backups: archive names with backslashes (a path
  escape on Windows), invalid flight ids in the manifest or database, database
  triggers or views, and backups larger than the free disk space.
- Every response carries `X-Content-Type-Options`, `Referrer-Policy` and
  `X-Frame-Options`; the web UI has a Content-Security-Policy (inline scripts
  allowed only by hash). `serve` warns when it listens beyond this computer.
- Uploads are refused (queue full, low disk) before their body is read.
- react-router 7.18.6 (moderate advisories in 6.x).
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
