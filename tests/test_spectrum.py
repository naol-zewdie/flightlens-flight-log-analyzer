"""Gyro noise spectrum (flightlens.spectrum) and the vibration.spectrum check."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest

from flightlens import parsers
from flightlens.checks.spectrum import _ardupilot_notch, _px4_notch, lowpass_gain
from flightlens.engine import analyze
from flightlens.model import Flight, FlightMetadata, Severity
from flightlens.sim.benchmark import Fault, copter
from flightlens.spectrum import analyze_spectrum

from .conftest import FlightFactory

FS = 1000.0


def _gyro_flight(
    make_flight: FlightFactory,
    tones: list[tuple[float, float]],
    *,
    seconds: float = 60.0,
    fs: float = FS,
    thr: Any = None,
    params: dict[str, float] | None = None,
) -> Flight:
    """Gyro x/y/z in deg/s with sine tones (Hz, amplitude) on x, airborne 0..seconds."""
    rng = np.random.default_rng(3)
    t = np.arange(0, seconds, 1 / fs)
    x = rng.normal(0, 0.5, t.size)
    for f, a in tones:
        x += a * np.sin(2 * np.pi * (f(t) if callable(f) else f) * t)
    sig: dict[str, tuple[np.ndarray, np.ndarray]] = {
        "gyro.raw.x": (t, x),
        "gyro.raw.y": (t, rng.normal(0, 0.5, t.size)),
        "gyro.raw.z": (t, rng.normal(0, 0.5, t.size)),
    }
    if thr is not None:
        tt = np.arange(0, seconds, 0.1)
        sig["ctrl.thr"] = (tt, thr(tt))
    flight = make_flight(signals=sig, segments=[(0.0, seconds)])
    flight.arming_known = True
    flight.metadata = FlightMetadata(log_format="ulog", vehicle_type="Quadrotor")
    flight.parameters = params or {}
    return flight


def _spec_finding(flight: Flight) -> Any:
    return next(f for f in analyze(flight).findings if f.check_id == "vibration.spectrum")


def test_peak_harmonics_and_rms(make_flight: FlightFactory) -> None:
    s = analyze_spectrum(_gyro_flight(make_flight, [(150, 4.0), (300, 1.5)]))
    assert s is not None
    assert s.fs == pytest.approx(FS, rel=1e-6)
    assert s.peak_hz == pytest.approx(150, abs=2)
    assert s.peak_axis == "x"
    assert s.peak_rms == pytest.approx(4.0 / np.sqrt(2), rel=0.15)  # RMS of a sine
    assert s.harmonics and s.harmonics[0] == pytest.approx(300, abs=4)
    d = s.to_dict()
    assert len(d["spectrogram"]["t"]) == len(d["spectrogram"]["db"])
    assert set(d["psd_db"]) == {"x", "y", "z"}


def test_noise_that_follows_throttle_is_motor_noise(make_flight: FlightFactory) -> None:
    def throttle(t: Any) -> Any:
        return 0.5 + 0.2 * np.sin(2 * np.pi * t / 30)

    def freq(t: Any) -> Any:
        return 120 * np.sqrt(throttle(t) / 0.5)

    # instantaneous frequency of sin(2π∫f dt): integrate f numerically
    flight = _gyro_flight(make_flight, [], seconds=90, thr=throttle)
    x = flight.signals["gyro.raw.x"]
    phase = 2 * np.pi * np.cumsum(freq(x.t)) / FS
    flight.signals["gyro.raw.x"] = type(x)(x.name, x.unit, x.t, x.values + 5 * np.sin(phase))
    s = analyze_spectrum(flight)
    assert s is not None and s.tracks_throttle is True
    assert s.hover_hz == pytest.approx(120, abs=8)


def test_constant_throttle_does_not_claim_resonance(make_flight: FlightFactory) -> None:
    s = analyze_spectrum(_gyro_flight(make_flight, [(150, 4.0)], thr=lambda t: 0.5 + 0 * t))
    assert s is not None and s.tracks_throttle is None


def test_check_attention_without_notch_and_ok_with_one(make_flight: FlightFactory) -> None:
    loud = _gyro_flight(make_flight, [(150, 6.0)], params={"IMU_GYRO_CUTOFF": 80})
    f = _spec_finding(loud)
    assert f.severity is Severity.ATTENTION
    assert "150 Hz" in f.title and "No notch filter" in f.explanation
    assert "IMU_GYRO_NF0_FRQ=150" in f.recommendation
    notched = _gyro_flight(
        make_flight,
        [(150, 6.0)],
        params={"IMU_GYRO_CUTOFF": 80, "IMU_GYRO_NF0_FRQ": 148, "IMU_GYRO_NF0_BW": 30},
    )
    assert _spec_finding(notched).title == "Noise at 150 Hz is covered by the notch filter"
    filtered = _gyro_flight(make_flight, [(150, 6.0)], params={"IMU_GYRO_CUTOFF": 20})
    assert _spec_finding(filtered).severity is Severity.OK  # the low-pass takes it below 1 deg/s
    quiet = _gyro_flight(make_flight, [])
    assert _spec_finding(quiet).title == "No dominant gyro noise peak"


def test_low_rate_gyro_is_not_evaluated(make_flight: FlightFactory) -> None:
    f = _spec_finding(_gyro_flight(make_flight, [(40, 3.0)], fs=120))
    assert f.severity is Severity.NOT_EVALUATED
    assert "only 120 Hz" in f.explanation and "SDLOG_PROFILE" in f.recommendation


def test_missing_gyro_says_how_to_log_it(make_flight: FlightFactory) -> None:
    flight = make_flight(segments=[(0.0, 10.0)])
    f = _spec_finding(flight)
    assert f.severity is Severity.NOT_EVALUATED
    assert "high-rate gyro" in f.explanation and "INS_LOG_BAT_MASK" in f.recommendation


def test_notch_coverage_rules() -> None:
    assert lowpass_gain(80, 40) == pytest.approx(1 / np.sqrt(17))
    assert lowpass_gain(80, 0) == 1.0
    assert _px4_notch({"IMU_GYRO_DNF_EN": 1}, 200)[0]
    assert not _px4_notch({"IMU_GYRO_NF0_FRQ": 9, "IMU_GYRO_NF0_BW": 5}, 87)[0]
    thr_notch = {
        "INS_HNTCH_ENABLE": 1,
        "INS_HNTCH_MODE": 1,
        "INS_HNTCH_FREQ": 100,
        "INS_HNTCH_BW": 40,
        "INS_HNTCH_REF": 0.36,
    }
    # throttle mode: centre = FREQ * sqrt(hover / REF) = 100 * sqrt(0.49 / 0.36) ≈ 117 Hz
    assert _ardupilot_notch(thr_notch, 117, 0.49)[0]
    covered, why = _ardupilot_notch(thr_notch, 200, 0.49)
    assert not covered and "centres the notch at 117 Hz" in why
    assert _ardupilot_notch({**thr_notch, "INS_HNTCH_MODE": 3}, 200, 0.49)[0]


# --- ArduPilot batch sampler end to end ---------------------------------------------


def test_batch_sampler_is_read_and_diagnosed(tmp_path: Path) -> None:
    flight = parsers.load(copter(tmp_path / "noise.bin", Fault("gyro_noise", level=12.0), 7))
    assert flight.signals["gyro.raw.x"].source.startswith("ISBD batch sampler")
    s = analyze_spectrum(flight)
    assert s is not None and s.fs == pytest.approx(2000, rel=1e-3)
    assert s.tracks_throttle is True and s.hover_hz == pytest.approx(119, abs=6)
    f = next(x for x in analyze(flight).findings if x.check_id == "vibration.spectrum")
    assert f.severity is Severity.ATTENTION
    assert "INS_HNTCH_MODE=1" in f.recommendation and "INS_HNTCH_HMNCS=3" in f.recommendation


def test_px4_sensor_combined_fallback() -> None:
    path = Path(__file__).parent.parent / "testdata" / "logs" / "px4-v1.13-sitl-tagged.ulg"
    flight = parsers.load(path)
    gx = flight.signals["gyro.raw.x"]
    assert gx.unit == "deg/s" and gx.source in (
        "sensor_combined.gyro_rad",
        "sensor_gyro_fifo[0] (raw)",
    )
    assert np.all(np.diff(gx.t) >= 0)
