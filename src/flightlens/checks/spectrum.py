"""Gyro noise spectrum and notch-filter advice.

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
"""

from __future__ import annotations

import numpy as np

from flightlens.checks.base import register
from flightlens.model import CheckConfig, Finding, Flight, Severity
from flightlens.spectrum import Spectrum, analyze_spectrum

LOW_RATE_HZ = 180.0
"""Below this the gyro log cannot show typical motor noise (above about 80 Hz)."""

PX4_LOGGING = (
    "PX4: enable 'Raw FIFO high-rate IMU (Gyro)' in SDLOG_PROFILE. "
    "ArduPilot: set INS_LOG_BAT_MASK = 1 (batch sampler, first IMU), or raw IMU logging "
    "with INS_RAW_LOG_OPT on 4.5 and later."
)


def lowpass_gain(f: float, cutoff: float | None) -> float:
    if not cutoff or cutoff <= 0:
        return 1.0
    return float(1 / np.sqrt(1 + (f / cutoff) ** 4))


def _ardupilot_notch(p: dict[str, float], f: float, hover: float | None) -> tuple[bool, str]:
    """(covered, description) for INS_HNTCH_* and INS_HNTC2_*."""
    notes = []
    for prefix in ("INS_HNTCH_", "INS_HNTC2_"):
        if p.get(prefix + "ENABLE", 0) < 1:
            continue
        mode = int(p.get(prefix + "MODE", 0))
        freq, bw = p.get(prefix + "FREQ", 0.0), p.get(prefix + "BW", 0.0)
        ref = p.get(prefix + "REF", 0.0)
        if mode >= 2:  # RPM, ESC telemetry or in-flight FFT follow the motors themselves
            return True, f"dynamic notch ({prefix}MODE={mode})"
        centre = freq
        if mode == 1 and ref > 0 and hover:
            centre = freq * np.sqrt(hover / ref)
        if freq > 0 and abs(centre - f) <= max(bw / 2, 5.0):
            return True, f"notch at {centre:.0f} Hz ({prefix}FREQ={freq:g})"
        notes.append(f"{prefix}FREQ={freq:g} Hz centres the notch at {centre:.0f} Hz")
    return False, "; ".join(notes)


def _px4_notch(p: dict[str, float], f: float) -> tuple[bool, str]:
    dnf = int(p.get("IMU_GYRO_DNF_EN", 0))
    if dnf > 0:
        return True, f"dynamic notch (IMU_GYRO_DNF_EN={dnf})"
    notes = []
    for n in (0, 1):
        freq, bw = p.get(f"IMU_GYRO_NF{n}_FRQ", 0.0), p.get(f"IMU_GYRO_NF{n}_BW", 0.0)
        if freq > 0:
            if abs(freq - f) <= max(bw / 2, 5.0):
                return True, f"notch at {freq:g} Hz (IMU_GYRO_NF{n}_FRQ)"
            notes.append(f"IMU_GYRO_NF{n}_FRQ={freq:g} Hz does not cover {f:.0f} Hz")
    return False, "; ".join(notes)


def _recommend(s: Spectrum, ardupilot: bool, multirotor_hover: float | None) -> str:
    f = s.hover_hz or s.peak_hz or 0.0
    harm = s.harmonics
    if ardupilot:
        if s.tracks_throttle is False:
            text = (
                f"The peak does not move with throttle (frame resonance): a fixed notch, "
                f"INS_HNTCH_ENABLE=1, INS_HNTCH_MODE=0, INS_HNTCH_FREQ={f:.0f}, "
                f"INS_HNTCH_BW={f / 2:.0f}."
            )
        else:
            ref = f", INS_HNTCH_REF={multirotor_hover:.2f}" if multirotor_hover else ""
            text = (
                f"A throttle-based harmonic notch: INS_HNTCH_ENABLE=1, INS_HNTCH_MODE=1 "
                f"(3 with ESC telemetry), INS_HNTCH_FREQ={f:.0f}, INS_HNTCH_BW={f / 2:.0f}{ref}."
            )
        if harm:
            text += " Harmonics are present: INS_HNTCH_HMNCS=3 covers the 2nd as well."
    else:
        text = (
            "A dynamic notch: IMU_GYRO_DNF_EN=1 if the ESCs report RPM, otherwise "
            f"IMU_GYRO_FFT_EN=1 and IMU_GYRO_DNF_EN=2; or a fixed notch, IMU_GYRO_NF0_FRQ={f:.0f} "
            f"and IMU_GYRO_NF0_BW={max(10.0, f / 3):.0f}."
        )
    text += " Check props and motor mounts first: removing the source beats filtering it."
    return text


