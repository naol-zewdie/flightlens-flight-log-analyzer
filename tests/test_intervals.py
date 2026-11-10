from __future__ import annotations

import numpy as np

from flightlens.intervals import format_time, intervals_where

T = np.arange(10, dtype=np.float64)


def mask(*idx: int) -> np.ndarray:
    m = np.zeros(10, dtype=bool)
    m[list(idx)] = True
    return m


def test_no_true_samples() -> None:
    assert intervals_where(T, mask()) == []


def test_separate_runs() -> None:
    assert intervals_where(T, mask(1, 2, 6, 7, 8)) == [(1.0, 2.0), (6.0, 8.0)]


def test_runs_merge_within_gap() -> None:
    assert intervals_where(T, mask(1, 2, 4, 5), merge_gap=2.0) == [(1.0, 5.0)]


def test_format_time() -> None:
    assert format_time(130) == "02:10"
    assert format_time(3725) == "1:02:05"
    assert format_time(None) == "--:--"
