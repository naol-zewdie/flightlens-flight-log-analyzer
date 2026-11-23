# Results on real PX4 logs

Corpus: 25 recent public logs from PX4 Flight Review plus 3 pyulog sample logs (28 in total). They cover firmware v1.6 to v1.17, quadrotors, hexarotors, standard VTOLs, SITL, and flights from 0 s to 22 min. Downloaded with `scripts/fetch_px4_logs.py` on 2026-11-10; the logs are not committed.

## Robustness

| Measure | Result |
|---|---|
| Logs parsed and analyzed | 28 / 28, 0 crashes |
| Mean analysis time | 0.4 s per log (22-min, 101 MB log: 3.6 s) |
| Truncated to 60% and 5% | analyzed; flagged "log ends while armed" |
| 200-byte stub | *insufficient data*, never healthy |
| Valid header + random bytes | rejected with a clear message |

## Verdicts

Healthy 11 · Attention 9 · Problem 4 · Insufficient data 4.

Problems found:

| Log | What FlightLens reports | Assessment |
|---|---|---|
| `69531256` quad, v1.12 | Failure detector "roll angle exceeded" at 06:02, 306 clipping events, log ends armed | A crash. The firmware's own detector agrees. |
| `9c90f86e` hexa, v1.14, 22 min | 27,935 clipping events, vibration 21–29 m/s² throughout (peak 43), 3.20 V/cell at the end | Strong vibration problem; long flight ran the pack low. |
| `2f286ced`, `ac92641d` VTOL | Failure detector "motor failure"; one motor well below the others | Reported by the firmware itself. |

## Calibration changes made from this corpus

Each change was made because a real log produced a wrong or misleading result.

1. **PX4 vibration uses `accel_vibration_metric`, not raw accelerometer data.** The raw accelerometer is logged at ~200 Hz, too slow to measure vibration without aliasing. The metric's scale is smaller than ArduPilot's VIBE: healthy flights stayed below ~20 m/s². New limits: 20 / 30 m/s², held 3 s.
2. **An all-zero vibration metric means "not computed", not "no vibration"**, so the check is not evaluated.
3. **Never-armed logs, very short logs, and logs where fewer than 3 checks ran are *insufficient data*.** Before this, a 200-byte stub and a never-armed SITL log both scored "healthy 100".
4. **Attitude tracking only in the air, with the attitude controller active, and windows must be continuous.** Ground contact at landing and aggressive stick flying (setpoint at the 35° tilt limit, error dropping back within fractions of a second) had produced false Problems.
5. **Estimator heading ratio is ignored for 10 s after takeoff.** PX4 re-aligns yaw in flight right after takeoff, which spiked the ratio to 2.0 on a healthy 10-minute flight.
6. **The estimator check runs on whichever test ratios exist.** PX4 leaves the GPS ratios empty when GPS is not fused (indoor and vision flights).
7. **Within a severity, firmware-reported failures are listed first**, because they are usually the cause of the other findings.

## Still not evaluated often

| Check | Not evaluated | Why |
|---|---|---|
| Battery voltage | 17 / 28 | No power module (≈0 V), cell count set to 1, or no battery topic |
| GPS quality | 13 / 28 | Indoor, vision or SITL flights without a GPS fix |
| Motor balance | 10 / 28 | Less than 10 s of steady hover |

These are honest gaps in the logs, not parser errors. The verdict never treats them as passes.

## Not yet validated

- No labelled ground truth: the assessments above are engineering judgement from the data, not confirmed by the pilots.
- The thresholds are community starting values tuned on 28 logs; they are not approved for any specific aircraft.
- Injected-fault SITL logs (see [sitl-log-library.md](sitl-log-library.md)) are still needed for the detection-rate target (≥ 90%).

## ArduPilot `.bin`: 15 real ArduPlane logs

Owner's own 6S fixed wing, ArduPlane V4.6.2 (logs not committed). Owner's account:
runway takeoff, a short flight, the aircraft banks to one side and crashes; a
tuning/control problem.

**Before calibration** 13 of 15 were PROBLEM, mostly for the wrong reasons:
"poor yaw tracking" (ArduPlane logs no yaw setpoint, `DesYaw` = 0) and
"vibration too high" from single impact spikes (80–104 m/s² for a fraction of
a second at touchdown or crash).

**Changes made from these logs:**

1. Airborne time from `STAT.isFlying`; arming from `STAT.Armed` when ARM/EV
   records are missing (one log had none).
2. Yaw never tracked on fixed wing; an axis whose setpoint never changes is skipped.
3. ArduPilot vibration must hold 3 s in the air; impact spikes are excluded from
   vibration, clipping and battery checks and handled by a new crash check.
4. New checks: crash or hard landing (with the earlier findings of the same flight
   linked as likely causes), attitude beyond the configured limit, control surface
   at full travel, out of trim.
5. The battery finding estimates internal resistance from voltage against current.

**Result:**

| | Logs |
|---|---|
| Crash detected (impact at the end of the flight, then upset, rest on its side, or log cut off) | 6 |
| Hard landing (impact, upright) | 2 |
| Never airborne / no flight | 2 |
| False "yaw tracking" or single-spike "vibration" Problems | 0 (was 13) |

