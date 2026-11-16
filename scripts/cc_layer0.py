#!/usr/bin/env python3
"""Manifest builder (offline): cc-index shards -> cc_manifest/manifest.parquet.

One row per capture with the exact WARC byte range. Offline-safe.
Usage: python scripts/cc_layer0.py [--min-year 2024]
Inputs: cc_index/*.jsonl (gunzipped shards, as downloaded from the cc-index release)
"""
import argparse
import json
import os
from pathlib import Path

import duckdb

BASE = os.environ.get("KILIMALL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
IDX = Path(BASE) / "cc_index"
OUT = Path(BASE) / "cc_manifest"
OUT.mkdir(parents=True, exist_ok=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-year", type=int, default=2024)
    args = ap.parse_args()
    rows = []
    for path in sorted(IDX.glob("*.jsonl")):
        n = 0
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                ts = str(r.get("timestamp", ""))
                if args.min_year and ts[:4].isdigit() and int(ts[:4]) < args.min_year:
                    continue
                url = str(r.get("url", ""))
                if "/listing/" not in url:
                    continue
                rows.append({
                    "url": url,
                    "capture_ts": ts,
                    "crawl": r.get("_coll", ""),
                    "warc_filename": r.get("filename"),
                    "offset": int(r.get("offset") or 0),
                    "length": int(r.get("length") or 0),
                    "status": str(r.get("status", "")),
                    "mime": r.get("mime", ""),
                    "digest": r.get("digest", ""),
                })
                n += 1
        print(f"{path.name}: {n} captures")
    tmp = OUT / "manifest.jsonl"
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            import json as _json
            f.write(_json.dumps(r, separators=(",", ":")) + "\n")
    con = duckdb.connect()
    out = OUT / "manifest.parquet"
    con.sql(f"copy (select * from read_ndjson_auto('{tmp}')) to '{out}' (format parquet)")
    print(f"manifest: {len(rows)} captures -> {out}")


if __name__ == "__main__":
    main()
