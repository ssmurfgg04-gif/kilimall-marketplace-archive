#!/usr/bin/env python3
"""Merge WARC part SQLite files (from cc-drain artifacts) into the main DB.

Loads part DBs (table warc_item) into the kilimall archive DB:
  - warc_item(url, capture_ts, digest, sku, title, price, currency,
              availability, brand, category, description, fetched_at, source)
  - price_history rows tagged source='commoncrawl' (one per NEW capture w/ price)

Part DBs are found recursively: `gh run download --pattern` puts each artifact
in its own sub-directory (warc-parts/warc-part-N/*.db).

Idempotent: re-running over the same parts inserts nothing new (warc_item is
keyed on (url, capture_ts); price_history rows are only added for captures that
were actually inserted).

Exit codes: 0 ok, 2 no part DBs found, 3 parts had rows but nothing was merged
(and the DB had none of them already) — so CI fails loudly instead of
publishing an empty merge.

Usage: python scripts/warc_merge.py --part-dir /tmp/warc-parts [--db path]
"""
import argparse
import glob
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE = os.environ.get("KILIMALL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = Path(BASE) / "data" / "kilimall.db"
NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

COLS = ("url", "capture_ts", "digest", "sku", "title", "price", "currency",
        "availability", "brand", "category", "description")

DDL = """
CREATE TABLE IF NOT EXISTS warc_item(
  url TEXT, capture_ts TEXT, digest TEXT, sku TEXT, title TEXT, price REAL,
  currency TEXT, availability TEXT, brand TEXT, category TEXT, description TEXT,
  fetched_at TEXT, source TEXT,
  PRIMARY KEY (url, capture_ts)
);
CREATE TABLE IF NOT EXISTS price_history (
  guid TEXT, seen_at TEXT, price REAL, hash TEXT, source TEXT
);
"""


def find_parts(part_dir):
    return sorted(set(glob.glob(os.path.join(part_dir, "**", "*.db"), recursive=True)))


def part_rows(path):
    part = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        have = {r[1] for r in part.execute("pragma table_info(warc_item)")}
        if not have:
            print(f"  {path}: no warc_item table, skipped", file=sys.stderr)
            return []
        sel = ", ".join(c if c in have else "NULL" for c in COLS)
        return part.execute(f"select {sel} from warc_item").fetchall()
    finally:
        part.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part-dir", default="/tmp/warc-parts")
    ap.add_argument("--db", default=str(DB))
    args = ap.parse_args()

    parts = find_parts(args.part_dir)
    print(f"merging {len(parts)} part dbs into {args.db}")
    if not parts:
        print("no part DBs found", file=sys.stderr)
        return 2

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db, timeout=180)
    db.executescript(DDL)

    seen_total = n_item = n_hist = bad = 0
    for p in parts:
        try:
            rows = part_rows(p)
        except sqlite3.Error as e:
            print(f"  bad part db {p}: {e}", file=sys.stderr)
            bad += 1
            continue
        seen_total += len(rows)
        added = 0
        with db:
            for r in rows:
                rec = dict(zip(COLS, r))
                if not rec["url"] or not rec["capture_ts"]:
                    continue
                cur = db.execute(
                    "insert or ignore into warc_item values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*r, NOW, "commoncrawl"))
                if cur.rowcount != 1:
                    continue  # already merged earlier
                added += 1
                if rec["price"] is not None:
                    db.execute(
                        "insert into price_history(guid, seen_at, price, hash, source) values(?,?,?,?,?)",
                        (rec["sku"] or rec["url"], rec["capture_ts"], rec["price"],
                         f"cc:{rec['digest'] or rec['capture_ts']}", "commoncrawl"))
                    n_hist += 1
        n_item += added
        print(f"  {os.path.relpath(p, args.part_dir)}: {len(rows)} captures, +{added} new")

    total = db.execute("select count(*) from warc_item").fetchone()[0]
    db.close()
    print(f"done: parts={len(parts)} bad={bad} part_rows={seen_total} "
          f"new_warc_item={n_item} new_price_history={n_hist} warc_item_total={total}")
    if seen_total and total == 0:
        print("parts had rows but warc_item is empty after merge", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