@register
class GyroSpectrum:
    id = "vibration.spectrum"
    title = "Gyro noise and filters"
    version = "1.0.0"
    required_signals = ("gyro.raw.x", "gyro.raw.y", "gyro.raw.z")
    defaults = {"min_peak_db": 15.0, "attention_rms": 1.0}
    missing_explanation = (
        "The log has no high-rate gyro data, so the noise spectrum (where motor and "
        "propeller noise sits, and which filter would remove it) cannot be computed."
    )
    missing_recommendation = "To get it next time: " + PX4_LOGGING

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        s = analyze_spectrum(flight)
        evidence = ("gyro.raw.x", "gyro.raw.y", "gyro.raw.z")
        if s is None:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Gyro noise not evaluated",
                    "Less than 10 s of evenly sampled gyro data in the air.",
                    evidence_signals=evidence,
                )
            ]
        if s.fs < LOW_RATE_HZ:
            return [
                Finding(
                    self.id,
                    Severity.NOT_EVALUATED,
                    "Gyro noise not evaluated",
                    f"The gyro was logged at only {s.fs:.0f} Hz, so the spectrum stops at "
                    f"{s.nyquist:.0f} Hz, below where motor noise usually sits.",
                    recommendation="To log it at sensor rate: " + PX4_LOGGING,
                    evidence_signals=evidence,
                )
            ]

        p = flight.parameters
        ardupilot = flight.metadata.log_format == "dataflash"
        source = f"{s.source}, {s.fs:.0f} Hz, {s.air_s:.0f} s in the air"
        aliased = s.fs < 400
        alias_note = (
            f" At {s.fs:.0f} Hz logging, noise above {s.nyquist:.0f} Hz appears folded back, so "
            "this frequency may be an alias; log raw gyro before setting a notch."
            if aliased
            else ""
        )
        if s.peak_hz is None or s.peak_db < config["min_peak_db"]:
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    "No dominant gyro noise peak",
                    f"The gyro noise has no narrow peak above 30 Hz ({source}).",
                    measured=round(s.peak_db, 1),
                    threshold=config["min_peak_db"],
                    comparison="<",
                    unit="dB",
                    evidence_signals=evidence,
                )
            ]

        f = s.peak_hz
        cutoff = p.get("INS_GYRO_FILTER") if ardupilot else p.get("IMU_GYRO_CUTOFF")
        residual = s.peak_rms * lowpass_gain(f, cutoff)
        target = s.hover_hz or f  # a throttle-following notch is set for hover
        covered, notch = (
            _ardupilot_notch(p, target, s.hover_throttle) if ardupilot else _px4_notch(p, target)
        )
        harm = (
            f", harmonics at {', '.join(f'{h:.0f}' for h in s.harmonics)} Hz" if s.harmonics else ""
        )
        at_hover = f" ({s.hover_hz:.0f} Hz at hover)" if s.hover_hz else ""
        follows = {
            True: f" It rises with throttle{at_hover}: motor or propeller noise.",
            False: " It stays put as throttle changes: likely a frame resonance.",
            None: "",
        }[s.tracks_throttle]
        lpf = (
            "with the gyro low-pass disabled"
            if cutoff is not None and cutoff <= 0
            else f"after the {cutoff:g} Hz gyro low-pass"
            if cutoff
            else "(no low-pass setting in the log)"
        )
        head = (
            f"Noise peak at {f:.0f} Hz on the {s.peak_axis} axis, {s.peak_db:.0f} dB over the "
            f"band{harm}: {s.peak_rms:.1f} deg/s RMS, about {residual:.1f} deg/s {lpf}.{follows}"
        )
        if covered:
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    f"Noise at {f:.0f} Hz is covered by the notch filter",
                    f"{head} Covered by the {notch} ({source}).",
                    measured=round(residual, 2),
                    unit="deg/s",
                    evidence_signals=evidence,
                )
            ]
        if residual < config["attention_rms"]:
            return [
                Finding(
                    self.id,
                    Severity.OK,
                    f"Gyro noise peak at {f:.0f} Hz is filtered enough",
                    f"{head} ({source}).{alias_note}",
                    measured=round(residual, 2),
                    threshold=config["attention_rms"],
                    comparison="<",
                    unit="deg/s",
                    evidence_signals=evidence,
                )
            ]
        why = f" {notch}." if notch else " No notch filter is configured."
        return [
            Finding(
                self.id,
                Severity.ATTENTION,
                f"Gyro noise at {f:.0f} Hz reaches the controller",
                f"{head}{why} ({source}).{alias_note}",
                recommendation=_recommend(s, ardupilot, s.hover_throttle),
                measured=round(residual, 2),
                threshold=config["attention_rms"],
                unit="deg/s",
                evidence_signals=evidence,
            )
        ]
