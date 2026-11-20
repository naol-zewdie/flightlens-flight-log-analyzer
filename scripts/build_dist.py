"""Build the release artifacts: the web UI, then a wheel and sdist that bundle it.

    uv run python scripts/build_dist.py            # npm ci + build, then uv build
    uv run python scripts/build_dist.py --no-npm   # reuse an existing web/dist

The wheel carries the UI as ``flightlens/web``, so ``pip install flightlens``
then ``flightlens serve`` works without Node. Fails if the wheel lacks the UI,
the signal mappings or the version is inconsistent.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NPM = "npm.cmd" if sys.platform == "win32" else "npm"


def run(cmd: list[str], cwd: Path = ROOT) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--no-npm", action="store_true", help="reuse web/dist as it is")
    args = ap.parse_args()

    web = ROOT / "web"
    if not args.no_npm:
        run([NPM, "ci", "--no-audit", "--no-fund"], web)
        run([NPM, "run", "build"], web)
    dist_ui = web / "dist"
    if not (dist_ui / "index.html").exists():
        sys.exit("web/dist/index.html missing: build the UI first")
    bundled = ROOT / "src" / "flightlens" / "web"
    shutil.rmtree(bundled, ignore_errors=True)
    shutil.copytree(dist_ui, bundled)

    out = ROOT / "dist"
    shutil.rmtree(out, ignore_errors=True)
    # Separately: by default uv builds the wheel from the sdist, which leaves out
    # the (gitignored) bundled UI.
    run(["uv", "build", "--sdist", "--out-dir", str(out)])
    try:
        run(["uv", "build", "--wheel", "--out-dir", str(out)])
    finally:
        # Only the wheel needs this copy; left in place it would shadow web/dist.
        shutil.rmtree(bundled, ignore_errors=True)

    version = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    init = (ROOT / "src" / "flightlens" / "__init__.py").read_text()
    assert version and f'__version__ = "{version[1]}"' in init, "pyproject and __init__ disagree"
    wheel = next(out.glob("*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    for required in (
        "flightlens/web/index.html",
        "flightlens/parsers/mappings/px4.yaml",
        "flightlens/parsers/mappings/ardupilot.yaml",
        "flightlens/py.typed",
    ):
        assert required in names, f"{required} missing from {wheel.name}"
    print(f"ok: {wheel.name} ({wheel.stat().st_size / 1e6:.1f} MB), {len(names)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
