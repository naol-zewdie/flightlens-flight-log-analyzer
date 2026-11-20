# Check reference

Generated from FlightLens 0.2.0 by `scripts/gen_check_docs.py`; `flightlens checks` prints the same from the installed version. Thresholds are starting values, not limits approved for your aircraft; override them with `analyze(flight, config={"vibration.level": {"attention": 25}})`.

## Attitude tracking check

Signals: ``att.roll/pitch/yaw`` (measured) and ``att.des_roll/pitch/yaw``
(setpoint), in degrees. The setpoint is resampled onto the measurement times;
gaps over 0.5 s are never bridged.

Method: absolute tracking error while in the air (armed and not landed) and
while the attitude controller is active (not Acro or Manual), held for at
least 2 s.
Roll and pitch: Attention > 5°, Problem > 10° on multirotors; 10° / 20° on
fixed wing, whose attitude loops are slower. Yaw tracks more loosely on most
multirotors (yaw authority is weaker), so its default limits are 15° / 30°.
Fixed wing has no yaw-angle setpoint, so yaw is never tracked there.

An axis whose setpoint never changes while in use is skipped: a constant value
(often 0) means the firmware is not commanding that axis, not that it holds it.

### `attitude.tracking` · Attitude tracking

Version 1.1.0 · signals: `att.roll`, `att.pitch`, `att.des_roll`, `att.des_pitch`

| Setting | Default |
|---|---|
| `attention` | 5 |
| `problem` | 10 |
| `fw_attention` | 10 |
| `fw_problem` | 20 |
| `yaw_attention` | 15 |
| `yaw_problem` | 30 |
| `min_duration_s` | 2 |

## Power and link checks: the autopilot's 5 V supply, battery reserve, RC link

**power.supply** — signal ``power.5v`` (V; PX4 ``system_power.voltage5v_v``,
ArduPilot ``POWR.Vcc``), in the air. The flight controller and its sensors run
from this rail; below about 4.5 V boards start to brown out and reset. Healthy
boards in the calibration logs sit at 4.6-5.25 V (servos on the rail make it
swing by up to 0.35 V, which is not judged). Attention below ``attention``
(4.5 V), Problem below ``problem`` (4.3 V).

**battery.reserve** — signal ``bat.remaining`` (0-1), the flight controller's
own estimate. Judged at landing (the median of the last few seconds in the
air), and only when the estimate tracks the charge used: it must fall during
the flight (many logs carry a constant 0 or 1 because the capacity is not
configured), not faster than ``max_drop_per_min`` (half a pack per minute),
and never rise by ``max_rise`` (10 points) in the air. An estimate that breaks
the last two follows the voltage sag under load, as seen on real PX4 logs
(50% to 0% in 30 s of flight, back to 23% after landing).
Attention below ``attention`` (20% left), Problem below ``problem`` (10%).

**rc.link** — signal ``rc.lost`` (PX4 ``input_rc.rc_lost``), in the air.
Attention for any loss of the RC signal; Problem when it adds up to
``problem_s`` (1 s) or more. ArduPilot reports RC loss as a failsafe event,
which ``events.failsafes`` already covers.

### `battery.reserve` · Battery reserve at landing

Version 1.0.0 · signals: `bat.remaining`

| Setting | Default |
|---|---|
| `attention` | 0.2 |
| `problem` | 0.1 |
| `min_drop` | 0.05 |
| `max_drop_per_min` | 0.5 |
| `max_rise` | 0.1 |
| `landing_window_s` | 5 |

### `power.supply` · Autopilot power supply

Version 1.0.0 · signals: `power.5v`

| Setting | Default |
|---|---|
| `attention` | 4.5 |
| `problem` | 4.3 |
| `min_duration_s` | 0 |

### `rc.link` · RC link

Version 1.0.0 · signals: `rc.lost`

| Setting | Default |
|---|---|
| `problem_s` | 1 |

## Battery sag check

Signals: ``bat.volt`` and the cell count (``bat.cells``, or the BAT1_N_CELLS /
BAT_N_CELLS parameter on older PX4).

