"""Regression tests on real or simulated logs that live outside the repository.

Each ``private-logs/*.yaml`` (gitignored) is a validation manifest (format in
``flightlens/validate.py``) pointing at a folder of logs. Missing folders or
files are skipped, so these tests run only where the logs are. The same files
work with ``flightlens validate private-logs``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from flightlens import validate

LABELS = sorted((Path(__file__).parent.parent / "private-logs").glob("*.yaml"))
CASES = [pytest.param(c, id=c.id) for m in LABELS for c in validate.load_manifest(m)]


@pytest.mark.skipif(not CASES, reason="no private-logs/*.yaml label files")
@pytest.mark.parametrize("case", CASES)
def test_labelled_log(case: validate.Case) -> None:
    if not case.path.exists():
        pytest.skip(f"{case.path} not present on this machine")
    outcome = validate.evaluate(case)
    got = (
        [(f.check_id, f.severity.value, f.title) for f in outcome.report.findings]
        if outcome.report
        else []
    )
    assert outcome.passed, (outcome.problems(), got)