The clearest crash reads: *Crash at 08:10, came to rest at 99° roll, log ends 2 s
later. Earlier in this flight: battery sagged (06:16), bank beyond the 45° limit
for 19 s (06:28), aileron at full travel (07:53). Throughout: out of trim, 19% left
aileron needed to fly level.* Across the flights the battery fell to about 2.9–3.1
V per cell at 60–79 A, an internal resistance of about 15 mΩ per cell, several
times a healthy pack: likely a contributing factor.

These are labels from the owner's account and the data, not an independent
investigation. They are kept as regression tests (`tests/test_real_logs.py` with
labels in the gitignored `private-logs/`).

## PX4 SITL: 8 attempted flights that never flew

PX4 v1.17 SITL sessions where the owner tried to fly normally. Before: 5
"insufficient data" with no reason, 3 with "motor at full power / motor works less
/ poor roll tracking" recorded while the vehicle sat on the ground.

After: 4 of the 5 never-armed logs say why (**"Arming was refused: height estimate
not stable"**; the fifth logged no reason), and the 3 others say **"Takeoff failed:
Takeoff mode engaged but the altitude never changed by 1 m"**, with the motor and
attitude symptoms suppressed because the aircraft was never in the air.

## Effect on the 28 PX4 logs

Only one result changed: the known crash (`69531256`) is now also reported as a
Crash, with the bank beyond its configured limit linked as a cause.

## MAVLink `.tlog`: 71 real Mission Planner telemetry logs

The owner's ground-station logs (not committed): 14 from a fixed wing
(ArduPlane 4.5.7 and 4.6.2), 57 from a quadrotor (ArduCopter 4.6.3, 4.7.0,
4.7.2, 4.7.3, and a Betaflight-style flight controller that reports a generic
MAVLink autopilot), plus 5 onboard `.bin` logs from the same sessions.

- **Decoding:** all 71 read, in 0.1-5.5 s (the largest, 100 MB, was mostly a
  log download over the link: 924,355 of 945,765 packets are messages
  FlightLens does not need). Radio loss from the sequence numbers: 0-8%.
- **What they contain:** almost all are bench and setup sessions. The vehicle's
  HEARTBEAT armed flag is never set in 68 of them, and the onboard logs of the
  same day agree. FlightLens calls them *insufficient data* and, for 64 of
  them, quotes why arming was refused (`PreArm: RC not found`, `Compass not
  calibrated`, `EKF3 Roll/Pitch inconsistent`, `Battery 1 below minimum arming
  voltage`, Betaflight's `Arming disabled: THROTTLE ANGLE`...).
- **The three armed sessions:** a ground run-up with arming checks disabled
  (throttle to 95%, no climb), recorded on two links at once: both tlogs say
  *did not take off*; and a 21 s taxi that never climbed: *insufficient data*.

Fixes made from these logs, all of them general (every format):

| What the real data showed | Change |
|---|---|
| Prop-wash on the barometer moved the 2 Hz altitude by ±0.5 m per sample during a run-up, so min-to-max crossed the 1 m "it climbed" rule and the run-up counted as a flight (*healthy*). | The climb is measured after a running median (at least 5 samples and 0.5 s, edges padded). A real 1.1 m launch climb in the last second of an ArduPlane log still counts. |
| A taxi was armed for 21 s with good checks, and was called *healthy*. | *Healthy* needs 10 s in the air, not 10 s armed, when the log tells when the aircraft flew. |
| GPS findings came from time on the ground, unlike every other check. | GPS is judged in the air. |
| Betaflight's arming blockers were not recognised. | `Arming disabled: <flags>` counts as an arming refusal. |

On the earlier corpus these changes alter only what they should: GPS findings
from the ground disappear on three ArduPlane logs, and one PX4 log that never
left the ground (0.2 m) is *insufficient data* instead of *healthy*. The owner's
labelled set (now 92 logs, including these) is fully correct.

## Gyro noise spectrum (`vibration.spectrum`)

Calibrated on the 28 PX4 logs; thresholds are FlightLens defaults (a peak 15 dB
over the band and still 1 deg/s RMS after the configured gyro low-pass), not
community limits. The check never reports a Problem: filter setup is advice.

| Gyro data in the log | Logs | Result |
|---|---|---|
| Raw FIFO, 1 to 8 kHz | 5 | 2 Attention (VTOLs with the gyro low-pass off and the notch at 9 to 20 Hz, noise at 87 Hz); 2 covered by a notch (dynamic, or fixed at 80 Hz); 1 too short |
| `sensor_combined`, about 200 Hz | 17 | 2 Attention (83 Hz, and a 64 deg/s peak at 57 Hz on yaw), 3 strong peaks the low-pass already removes, 10 with no dominant peak, 2 below 180 Hz (not evaluated) |
| none usable | 6 | not evaluated, with how to enable high-rate logging |

Only one verdict changed: `2d145d43` from healthy to Attention, an 83 Hz peak
seen in 222 Hz data. The finding says the frequency may be an alias and asks
for raw gyro logging before a notch is set. The 15 ArduPlane logs have no
high-rate gyro (IMU at 25 to 50 Hz, no batch sampler), so the check explains how
to log it; the ArduPilot paths (batch sampler and `GYR`) are verified on
generated logs only.
