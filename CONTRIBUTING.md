# Contributing

Thanks for helping make FlightLens better. Bug reports with a log that shows
the problem are the most useful contribution of all.

## Set up

You need Python 3.12+, [uv](https://docs.astral.sh/uv/) and Node 20+.

```bash
uv sync                               # Python and dev tools
cd web && npm ci && npm run build     # the web UI
uv run flightlens demo                # example flights at http://localhost:8000
```

## Before you open a pull request

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest
cd web && npm run build
```

CI runs the same on Linux, Windows and macOS, plus the fault benchmark, a
dependency audit and a Docker build.

## Adding or changing a check

Read [docs/writing-a-check.md](docs/writing-a-check.md). In short:

- A check without the data it needs reports **not evaluated**, never OK.
- Bump the check's `version` whenever its logic or defaults change; reports
  record it, and older analyses are then marked outdated.
- Set thresholds from real logs where possible and say where they came from in
  the module docstring. Add a benchmark scenario in
  `src/flightlens/sim/benchmark.py` with the fault and, if it has a limit, a
  healthy flight just below it.
- Run `uv run python scripts/gen_check_docs.py` and
  `uv run python scripts/benchmark.py` to refresh the generated docs.

## Mappings

Firmware-specific names live only in
`src/flightlens/parsers/mappings/*.yaml`. Bump a mapping's `version` whenever
you change it.

## Logs

**Never commit private or company flight logs**, nor details that identify an
aircraft or operator. Use the generated flights (`flightlens.sim`), the public
sample logs in `testdata/logs/`, or public PX4 Flight Review logs
(`scripts/fetch_px4_logs.py`, gitignored). Your own labelled logs belong in
`private-logs/`, which is gitignored.

## Reporting security problems

See [SECURITY.md](SECURITY.md).