Method: per-cell voltage while armed, held for at least 1 s so single-sample
dips are ignored. Attention below 3.5 V/cell, Problem below 3.3 V/cell
(LiPo/Li-ion starting values; Li-ion packs often run lower by design, so tune
per aircraft profile).

### `battery.sag` · Battery voltage

Version 1.2.0 · signals: `bat.volt`

| Setting | Default |
|---|---|
| `attention` | 3.5 |
| `problem` | 3.3 |
| `min_duration_s` | 1 |

## Compass interference: the magnetic field changes with motor current

Signals: ``mag.x/y/z`` (gauss) and ``bat.curr`` (A), in the air.

The Earth's field is constant over a flight, so its measured strength should
be too. Current in nearby power wires and motors adds a field of its own, which
grows with the current: the estimator then sees a heading error that changes
with throttle (toilet-bowling, drifting heading, EKF compass failures).

The field strength is fitted against current. As in ArduPilot's compassmot,
the interference is the field the current adds at the highest current of the
flight, relative to the Earth's field (the fitted strength at zero current).
It only sees the part of the interference that changes the field's strength
(not its direction), so it is a lower bound, judged against compassmot's
limits: Attention above ``attention`` (30%), Problem above ``problem`` (60%).
It needs at least ``min_current_span`` (3 A) between low and high current in
the air.

### `compass.interference` · Compass interference

Version 1.0.0 · signals: `mag.x`, `mag.y`, `mag.z`, `bat.curr`

| Setting | Default |
|---|---|
| `attention` | 30 |
| `problem` | 60 |
| `min_current_span` | 3 |

## Control checks: attitude beyond the configured limits, control surfaces at full

travel, and an aircraft out of trim.

Attitude limits (``control.attitude_limits``). Roll or pitch beyond the limit
the aircraft is configured for, held 1 s in the air. Limits come from the
log's parameters: ArduPlane ROLL_LIMIT_DEG / LIM_ROLL_CD and PTCH_LIM_* /
LIM_PITCH_*; PX4 fixed wing FW_R_LIM, FW_P_LIM_*; ArduCopter ANGLE_MAX; PX4
multicopter MPC_TILTMAX_AIR. Attention beyond the limit; Problem beyond the
limit + 30° or past 150° (inverted).

Saturation (``control.saturation``). Aileron or elevator at 95% or more of its
travel in the air. Attention above 1 s in total, Problem above 5 s: the pilot
or controller had no authority left on that axis.

Trim (``control.trim``). In steady wings-level flight (|roll| < 10° for at
least 10 s), the median aileron deflection. A straight aircraft needs almost
none; Attention above 10% of travel, Problem above 25%.

### `control.attitude_limits` · Attitude limits

Version 1.0.0 · signals: `att.roll`

| Setting | Default |
|---|---|
| `min_duration_s` | 1 |
| `problem_margin_deg` | 30 |
| `inverted_deg` | 150 |

### `control.saturation` · Control surface travel

Version 1.0.0 · signals: `ctrl.ail`

| Setting | Default |
|---|---|
| `limit` | 0.95 |
| `attention_s` | 1 |
| `problem_s` | 5 |

### `control.trim` · Trim

Version 1.0.0 · signals: `ctrl.ail`, `att.roll`

| Setting | Default |
|---|---|
| `attention` | 0.1 |
| `problem` | 0.25 |
| `level_deg` | 10 |
| `min_level_s` | 10 |

## Rate-loop tuning: the step response of the roll and pitch rate controllers

Signals: ``rate.des_<axis>`` and ``rate.<axis>`` (deg/s), in the air and, when
the log says so, only while the flight controller stabilises (not Manual).

The step response is estimated from normal flying (``flightlens.tuning``). It
is judged only when the estimate is trustworthy: at least ``min_windows``
two-second windows with stick or correction activity, and a setpoint that
explains the measured rate (coherence ≥ ``min_coherence``). Yaw is shown in the
web app but not judged: its loop is normally much slower.

Calibration: on simulated loops of known damping the estimated peak is within
0.1 of the true one; on 20 public PX4 flights the well-flown ones settle at
0.9-1.1 with at most +0.2 of overshoot. Estimates from fewer than 20 windows
were too noisy on real 50 Hz logs (spurious peaks of 1.3-1.6) to be judged.

