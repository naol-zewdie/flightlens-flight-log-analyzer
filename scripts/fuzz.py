"""Mutation fuzzer for the log parsers and the checks.

Takes valid logs (generated ArduPilot logs from ``flightlens.sim`` and the
PX4 samples in ``testdata/logs``), damages them in the ways real files get
damaged (cut off, bit flips, zeroed or duplicated blocks, inserted junk,
corrupted FMT definitions) and requires, for every mutant:

- ``parsers.load`` returns a flight or raises ``ParseError``, nothing else;
- ``analyze`` never has a check that crashes (an ``error`` result);
- neither takes longer than ``--slow`` seconds (a hang is reported with a
  stack trace and the run stops).

Failing inputs are saved to ``--out`` so they can be replayed and turned into
regression tests. CI runs a short Hypothesis version of this
(``tests/test_fuzz.py``); run this script for long campaigns::

    uv run python scripts/fuzz.py --iterations 20000 --seed 1
"""

from __future__ import annotations

import argparse
import faulthandler
import hashlib
import random
import sys
import tempfile
import time
import traceback
from collections import Counter
from pathlib import Path

from flightlens import parsers
from flightlens.engine import analyze
from flightlens.model import Severity

ROOT = Path(__file__).resolve().parents[1]
FMT_HEAD = b"\xa3\x95\x80"


def seeds(tmp: Path) -> list[tuple[str, bytes]]:
    from flightlens.sim.benchmark import Fault, copter
    from flightlens.sim.plane import plane_log

    out = [
        ("copter.bin", copter(tmp / "copter.bin", None, 1).read_bytes()),
        ("copter-vib.bin", copter(tmp / "v.bin", Fault("vibration", 20, 40, 70), 2).read_bytes()),
        ("plane-crash.bin", plane_log(tmp / "p.bin", "crash", trim=-0.2).read_bytes()),
    ]
    out += [(p.name, p.read_bytes()) for p in sorted((ROOT / "testdata/logs").glob("*.ulg"))]
    from flightlens import parsers
    from flightlens.sim.telemetry import telemetry

    tl = telemetry(parsers.load(tmp / "v.bin"), tmp / "v.tlog", loss=0.02, seed=3)
    out.append(("copter-vib.tlog", tl.read_bytes()))
    return out


def mutate(data: bytes, rng: random.Random) -> tuple[bytes, str]:
    b = bytearray(data)
    n = len(b)
    kind = rng.choice(
        ["truncate", "flip", "flip_many", "zero", "dup", "insert", "fmt", "header_only", "swap"]
    )
    if kind == "truncate":
        return bytes(b[: rng.randrange(0, n)]), kind
    if kind == "header_only":
        return bytes(b[: rng.randrange(1, min(n, 600))]), kind
    if kind == "flip":
        for _ in range(rng.randint(1, 8)):
            i = rng.randrange(n)
            b[i] ^= 1 << rng.randrange(8)
        return bytes(b), kind
    if kind == "flip_many":
        for _ in range(rng.randint(10, 400)):
            b[rng.randrange(n)] = rng.randrange(256)
        return bytes(b), kind
    if kind == "zero":
        i = rng.randrange(n)
        b[i : i + rng.randint(1, 4096)] = b"\0" * len(b[i : i + rng.randint(1, 4096)])
        return bytes(b), kind
    if kind == "dup":
        i = rng.randrange(n)
        chunk = b[i : i + rng.randint(1, 8192)]
        j = rng.randrange(n)
        return bytes(b[:j] + chunk + b[j:]), kind
    if kind == "insert":
        j = rng.randrange(n)
        return bytes(b[:j] + rng.randbytes(rng.randint(1, 512)) + b[j:]), kind
    if kind == "swap":
        i, j = sorted(rng.randrange(n) for _ in range(2))
        k = rng.randint(1, 2048)
        return bytes(b[:i] + b[j : j + k] + b[i + k : j] + b[i : i + k] + b[j + k :]), kind
    # fmt: corrupt a FMT definition of a DataFlash log (type, length, format chars, names)
    positions = [i for i in range(n - 89) if b[i : i + 3] == FMT_HEAD]
    if not positions:
        return mutate(data, rng)
    i = rng.choice(positions)
    field = rng.choice(["type", "length", "format", "columns", "name"])
    if field == "type":
        b[i + 3] = rng.randrange(256)
    elif field == "length":
        b[i + 4] = rng.choice([0, 1, 2, 3, 4, 255, rng.randrange(256)])
    elif field == "format":
        for _ in range(rng.randint(1, 4)):
            b[i + 9 + rng.randrange(16)] = ord(rng.choice("bBMhHiIfdqQcCeELnNZa?x\0"))
    elif field == "columns":
        for _ in range(rng.randint(1, 6)):
            b[i + 25 + rng.randrange(64)] = ord(rng.choice("abc,,,\0 Time"))
    else:
        b[i + 5 : i + 9] = rng.randbytes(4)
    return bytes(b), f"fmt-{field}"


