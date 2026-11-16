#!/usr/bin/env python3
"""Data-quality maintenance: died_at marking (stale mean divested), trims.

Safe: additive column + whitespace-only updates. Idempotent.
Usage: python scripts/data_quality.py [--days 14]
"""
import argparse
import os
import sqlite3
from datetime import datetime, timezone

BASE = os.environ.get("KILIMALL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(BASE, "data", "kilimall.db")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    args = ap.parse_args()
    db = sqlite3.connect(DB, timeout=120)
    cols = [c[1] for c in db.execute("PRAGMA table_info(listings)").fetchall()]
    if "died_at" not in cols:
        db.execute("ALTER TABLE listings ADD COLUMN died_at TEXT")
        print("migrated: +died_at")
    cur = db.execute(
        "UPDATE listings SET died_at=COALESCE(died_at, last_seen) "
        "WHERE last_seen < datetime('now', ?) AND (died_at IS NULL OR died_at='')",
        (f"-{args.days} days",))
    print(f"marked died: {cur.rowcount}")
    db.commit()
    db.close()
    print("ran_at:", datetime.now(timezone.utc).isoformat(timespec="seconds"))


if __name__ == "__main__":
    main()
