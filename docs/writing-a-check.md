# Writing a check

A check is a class with an id, a version, the signals it needs, default
thresholds and a `run` method. Put the file in `src/flightlens/checks/`; it is
discovered automatically.

```python
"""My check.

Signals: bat.volt. Attention below 3.5 V per cell for 1 s in the air, Problem
below 3.3 V.
"""

from flightlens.checks import register
from flightlens.checks._util import in_air_mask
from flightlens.model import CheckConfig, Finding, Flight, Severity


@register
class MyCheck:
    id = "my.check"
    title = "My check"
    version = "1.0.0"  # bump when logic or thresholds change
    required_signals = ("bat.volt",)
    defaults = {"attention": 3.5, "problem": 3.3}

    def run(self, flight: Flight, config: CheckConfig) -> list[Finding]:
        volt = flight.signal("bat.volt")
        air = in_air_mask(flight, volt.t)
        ...
        return [Finding(self.id, Severity.OK, "Battery voltage fine", "...")]
```

Rules that keep results honest:

- Return `NOT_EVALUATED` with the reason when the data is not good enough;
  never `OK` by default.
- Judge time in the air (`in_air_mask`) and leave impacts to the crash check
  (`outside(t, impact_windows(flight))`).
- Put the measured value, the limit and the time window in the finding, and the
  signals that show it in `evidence_signals`.
- The module docstring becomes the [check reference](checks.md): say what is
  measured and when it is Attention or Problem.

Test it on synthetic flights (`tests/conftest.py` has a `make_flight` factory)
and add a scenario to the benchmark (`flightlens/sim/benchmark.py`) so CI
measures its recall and false alarms.
