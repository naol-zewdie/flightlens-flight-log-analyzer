"""Download public PX4 logs from PX4 Flight Review for testing.

Logs on Flight Review are uploaded publicly by their pilots; use them for
testing only and respect the site's terms. Downloads go to testdata/real/
(gitignored) and are never committed.

Usage:
    uv run python scripts/fetch_px4_logs.py --count 20
    uv run python scripts/fetch_px4_logs.py --id 2d145d43-e9e8-4d6d-ab35-8a28afac1a48
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://review.px4.io"
USER_AGENT = "flightlens-test-fetcher/0.1"
LOG_ID = re.compile(r"log=([0-9a-f-]{36})")


def _get(url: str, timeout: float = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return bytes(resp.read())


def list_logs(count: int, search: str) -> list[dict[str, str]]:
    """Return the most recent public logs: id, airframe, hardware, firmware, duration."""
    params = {
        "draw": "1",
        "start": "0",
        "length": str(count),
        "search[value]": search,
        "order[0][column]": "1",
        "order[0][dir]": "desc",
    }
    url = f"{BASE}/browse_data_retrieval?{urllib.parse.urlencode(params)}"
    rows = json.loads(_get(url))["data"]
    logs = []
    for row in rows:
        m = LOG_ID.search(row[1])
        if m:
            logs.append(
                {
                    "id": m.group(1),
                    "type": str(row[3]),
                    "airframe": str(row[4]),
                    "hardware": str(row[5]),
                    "firmware": str(row[6]),
                    "duration": str(row[7]),
                }
            )
    return logs


def download(log_id: str, out_dir: Path, max_mb: int) -> Path | None:
    dest = out_dir / f"{log_id}.ulg"
    if dest.exists():
        return dest
    req = urllib.request.Request(
        f"{BASE}/download?log={log_id}", headers={"User-Agent": USER_AGENT}
    )
    limit = max_mb * 1024 * 1024
    tmp = dest.with_suffix(".part")
    size = 0
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
        while chunk := resp.read(1 << 20):
            size += len(chunk)
            if size > limit:
                fh.close()
                tmp.unlink()
                return None
            fh.write(chunk)
    tmp.replace(dest)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--count", type=int, default=20, help="how many recent logs to fetch")
    ap.add_argument("--search", default="", help="filter, e.g. 'Hexarotor' or 'v1.15'")
    ap.add_argument("--id", action="append", default=[], help="specific log id (repeatable)")
    ap.add_argument("--out", type=Path, default=Path("testdata/real"))
    ap.add_argument("--max-mb", type=int, default=150, help="skip logs larger than this")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    targets = [{"id": i} for i in args.id] or list_logs(args.count, args.search)
    index_path = args.out / "index.json"
    index = json.loads(index_path.read_text()) if index_path.exists() else {}

    for meta in targets:
        try:
            path = download(meta["id"], args.out, args.max_mb)
        except Exception as exc:
            print(f"  fail  {meta['id']}: {exc}", file=sys.stderr)
            continue
        if path is None:
            print(f"  skip  {meta['id']} (over {args.max_mb} MB)")
            continue
        index[meta["id"]] = {**index.get(meta["id"], {}), **meta}
        desc = " · ".join(v for k, v in meta.items() if k != "id")
        print(f"  ok    {path.name}  {desc}")

    index_path.write_text(json.dumps(index, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
