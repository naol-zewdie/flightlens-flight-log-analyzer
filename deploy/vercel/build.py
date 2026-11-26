"""Build the demo library shipped with the Vercel function (run by Vercel at deploy time).

Generates the demo flights (each with a known fault) and analyzes them with the
installed FlightLens, so the demo always matches the version it runs.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from flightlens import __version__
from flightlens.demo import seed

OUT = Path(__file__).resolve().parent / "demo-data"

if __name__ == "__main__":
    shutil.rmtree(OUT, ignore_errors=True)
    ids = seed(OUT, progress=lambda name: print(f"  analyzing {name}", flush=True))
    print(f"FlightLens {__version__}: {len(ids)} demo flights in {OUT}")
