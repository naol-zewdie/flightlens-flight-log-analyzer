# Getting started

## Install

=== "pip"

    ```bash
    pip install flightlens        # Python 3.12 or later; the web UI is included
    ```

=== "Docker"

    ```bash
    docker run -d -p 127.0.0.1:8000:8000 -v flightlens-data:/data ghcr.io/OWNER/flightlens
    ```

    Images are built for `linux/amd64` and `linux/arm64` (a Raspberry Pi or a
    ground-station laptop). Replace `OWNER` with the GitHub account that
    publishes the release.

=== "From source"

    ```bash
    uv sync
    cd web && npm ci && npm run build && cd ..
    uv run flightlens serve
    ```

## See it on example flights

```bash
flightlens demo
```

This generates nine flights, each with one known fault (vibration, a motor
failure, GPS loss, a sagging battery, gyro noise without a notch filter, a
refused arming, a fixed-wing crash and a hard landing) plus a healthy one, and
opens them at <http://localhost:8000>. For a public, read-only demo:

```bash
flightlens demo --host 0.0.0.0 --allow-host demo.example.org --read-only
```

## Analyze a log

```bash
flightlens analyze flight.ulg              # verdict and findings
flightlens analyze flight.bin --json r.json
flightlens batch logs/ --csv summary.csv   # a whole folder
flightlens signals flight.bin              # what the log contains
```

Exit codes make it usable in CI or a post-flight script: `0` healthy,
`1` attention, `2` problem, `3` insufficient data or unreadable log, `64` bad
arguments.

## The web app

```bash
flightlens serve                 # http://localhost:8000, flights in ./data
```

Drop `.ulg` or `.bin` files on the page. Each flight gets an overview (findings
with their evidence, flight path, timeline), a signal explorer, a noise
spectrum, events and a data-provenance page. The server answers only on
`localhost` unless you pass `--host 0.0.0.0 --allow-host <name>`.

## Check FlightLens against your own logs

Label what each log should and should not produce in a YAML manifest, then score
it; see [Validation](validation.md) and the [labelled log library](sitl-log-library.md).

```bash
flightlens validate my-labels.yaml --markdown results.md
```
