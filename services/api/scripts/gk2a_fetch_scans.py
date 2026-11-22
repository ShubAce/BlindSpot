"""Download GK-2A scans for offline evaluation, without touching any database.

Range-reads sw038 + ir112 of each slot for the job's window (sources/gk2a.py, exactly as the job
does) from the keyless NOAA bucket and saves them as `YYYYmmdd_HHMM.npz` in the layout of the
job's disk cache, so gk2a_eval_lib.load_npz reads both. blindspot.http.fetch is replaced in memory
by a plain HTTP client: no source_fetch rows are written anywhere.

    uv run python scripts/gk2a_fetch_scans.py OUTDIR 202611171110 202611171120 ...
    uv run python scripts/gk2a_fetch_scans.py OUTDIR --from-file slots.txt   (one YYYYmmddHHMM a line)
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np
import orjson

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # services/api (not installed)

from blindspot.sources import gk2a  # noqa: E402

AOI = (73.8, 27.6, 77.7, 32.6)  # Settings.aoi_bbox, the job's window
CHANNELS = ("sw038", "ir112")
CLIENT = httpx.Client(
    timeout=httpx.Timeout(60.0, connect=15.0), headers={"User-Agent": "gk2a-offline-evaluation"}
)


@dataclass
class _Result:
    data: object
    status_code: int
    headers: dict = field(default_factory=dict)
    fetch_id: int | None = None


def _fetch(source_key, url, *, params=None, headers=None, expect="json", **_):  # noqa: ANN001
    for attempt in range(4):
        try:
            r = CLIENT.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.text if expect == "text" else r.content
            return _Result(data, r.status_code, {k.lower(): v for k, v in r.headers.items()})
        except httpx.HTTPError:
            if attempt == 3:
                raise
    raise RuntimeError("unreachable")


gk2a.fetch = _fetch  # type: ignore[assignment]
gk2a.set_fetch_records = lambda *a, **k: None  # type: ignore[assignment]


def save(scan: gk2a.Scan, path: Path) -> None:
    w = scan.window
    arrays: dict[str, np.ndarray] = {}
    meta = {"slot": scan.slot.isoformat(), "window": [w.row0, w.row1, w.col0, w.col1], "channels": {}}
    for ch, cw in scan.channels.items():
        arrays[f"{ch}_counts"], arrays[f"{ch}_quality"] = cw.counts, cw.quality
        meta["channels"][ch] = {
            "key": cw.file.key, "size": cw.file.size, "last_modified": None,
            "attrs": {k: cw.attrs[k] for k in gk2a._CACHED_ATTRS if k in cw.attrs},
            "fetch_ids": [], "bytes_fetched": cw.bytes_fetched, "requests": cw.requests,
        }  # fmt: skip
    arrays["meta"] = np.frombuffer(orjson.dumps(meta), dtype=np.uint8)
    tmp = path.with_name(path.stem + ".partial.npz")
    np.savez_compressed(tmp, **arrays)
    tmp.replace(path)


def one(out: Path, stamp: str) -> str:
    slot = datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=UTC)
    path = out / f"{slot:%Y%m%d_%H%M}.npz"
    if path.exists():
        return f"{stamp} already here"
    try:
        files = gk2a.complete_scans(gk2a.list_hour(slot), CHANNELS)
        if slot not in files:
            return f"{stamp} not in the bucket"
        scan = gk2a.load_scan(files[slot], AOI, CHANNELS, use_cache=False)
    except (ValueError, OSError, httpx.HTTPError) as exc:  # e.g. a file stored without chunks
        return f"{stamp} failed: {type(exc).__name__}: {exc}"
    save(scan, path)
    return f"{stamp} ok {scan.bytes_fetched / 1e6:.1f} MB"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("outdir", type=Path)
    ap.add_argument("slots", nargs="*", help="YYYYmmddHHMM (UTC)")
    ap.add_argument("--from-file", type=Path)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    stamps = list(args.slots)
    if args.from_file:
        stamps += [s.strip() for s in args.from_file.read_text().split() if s.strip()]
    args.outdir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(args.workers) as pool:
        for msg in pool.map(lambda s: one(args.outdir, s), stamps):
            print(msg, flush=True)


if __name__ == "__main__":
    main()
