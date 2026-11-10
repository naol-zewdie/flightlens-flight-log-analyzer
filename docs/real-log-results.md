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
