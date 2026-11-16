#!/usr/bin/env python3
"""Experimental views-free risk heuristic for Kilimall listings.

Kilimall pages expose no view counts, so the Jiji-style views/bait heuristic
can't be ported. This uses price/availability dynamics from price_history instead:

  observations        = number of price observations recorded
  distinct_hashes     = distinct canonical hashes (any change counts as 1 flip)
  availability_flips  = distinct_hashes - 1 (upper bound on availability churn)
  price_vs_median     = latest price / median(observed prices).  <<1 suggests deep deal
                        (or bait); >>1 overpriced vs itself historically.
  discount_persist    = trailing observations priced below 0.8 * median
  risk                = 0..10 heuristic score (flips + discount persistence).
                        NOT a fraud verdict — treat as a review flag only.

Writes table listing_risk(guid, observations, distinct_hashes, availability_flips,
                          price_vs_median, discount_persist, risk, checked_at).
Usage: python scripts/listing_risk.py
"""
import os
import sqlite3
from statistics import median

BASE = os.environ.get("KILIMALL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(BASE, "data", "kilimall.db")
from datetime import datetime, timezone

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")


def main():
    db = sqlite3.connect(DB, timeout=120)
    db.executescript("""
    CREATE TABLE IF NOT EXISTS listing_risk (
      guid TEXT PRIMARY KEY, observations INT, distinct_hashes INT,
      availability_flips INT, price_vs_median REAL, discount_persist INT,
      risk INTEGER, checked_at TEXT
    );
    """)
    series = {}
    for guid, price, h, seen in db.execute(
            "select guid, price, hash, seen_at from price_history order by guid, seen_at"):
        d = series.setdefault(guid, [0, set(), []])
        d[0] += 1
        d[1].add(h)
        if price is not None:
            d[2].append(price)
    out = []
    for guid, (n, hashes, vals) in series.items():
        dh = len(hashes)
        if not vals:
            continue
        med = median(vals) or 1.0
        ratio = vals[-1] / med
        below = 0
        for v in reversed(vals):
            if v < 0.8 * med:
                below += 1
            else:
                break
        flips = dh - 1
        risk = min(10, int(flips / 3) + (4 if below >= 3 else 0) + (2 if ratio < 0.6 else 0))
        out.append((guid, n, dh, flips, round(ratio, 3), below, risk, NOW))
    db.execute("delete from listing_risk")
    db.executemany(
        "insert into listing_risk values(?,?,?,?,?,?,?,?)", out)
    db.commit()
    flagged = sum(1 for o in out if o[6] >= 5)
    print(f"scored {len(out)} listings; flagged(risk>=5) {flagged}; ran_at {NOW}")


if __name__ == "__main__":
    main()
