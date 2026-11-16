#!/usr/bin/env python3
"""Merge WARC part SQLite files (from cc-drain artifacts) into the main DB.

Loads part dbdb's (table warc_item) into the kilimall archive DB:
  - warc_item(url, capture_ts, digest, sku, title, price, currency,
              availability, brand, category, description, fetched_at, source)
  - price_history rows tagged source='commoncrawl' (one per capture w/ price)

Usage: python scripts/warc_merge.py --part glob  OR  --part-dir /path/to/parts
"""
import argparse
import glob
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE = os.environ.get("KILIMALL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = Path(BASE) / "data" / "kilimall.db"
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

DDL = """
CREATE TABLE IF NOT EXISTS warc_item(
  url TEXT, capture_ts TEXT, digest TEXT, sku TEXT, title TEXT, price REAL,
  currency TEXT, availability TEXT, brand TEXT, category TEXT, description TEXT,
  fetched_at TEXT, source TEXT,
  PRIMARY KEY (url, capture_ts)
);
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part-dir", default="/tmp/warc-parts")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()
    parts = sorted(glob.glob(os.path.join(args.part_dir, "*.db")))
    print(f"merging {len(parts)} part dbs into {args.db}")
    db = sqlite3.connect(args.db, timeout=180)
    db.executescript(DDL)
    n_item = n_hist = 0
    for p in parts:
        part = sqlite3.connect(p)
        rows = list(part.execute(
            "select url, capture_ts, digest, sku, title, price, currency, availability, brand, category, description from warc_item"))
        for (url, ts, digest, sku, title, price, currency, availability, brand, category, description) in rows:
            db.execute(
                "insert or ignore into warc_item values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (url, ts, digest, sku, title, price, currency, availability, category, description, NOW, "commoncrawl"))
            n_item += 1
            if price is not None:
                db.execute(
                    "insert into price_history(guid, seen_at, price, hash, source) values(?,?,?,?,?)",
                    (sku or url, ts, price, f"cc:{digest or ts}", "commoncrawl"))
                n_hist += 1
        part.close()
        db.commit()
        print(f"  {os.path.basename(p)}: +{len(rows)} captures")
    db.close()
    print(f"done: warc_item rows inserted {n_item}, price_history rows {n_hist}")


if __name__ == "__main__":
    main()