- **Overshoot**: the peak above where the response settles (or above 1.0, if it
  settles lower) exceeds ``overshoot_attention`` (+0.3) — Attention; above
  ``overshoot_problem`` (+0.6) — Problem. A loop that settles above 1 without
  ringing is not overshoot.
- **Does not reach the setpoint**: settles below ``final_low`` (0.7) — Attention.
- **Does not follow the setpoint**: settles outside ``final_min``..``final_max``
  (0.5-1.6 times) — Attention: the controller is not in control of this axis
  (actuators saturated, out of trim, feed-forward wrong), or the logged
  setpoint is not the one flown.
- **Sluggish**: 90% of the final value takes longer than ``rise_attention_s``
  (0.3 s multicopter, 0.8 s fixed wing) — Attention. A sluggish loop has not
  settled within the window, so it is not also reported as falling short.

### `control.tuning` · Rate-loop tuning

Version 1.0.0 · signals: `rate.roll`, `rate.des_roll`

| Setting | Default |
|---|---|
| `min_windows` | 20 |
| `min_coherence` | 0.6 |
| `min_input_dps` | 5 |
| `overshoot_attention` | 0.3 |
| `overshoot_problem` | 0.6 |
| `final_low` | 0.7 |
| `final_min` | 0.5 |
| `final_max` | 1.6 |
| `rise_attention_s` | 0.3 |
| `fw_rise_attention_s` | 0.8 |

## Estimator (EKF) health check

Signals: innovation test ratios ``ekf.vel``, ``ekf.pos``, ``ekf.hgt``,
``ekf.mag``. A ratio of 1.0 means the estimator rejected that sensor's
measurement as inconsistent with its prediction.

Method: while in the air, each ratio held for at least 1 s. Attention > 0.5,
Problem > 1.0. The finding names the sensor with the highest ratio, because
that is where to look. The heading ratio is ignored for 10 s after each
takeoff, when PX4 re-aligns yaw in flight by design (observed on real logs).

### `estimator.health` · Estimator health

Version 1.2.0 · signals: `ekf.hgt`

| Setting | Default |
|---|---|
| `attention` | 0.5 |
| `problem` | 1 |
| `min_duration_s` | 1 |
| `heading_settle_s` | 10 |

## Estimator lane switches: the flight controller changed which estimator it trusts

Signal: ``ekf.primary`` (PX4 ``estimator_selector_status.primary_instance``,
ArduPilot ``XKF4.PI``), in the air.

Flight controllers with several IMUs run one estimator (lane, core) per IMU and
fly on the one whose sensors agree best. A switch in flight means the active
one became worse than another: an IMU or magnetometer disagreed, or vibration
or a sensor fault upset it. One switch is Attention; ``problem_switches`` (3) or
more is Problem, a sign of a persistent sensor problem.

### `estimator.lanes` · Estimator lane switches

Version 1.0.0 · signals: `ekf.primary`

| Setting | Default |
|---|---|
| `problem_switches` | 3 |

## Events, errors and failsafes check

Signals: none; works on the flight's event list (failsafes, failure detector,
logged errors). Only events while armed (or within 2 s of it) count, because
pre-flight check messages on the ground are expected.

Problem: any failsafe or failure-detector trigger, or a critical message.
Attention: any error message, or a log that ends while still armed.

### `events.failsafes` · Errors and failsafes

Version 1.0.0 · signals: none (events)

| Setting | Default |
|---|---|
| `margin_s` | 2 |

## Flight outcome checks: crash or hard landing, and takeoffs that never happened

Crash (``flight.crash``). An impact is a short burst of extreme vibration
(ArduPilot VIBE above 60 m/s² for at most 1.5 s). An impact during or right
after a flight is a crash when any of these is true:

- the attitude was upset (|roll| > 90° or |pitch| > 60°) in the 10 s before it,
- the aircraft came to rest on its side or inverted (|roll| > 60° after it),
- the log ends, still armed, within 10 s of it (power cut by the impact),
- the firmware's own crash detector fired around it.

Otherwise it is a hard landing (Attention). An attitude upset that is followed
by the end of the log while armed is a crash even without vibration data.
The engine lists the other findings in the 20 s before a crash as likely
contributing factors.

