#!/usr/bin/env python3
"""Partition the CC manifest into 20 worker chunks by stable hash.

Reads cc_manifest/manifest.parquet (run cc_layer0.py first).
Each chunk TSV: warc_filename, offset, length, url, capture_ts, mime, key
Output: data/warc_shards/kilimall_shard_NN.tsv (committed, like the Jiji repo).
"""
import hashlib
import os
from pathlib import Path

import duckdb

BASE = os.environ.get("KILIMALL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = Path(BASE) / "data" / "warc_shards"
OUT.mkdir(parents=True, exist_ok=True)
N = 20


def main():
    rows = duckdb.sql(f"select warc_filename, offset, length, url, capture_ts, mime from '{BASE}/cc_manifest/manifest.parquet'").fetchall()
    handles = [open(OUT / f"kilimall_shard_{i:02d}.tsv", "w", encoding="utf-8") for i in range(N)]
    seen = set()
    kept = junk = dup = 0
    for fn, off, ln, url, ts, mime in rows:
        key = hashlib.sha1(f"{fn}:{off}:{ln}".encode()).hexdigest()
        d = int(key, 16) % N
        if (url, ts) in seen:
            dup += 1
            continue
        seen.add((url, ts))
        handles[d].write(f"{fn}\t{off}\t{ln}\t{url}\t{ts}\t{mime}\t{key}\n")
        kept += 1
    for h in handles:
        h.close()
    print(f"kept {kept} / dup {dup}; shards -> {OUT}")


if __name__ == "__main__":
    main()
