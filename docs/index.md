# FlightLens

**FlightLens reads a drone flight log after landing and tells you whether the
aircraft is healthy, what went wrong, when, and what to fix first.** It works
on PX4 `.ulg` and ArduPilot `.bin` logs, runs entirely on your own machine, and
explains every finding with the evidence behind it.

![The flight library: a verdict and score per flight](img/library-light.png#only-light)
![The flight library: a verdict and score per flight](img/library-dark.png#only-dark)

## Why, when PX4 and ArduPilot already have log viewers

Flight Review, UAV Log Viewer and MAVExplorer are microscopes: they plot every
signal and leave the diagnosis to an expert. FlightLens is the triage nurse that
comes first:

- **A verdict, not a plot.** Healthy, attention or problem, with a 0–100 health
  score and findings ranked most serious first.
- **A diagnosis, not a list of symptoms.** A crash finding links what went wrong
  earlier in the same flight ("bank beyond the 45° limit at 01:25, aileron at
  full travel at 01:24, out of trim throughout").
- **Health over time, per aircraft.** Flights group by autopilot board; vibration,
  battery internal resistance, motor output and more are tracked across flights,
  and drift against the aircraft's own history is flagged as maintenance due.
- **Advice with numbers.** The noise check gives the notch-filter parameters to
  set; the trim check says how much aileron the aircraft needs to fly level.
- **Honest about missing data.** A check without data is *not evaluated*,
  never OK, and says how to log what it needs.
- **Proven.** Every check is scored against logs with known faults, in CI
  ([validation](validation.md)), and against real crashes
  ([real-log results](real-log-results.md)).

![A crash with its likely causes and the evidence](img/crash-light.png#only-light)
![A crash with its likely causes and the evidence](img/crash-dark.png#only-dark)

## Try it

```bash
pip install flightlens
flightlens demo          # example flights with known faults, at http://localhost:8000
flightlens analyze my-flight.bin
```

Next: [Getting started](getting-started.md) · [How it works](how-it-works.md) ·
[Check reference](checks.md)