Takeoff (``flight.takeoff``). An armed period in which a takeoff was commanded
(a Takeoff/Auto mode, or throttle or motor output above half for 3 s) but the
altitude never changed by 1 m: the aircraft did not take off.

### `flight.crash` · Crash or hard landing

Version 1.0.0 · signals: `att.roll`, `att.pitch`

| Setting | Default |
|---|---|
| `upset_roll_deg` | 90 |
| `upset_pitch_deg` | 60 |
| `rest_roll_deg` | 60 |
| `lookback_s` | 10 |
| `end_after_impact_s` | 10 |

### `flight.takeoff` · Takeoff

Version 1.0.0 · signals: `pos.rel_alt`

| Setting | Default |
|---|---|
| `command_level` | 0.5 |
| `command_s` | 3 |

## GPS quality check

Signals: ``gps.nsats``, ``gps.hdop``, ``gps.fix`` (fix type; 3 = 3D fix).

Method: while armed, the worst condition held for at least 5 s.
Attention: fewer than 8 satellites or HDOP above 1.4.
Problem: fewer than 6 satellites, HDOP above 2.5, or loss of 3D fix for 2 s.

If the receiver never had a 3D fix anywhere in the log, the flight most likely
did not use GPS (indoor, vision or simulation), so the check is not evaluated
instead of raising a false alarm.

### `gps.quality` · GPS quality

Version 1.0.0 · signals: `gps.nsats`, `gps.hdop`, `gps.fix`

| Setting | Default |
|---|---|
| `sats_attention` | 8 |
| `sats_problem` | 6 |
| `hdop_attention` | 1.4 |
| `hdop_problem` | 2.5 |
| `min_duration_s` | 5 |
| `fix_loss_s` | 2 |

## IMU clipping check

Signal: ``vibe.clip``, a cumulative count of accelerometer clipping events
(ArduPilot VIBE.Clip0-2 summed; PX4 vehicle_imu_status.accel_clipping summed,
worst IMU). Clipping means the accelerometer saturated, so its readings,
and everything the estimator derives from them, were wrong at that moment.

Method: count the new clipping events in the air, outside impacts (a touchdown
or crash saturates the accelerometer by design; the crash check reports those).
Any increase is Attention, more than 100 is a Problem.

### `imu.clipping` · IMU clipping

Version 1.2.0 · signals: `vibe.clip`

| Setting | Default |
|---|---|
| `attention` | 0 |
| `problem` | 100 |

## Motor checks: saturation and imbalance

Signals: lift motors ``mot.1..N`` as fractions of full output (0-1). VTOL
pushers are excluded by the parser.

Saturation: total time any lift motor spends above 95% in the air.
Attention > 1 s, Problem > 5 s. A saturated motor leaves no headroom for
control, so attitude can no longer be held.

Imbalance: in steady hover (armed, motors spinning, angular rates under
10 deg/s, climb under 0.5 m/s), the spread of each motor's mean output around
the average. Attention > 10%, Problem > 20%. Needs at least 10 s of hover.

### `motor.imbalance` · Motor balance

Version 1.1.0 · signals: `mot.1`, `mot.2`, `mot.3`, `rate.roll`, `rate.pitch`, `rate.yaw`

| Setting | Default |
|---|---|
| `attention` | 0.1 |
| `problem` | 0.2 |
| `max_rate_dps` | 10 |
| `max_climb_m_s` | 0.5 |
| `min_output` | 0.1 |
| `min_hover_s` | 10 |

### `motor.saturation` · Motor saturation

Version 1.1.0 · signals: `mot.1`, `mot.2`

| Setting | Default |
|---|---|
| `limit` | 0.95 |
| `attention_s` | 1 |
| `problem_s` | 5 |

## Pre-flight check: why arming was refused

Signals: none; reads the flight controller's messages. PX4 logs
"Preflight Fail: <reason>" and "Arming denied: ..."; ArduPilot logs
"PreArm: <reason>" and "Arm: <reason>".

If the aircraft never armed, the refusal reasons are the answer to "why did
nothing happen", so they are reported as Attention (the verdict stays
*insufficient data*: there was no flight to judge). If it armed later, the
earlier refusals are summarised in an OK finding for context.

