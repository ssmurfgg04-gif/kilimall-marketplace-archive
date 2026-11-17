#!/usr/bin/env python3
"""Merge live-leg part DBs into the main archive DB.

Each live leg starts from a copy of the release DB, writes `leg_meta(started_at)`,
then collects with kilimall_incremental.py. Only what the leg touched is merged:
  - listings with last_seen >= started_at  -> upsert (newer last_seen wins;
    first_seen is kept from the main DB)
  - listings with died_at   >= started_at  -> mark died in main
  - price_history rows source='live' and seen_at >= started_at -> append
Idempotent: price_history rows already present (guid, seen_at, source) are skipped.

Usage: python scripts/live_merge.py --part-dir /tmp/live-parts [--db data/kilimall.db]
"""
import argparse
import glob
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kilimall_incremental import DDL  # noqa: E402

BASE = os.environ.get("KILIMALL_ROOT", os.path.dirname(HERE))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--part-dir", default="/tmp/live-parts")
    ap.add_argument("--db", default=os.path.join(BASE, "data", "kilimall.db"))
    args = ap.parse_args()
    parts = sorted(glob.glob(os.path.join(args.part_dir, "**", "*.db"), recursive=True))
    print(f"live merge: {len(parts)} part dbs into {args.db}")
    db = sqlite3.connect(args.db, timeout=180)
    db.executescript(DDL)
    for col in ("category", "died_at", "source"):
        if col not in {r[1] for r in db.execute("pragma table_info(listings)")}:
            db.execute(f"alter table listings add column {col} TEXT")
    db.execute("create index if not exists idx_ph_guid_seen on price_history(guid, seen_at)")
    main_cols = [r[1] for r in db.execute("pragma table_info(listings)")]

    tot_new = tot_upd = tot_died = tot_ph = 0
    for p in parts:
        db.execute("attach database ? as p", (p,))
        try:
            try:
                started = db.execute("select min(started_at) from p.leg_meta").fetchone()[0]
            except sqlite3.Error:
                print(f"  {p}: no leg_meta, skipped")
                continue
            pcols = {r[1] for r in db.execute("pragma p.table_info(listings)")}
            cols = [c for c in main_cols if c in pcols]
            sel = ", ".join(f"l.{c}" for c in cols)
            rows = db.execute(f"select {sel} from p.listings l where l.last_seen >= ?", (started,)).fetchall()
            new = upd = 0
            with db:
                for r in rows:
                    rec = dict(zip(cols, r))
                    cur = db.execute("select last_seen from main.listings where guid=?", (rec["guid"],)).fetchone()
                    if cur is None:
                        db.execute(f"insert into main.listings({', '.join(cols)}) values({', '.join('?' * len(cols))})", r)
                        new += 1
                    elif (cur[0] or "") < rec["last_seen"]:
                        upd_cols = [c for c in cols if c not in ("guid", "first_seen")]
                        db.execute(f"update main.listings set {', '.join(c + '=?' for c in upd_cols)} where guid=?",
                                   [rec[c] for c in upd_cols] + [rec["guid"]])
                        upd += 1
                died = db.execute(
                    "update main.listings set died_at = coalesce(main.listings.died_at, d.died_at) "
                    "from (select guid, died_at from p.listings where died_at >= ?) d "
                    "where main.listings.guid = d.guid and main.listings.died_at is null", (started,)).rowcount
                ph = db.execute(
                    "insert into main.price_history(guid, seen_at, price, hash, source) "
                    "select h.guid, h.seen_at, h.price, h.hash, h.source from p.price_history h "
                    "where h.source = 'live' and h.seen_at >= ? and not exists ("
                    "  select 1 from main.price_history m where m.guid = h.guid and m.seen_at = h.seen_at and m.source = h.source)",
                    (started,)).rowcount
            print(f"  {os.path.relpath(p, args.part_dir)}: since {started} new={new} updated={upd} died={died} price_rows={ph}")
            tot_new += new; tot_upd += upd; tot_died += max(died, 0); tot_ph += max(ph, 0)
        finally:
            db.execute("detach database p")
    db.close()
    print(f"live merge done: new={tot_new} updated={tot_upd} died={tot_died} price_rows={tot_ph}")


if __name__ == "__main__":
    main()
