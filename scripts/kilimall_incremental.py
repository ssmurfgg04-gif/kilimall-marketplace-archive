#!/usr/bin/env python3
"""Layer 1 — polite live incremental collector for Kilimall listing pages.

robots.txt disallows /api/* — this collector only fetches public /listing/*
pages (allowed) and parses the server-rendered JSON-LD <Product> block.
One worker, random 3–8s delays, retries with backoff. Stores SQLite
(data/kilimall.db) + daily Parquet snapshot (data/snapshot-YYYY-MM-DD.parquet).

Usage:
  python scripts/kilimall_incremental.py --mode delta --max-items 60
  python scripts/kilimall_incremental.py --skus-file data/seed-skus.txt --dmin 4 --dmax 9
"""
import argparse
import json
import os
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import blake3
import duckdb

BASE = os.environ.get("KILIMALL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UA = {"User-Agent": "kilimall-marketplace-archive/0.1 (+https://github.com/ssmurfgg04-gif/kilimall-marketplace-archive)"}
LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)

DDL = """
CREATE TABLE IF NOT EXISTS listings (
  guid TEXT PRIMARY KEY, title TEXT, price REAL, currency TEXT, url TEXT,
  brand TEXT, availability TEXT, item_condition TEXT, category TEXT,
  first_seen TEXT, last_seen TEXT, last_hash TEXT, description TEXT,
  died_at TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS price_history (
  guid TEXT, seen_at TEXT, price REAL, hash TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, mode TEXT, new INTEGER, changed INTEGER
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(url, tries=2):
    import urllib.request, urllib.error
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace"), r.status
        except urllib.error.HTTPError as e:
            if e.code in (400, 403, 404, 410):
                return None, e.code
            last = e
            time.sleep(2 * (a + 1))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (a + 1))
    raise RuntimeError(f"fetch failed {url}: {last}")


def parse_product(html):
    """Return the schema.org Product dict, or None."""
    for m in LD_RE.finditer(html):
        try:
            obj = json.loads(m.group(1))
        except Exception:
            continue
        candidates = []
        if isinstance(obj, list):
            candidates = obj
        else:
            candidates = [obj]
            for k in ("mainEntity",):
                if isinstance(obj, dict) and isinstance(obj.get(k), dict):
                    candidates.append(obj[k])
        for c in candidates:
            if isinstance(c, dict) and c.get("@type") == "Product":
                bc = None
                if isinstance(obj, dict) and isinstance(obj.get("breadcrumb"), dict):
                    items_bc = obj["breadcrumb"].get("itemListElement") or []
                    names = []
                    for it in items_bc:
                        inner = it.get("item", it) if isinstance(it, dict) else {}
                        name = (inner.get("name") if isinstance(inner, dict) else None) or it.get("name")
                        if name and name.lower() != "home":
                            names.append(name)
                    if names:
                        bc = " > ".join(names)
                c = dict(c)
                if bc:
                    c["__breadcrumb__"] = bc
                return c
    return None


def record(o: dict):
    offers = o.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = offers.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    brand = o.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    return {
        "guid": str(o.get("sku") or "").strip(),
        "title": (o.get("name") or "").strip()[:500],
        "price": price,
        "currency": offers.get("priceCurrency") or "KES",
        "url": offers.get("url") or "",
        "brand": (brand or "").strip()[:200],
        "availability": offers.get("availability") or "",
        "item_condition": offers.get("itemCondition") or "",
        "description": (o.get("description") or "").strip()[:2000],
        "category": (o.get("__breadcrumb__") or "").strip()[:300],
    }


def canon_hash(r):
    s = "|".join([r["guid"], r["title"], str(r["price"]), r["availability"], r["currency"]])
    return blake3.blake3(s.encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.path.join(BASE, "data", "kilimall.db"))
    ap.add_argument("--index", default=os.path.join(BASE, "data", "url_index.tsv"))
    ap.add_argument("--skus-file", help="optional file of SKUs/URLs to collect explicitly")
    ap.add_argument("--mode", choices=["delta", "all"], default="delta")
    ap.add_argument("--max-items", type=int, default=60)
    ap.add_argument("--refresh-oldest", type=int, default=15,
                    help="additionally re-collect N stalest-seen listings so their prices refresh")
    ap.add_argument("--offset", type=int, default=0,
                    help="skip first N index candidates (deep backfill paging)")
    ap.add_argument("--dmin", type=float, default=3.0)
    ap.add_argument("--dmax", type=float, default=8.0)
    ap.add_argument("--snapshot", action="store_true", default=True)
    ap.add_argument("--no-snapshot", dest="snapshot", action="store_false")
    ap.add_argument("--shard", type=int, default=0, help="this leg's index (live legs)")
    ap.add_argument("--shards", type=int, default=1, help="number of parallel live legs")
    ap.add_argument("--skip-seen-hours", type=float, default=0,
                    help="mode=all: skip SKUs collected within the last N hours")
    ap.add_argument("--deadline-min", type=float, default=0,
                    help="stop cleanly after N minutes (0 = no limit)")
    ap.add_argument("--commit-every", type=int, default=25)
    args = ap.parse_args()
    t_end = time.monotonic() + args.deadline_min * 60 if args.deadline_min else None

    def mine(sku):
        if args.shards <= 1:
            return True
        return int(blake3.blake3(str(sku).encode()).hexdigest()[:8], 16) % args.shards == args.shard

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db, timeout=120)
    db.executescript(DDL)
    cols = [c[1] for c in db.execute("PRAGMA table_info(listings)").fetchall()]
    if "category" not in cols:
        db.execute("ALTER TABLE listings ADD COLUMN category TEXT")
    started = now()

    db_max = db.execute("select max(last_seen) from listings").fetchone()[0] or ""
    if args.skus_file:
        urls = [l.strip() for l in Path(args.skus_file).read_text().splitlines() if l.strip()]
        items = []
        for u in urls:
            if not u.startswith("http"):
                u = f"https://www.kilimall.co.ke/listing/{u.split('-')[0]}"
            items.append((u.split("/listing/")[-1].split("-")[0], u, ""))
    elif args.mode == "delta":
        rows = db.execute("select guid from listings").fetchall()
        seen = {g for (g,) in rows}
        items = []
        skipped = 0
        with open(args.index, encoding="utf-8") as f:
            next(f)
            for line in f:
                sku, url, lastmod, _shard = line.rstrip("\n").split("\t")
                if not sku:
                    continue
                if skipped < args.offset:
                    skipped += 1
                    continue
                if not mine(sku):
                    continue
                if sku in seen and lastmod <= db_max:
                    continue
                items.append((sku, url, lastmod))
                if len(items) >= args.max_items:
                    break
        # improvement: always give the stalest known listings a refresh,
        # otherwise their prices silently freeze forever.
        if args.refresh_oldest:
            already = {i[0] for i in items}
            oldest = db.execute(
                "select guid, url from listings order by last_seen asc limit ?",
                (args.refresh_oldest,)).fetchall()
            for g, u in oldest:
                if g not in already:
                    items.append((g, u, ""))
    else:  # all / backfill sweep (page with --offset); live legs use --shard/--shards
        last = dict(db.execute("select guid, last_seen from listings").fetchall())
        cutoff = ""
        if args.skip_seen_hours:
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=args.skip_seen_hours)).isoformat(timespec="seconds")
        unseen, stale = [], []
        skipped = 0
        with open(args.index, encoding="utf-8") as f:
            next(f)
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if not parts[0] or not mine(parts[0]):
                    continue
                if skipped < args.offset:
                    skipped += 1
                    continue
                seen_at = last.get(parts[0])
                if seen_at is None:
                    unseen.append(tuple(parts[:3]))
                elif not cutoff or seen_at < cutoff:
                    stale.append((seen_at,) + tuple(parts[:3]))
        stale.sort()  # stalest first
        items = (unseen + [r[1:] for r in stale])[: args.max_items]
        print(f"leg {args.shard}/{args.shards}: unseen={len(unseen)} stale={len(stale)}")

    print(f"collecting {len(items)} listings (mode={args.mode})")
    new = changed = gone = done_n = 0
    for sku, url, _lastmod in items:
        if t_end and time.monotonic() > t_end:
            print(f"deadline reached after {done_n} items — stopping cleanly")
            break
        done_n += 1
        if done_n % args.commit_every == 0:
            db.commit()
            print(f"  progress {done_n}/{len(items)} new={new} changed={changed} gone={gone}", flush=True)
        try:
            html, status = fetch(url)
        except RuntimeError as e:
            print(f"  [error] {e}")
            time.sleep(random.uniform(args.dmin, args.dmax))
            continue
        if html is None:
            if status in (404, 410):
                db.execute("update listings set died_at=coalesce(died_at, ?) where guid=?", (now(), sku))
                gone += 1
                print(f"  [gone {status}] {sku}")
            else:
                print(f"  [skip {status}] {sku}")
            time.sleep(random.uniform(args.dmin, args.dmax))
            continue
        p = parse_product(html)
        if not p:
            print(f"  [no JSON-LD] {sku}")
            time.sleep(random.uniform(args.dmin, args.dmax))
            continue
        r_ = record(p)
        h = canon_hash(r_)
        t = now()
        prev = db.execute("select last_hash from listings where guid=?", (r_["guid"],)).fetchone()
        if prev is None:
            db.execute(
                "insert into listings(guid,title,price,currency,url,brand,availability,item_condition,category,first_seen,last_seen,last_hash,description,source)"
                " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r_["guid"], r_["title"], r_["price"], r_["currency"], r_["url"] or url, r_["brand"], r_["availability"],
                 r_["item_condition"], r_["category"], t, t, h, r_["description"], "live"))
            new += 1
        else:
            db.execute("update listings set last_seen=?, last_hash=?, died_at=NULL,"
                       " title=?, price=?, availability=?, url=?, description=?, category=?"
                       " where guid=?",
                       (t, h, r_["title"], r_["price"], r_["availability"], r_["url"] or url, r_["description"], r_["category"], r_["guid"]))
            if prev[0] != h:
                changed += 1
        db.execute("insert into price_history(guid,seen_at,price,hash,source) values(?,?,?,?,?)",
                   (r_["guid"], t, r_["price"], h, "live"))
        time.sleep(random.uniform(args.dmin, args.dmax))

    db.execute("insert into runs(started_at,mode,new,changed) values(?,?,?,?)", (started, args.mode, new, changed))
    db.commit()

    if args.snapshot:
        path = os.path.join(BASE, "data", f"snapshot-{started[:10]}.parquet")
        duckdb.sql(f"copy (select * from sqlite_scan('{args.db}', 'listings')) to '{path}' (format parquet)")
        n = duckdb.sql(f"select count(*) from sqlite_scan('{args.db}', 'listings')").fetchall()[0][0]
        print(f"snapshot {path} rows={n}")

    print(f"done: new={new} changed={changed} gone={gone}")


if __name__ == "__main__":
    main()
