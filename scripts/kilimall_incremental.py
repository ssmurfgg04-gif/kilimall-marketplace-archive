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
    ap.add_argument("--dmin", type=float, default=3.0)
    ap.add_argument("--dmax", type=float, default=8.0)
    ap.add_argument("--snapshot", action="store_true", default=True)
    args = ap.parse_args()

    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(args.db, timeout=120)
    db.executescript(DDL)
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
        with open(args.index, encoding="utf-8") as f:
            next(f)
            for line in f:
                sku, url, lastmod, _shard = line.rstrip("\n").split("\t")
                if not sku:
                    continue
                if sku in seen and lastmod <= db_max:
                    continue
                items.append((sku, url, lastmod))
                if len(items) >= args.max_items:
                    break
    else:
        with open(args.index, encoding="utf-8") as f:
            next(f)
            items = [tuple(l.rstrip("\n").split("\t")[:3]) for l in f if l.strip()]
        items = items[: args.max_items]

    print(f"collecting {len(items)} listings (mode={args.mode})")
    new = changed = gone = 0
    for sku, url, _lastmod in items:
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
                "insert into listings(guid,title,price,currency,url,brand,availability,item_condition,first_seen,last_seen,last_hash,description,source)"
                " values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r_["guid"], r_["title"], r_["price"], r_["currency"], r_["url"] or url, r_["brand"], r_["availability"],
                 r_["item_condition"], t, t, h, r_["description"], "live"))
            new += 1
        else:
            db.execute("update listings set last_seen=?, last_hash=?, died_at=NULL,"
                       " title=?, price=?, availability=?, url=?, description=?"
                       " where guid=?",
                       (t, h, r_["title"], r_["price"], r_["availability"], r_["url"] or url, r_["description"], r_["guid"]))
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