def run_one(path: Path) -> tuple[str, str] | None:
    """None when the file is handled correctly, else (problem, detail)."""
    try:
        flight = parsers.load(path)
    except parsers.ParseError:
        return None
    except Exception as exc:
        tb = traceback.extract_tb(exc.__traceback__)[-1]
        where = f"{Path(tb.filename).name}:{tb.lineno}"
        return f"parse {type(exc).__name__} at {where}", "".join(traceback.format_exc())
    report = analyze(flight)
    errors = [r for r in report.results if r.status is Severity.ERROR]
    if errors:
        f = errors[0].findings[0]
        return f"check {errors[0].check_id} error: {f.explanation[:80]}", f.explanation
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--iterations", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--slow", type=float, default=20.0, help="seconds per mutant before a hang")
    ap.add_argument("--out", type=Path, default=ROOT / "fuzz-failures")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    failures: Counter[str] = Counter()
    first: dict[str, Path] = {}
    slowest = 0.0
    with tempfile.TemporaryDirectory() as tmp:
        corpus = seeds(Path(tmp))
        case = Path(tmp) / "case"
        t0 = time.monotonic()
        for i in range(args.iterations):
            name, data = rng.choice(corpus)
            mutant, kind = mutate(data, rng)
            path = case.with_suffix(Path(name).suffix)
            path.write_bytes(mutant)
            # kept if the next call hangs (faulthandler then exits the process)
            args.out.mkdir(exist_ok=True)
            (args.out / f"last-input{path.suffix}").write_bytes(mutant)
            faulthandler.dump_traceback_later(args.slow, exit=True)
            start = time.monotonic()
            problem = run_one(path)
            took = time.monotonic() - start
            faulthandler.cancel_dump_traceback_later()
            slowest = max(slowest, took)
            if problem is not None:
                sig = problem[0]
                failures[sig] += 1
                if sig not in first:
                    args.out.mkdir(exist_ok=True)
                    digest = hashlib.sha256(mutant).hexdigest()[:12]
                    saved = args.out / f"{digest}-{kind}{path.suffix}"
                    saved.write_bytes(mutant)
                    first[sig] = saved
                    print(f"[{i}] NEW {sig}  ({name}, {kind}) -> {saved.name}", flush=True)
            if (i + 1) % 500 == 0:
                rate = (i + 1) / (time.monotonic() - t0)
                print(
                    f"{i + 1} mutants, {rate:.1f}/s, {len(failures)} distinct failures", flush=True
                )

    print(f"\nslowest mutant: {slowest:.2f} s")
    if not failures:
        print(f"no failures in {args.iterations} mutants")
        return 0
    print("failures:")
    for sig, n in failures.most_common():
        print(f"  {n:5d}  {sig}  e.g. {first[sig].name}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
