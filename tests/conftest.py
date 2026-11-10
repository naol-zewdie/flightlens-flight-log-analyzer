from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pytest

from flightlens.model import Flight, FlightMetadata, Segment, Signal

FlightFactory = Callable[..., Flight]


def make_signal(name: str, t: np.ndarray, values: np.ndarray, unit: str = "") -> Signal:
    return Signal(
        name=name,
        unit=unit,
        t=np.asarray(t, dtype=np.float64),
        values=np.asarray(values, dtype=np.float64),
    )


@pytest.fixture
def make_flight() -> FlightFactory:
    def _make(
        signals: dict[str, tuple[np.ndarray, np.ndarray]] | None = None,
        segments: list[tuple[float, float]] | None = None,
        unit: str = "",
    ) -> Flight:
        sigs = {n: make_signal(n, t, v, unit) for n, (t, v) in (signals or {}).items()}
        return Flight(
            metadata=FlightMetadata(log_format="synthetic", vehicle_type="Quad"),
            signals=sigs,
            segments=[Segment(a, b) for a, b in (segments or [])],
        )

    return _make


def vibe_flight(
    make_flight: FlightFactory,
    z: np.ndarray,
    t: np.ndarray,
    segments: list[tuple[float, float]] | None = None,
    xy: float = 10.0,
) -> Flight:
    """A flight with vibration signals; X and Y held at a constant level."""
    const = np.full_like(t, xy)
    return make_flight(
        {"vibe.x": (t, const), "vibe.y": (t, const), "vibe.z": (t, z)},
        segments=segments,
        unit="m/s²",
    )