### `preflight.arming` · Arming

Version 1.0.0 · signals: none (events)

| Setting | Default |
|---|---|
| `max_reasons` | 5 |

## Vibration level check

Two signal sources, with their own thresholds because the metrics differ:

- ArduPilot: ``vibe.x/y/z`` (VIBE.VibeX/Y/Z, m/s²). Limits 30 / 60 m/s².
- PX4: ``vibe.metric`` (vehicle_imu_status.accel_vibration_metric, m/s²) from the
  worst IMU. The scale is smaller than ArduPilot's. Limits of 20 / 30 m/s², held
  for at least 3 s, were set from 25 public PX4 logs: healthy flights peaked
  below about 20, and a hexacopter with 27,935 clipping events sat at 21-29.

Method: while armed, find windows where the value stays above a limit for at
least ``min_duration_s`` (gaps up to 1 s are merged) and report the peak in the
worst window. All thresholds are community starting values, not
aircraft-approved limits; tune them per aircraft profile.

### `vibration.level` · Vibration level

Version 1.2.0 · signals: `vibe.x`, `vibe.y`, `vibe.z`

| Setting | Default |
|---|---|
| `attention` | 30 |
| `problem` | 60 |
| `min_duration_s` | 3 |
| `px4_attention` | 20 |
| `px4_problem` | 30 |
| `px4_min_duration_s` | 3 |

## Gyro noise spectrum and notch-filter advice

Signals: gyro.raw.x/y/z (high-rate gyro, before the flight controller's
filters): PX4 sensor_gyro_fifo or sensor_combined, ArduPilot GYR or the batch
sampler (ISBH/ISBD). Analysis in ``flightlens.spectrum``; only time in the air.

The question is whether a strong, narrow noise peak reaches the controller.
The peak's RMS is reduced by the configured gyro low-pass (modelled as a
2nd-order low-pass at IMU_GYRO_CUTOFF / INS_GYRO_FILTER); a notch filter that
covers the peak (dynamic, or fixed within its bandwidth) removes it.

Attention: a peak at least ``min_peak_db`` over the band, and still at least
``attention_rms`` deg/s after the low-pass, with no notch covering it.
Never Problem: filter setup is advice, not a fault. Thresholds are FlightLens
defaults calibrated on 28 public PX4 logs, not community limits.

### `vibration.spectrum` · Gyro noise and filters

Version 1.0.0 · signals: `gyro.raw.x`, `gyro.raw.y`, `gyro.raw.z`

| Setting | Default |
|---|---|
| `min_peak_db` | 15 |
| `attention_rms` | 1 |

## Trend metrics (aircraft pages)

Computed for every flight and compared with the same aircraft's earlier flights (at least 3). A flight is flagged when it is worse than their median by more than both minimums below and by more than 3 robust standard deviations of those flights. Signal metrics need 20 s in the air.

| Metric | Unit | Worse when | Drift: at least | What it shows |
|---|---|---|---|---|
| Health score | – | lower | 20 points | the overall verdict of each flight |
| Vibration | m/s² | higher | 30% and 3 m/s² | 95th percentile of the worst axis in the air: props, motors, mounts |
| IMU clipping | events/min | higher | 50% and 5 events/min | accelerometer saturation per minute in the air |
| Battery internal resistance | mΩ/cell | higher | 25% and 2 mΩ/cell | rises as a pack ages; compare packs by flying them in turn |
| Lowest cell voltage | V | lower | 0.1 V | 1st percentile per cell in the air: pack health and how far it was flown |
| Motor output | % | higher | 10% and 5 % | median mean motor output in the air: more power for the same flight means worn props or motors, or added weight |
| Motor imbalance | % | higher | 50% and 4 % | spread between motors in steady hover: CG, bent arm, a failing motor |
| Estimator innovations | ratio | higher | 50% and 0.2 ratio | 99th percentile of the worst test ratio: sensor disagreement |
| Gyro noise | deg/s | higher | 50% and 0.5 deg/s | the noise peak left after the gyro low-pass |
| Aileron trim | % | higher | 50% and 5 % | aileron needed to fly level (fixed wing): a warped wing, linkage or CG |
| Time in the air | min | (context only) | – | for context |
