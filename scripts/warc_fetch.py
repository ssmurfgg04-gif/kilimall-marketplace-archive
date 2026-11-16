#!/usr/bin/env python3
"""Fetch Common Crawl WARC bodies for kilimall /listing/* captures and parse them.

Byte-range requests only to data.commoncrawl.org (never kilimall.co.ke).
Body -> JSON-LD (same ItemPage/Product extractor as the live collector) ->
part SQLite /tmp/warc_part_<shard>.db (table warc_item).

Usage: python scripts/warc_fetch.py --shard-file data/warc_shards/kilimall_shard_00.tsv \
        --out-db /tmp/warc_part_00.db --workers 4 --delay 0.5 [--max 6000] [--batch 250]
"""
import argparse
import gzip
import io
import os
import random
import sqlite3
import sys
import threading
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kilimall_incremental import parse_product, record  # noqa: E402

import re as _re

RE_OG_TITLE = _re.compile(r'<meta property="og:title" content="([^"]+)"', _re.S)
RE_OG_AMOUNT = _re.compile(r'og:product:price:amount" content="([\d.]+)"')
RE_OG_CURRENCY = _re.compile(r'og:product:price:currency" content="([A-Za-z]{3})"')
RE_OG_DESC = _re.compile(r'<meta (?:name|property)="og:description" content="([^"]+)"', _re.S)
RE_SLUG_ID = _re.compile(r'/listing/(\d+)-')


def parse_legacy(html, url=""):
    """Pre-2025 storefront: client-rendered; product data lives in OpenGraph meta."""
    title = (RE_OG_TITLE.search(html) or [None, ""])[1]
    amount = (RE_OG_AMOUNT.search(html) or [None, ""])[1]
    currency = (RE_OG_CURRENCY.search(html) or [None, "KES"])[1]
    desc_m = RE_OG_DESC.search(html)
    try:
        price = float(amount) if amount else None
    except (TypeError, ValueError):
        price = None
    m = RE_SLUG_ID.search(url)
    guid = m.group(1) if m else ""
    return {
        "guid": guid,
        "title": title.strip()[:500],
        "price": price,
        "currency": currency or "KES",
        "url": url or "",
        "brand": "",
        "availability": "unknown",
        "item_condition": "",
        "description": (desc_m.group(1).strip()[:2000] if desc_m else ""),
        "category": "",  # breadcrumb only exists in current-era ld+json
    }

CC = "https://data.commoncrawl.org/"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) kilimall-archive-miner/0.2 (+contact: research)"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS warc_item(
  url TEXT, capture_ts TEXT, digest TEXT, sku TEXT, title TEXT, price REAL,
  currency TEXT, availability TEXT, brand TEXT, category TEXT, description TEXT,
  PRIMARY KEY (url, capture_ts)
);
CREATE TABLE IF NOT EXISTS done(url TEXT, capture_ts TEXT, PRIMARY KEY(url, capture_ts));
"""
LOCK = threading.Lock()


def fetch_range(fn, off, ln, sess):
    off, ln = int(off), int(ln)
    try:
        r = sess.get(CC + fn, headers={"Range": f"bytes={off}-{off + ln - 1}"},
                     timeout=60, stream=False)
    except requests.RequestException:
        return "fail", None
    if r.status_code == 429 or r.status_code in (502, 503, 504):
        return "retry", None
    if r.status_code not in (200, 206):
        return "fail", None
    raw = r.content
    try:
        html = gzip.GzipFile(fileobj=io.BytesIO(raw)).read().decode("utf-8", "replace")
    except Exception:
        # CC WARC records sometimes arrive zlib-compressed or as plain gzip;
        # try raw deflate as fallback.
        try:
            import zlib
            html = zlib.decompress(raw).decode("utf-8", "replace")
        except Exception:
            return "fail", None
    return "ok", html


def worker(sh, rows, db_path, delay):
    sess = requests.Session()
    for (fn, off, ln, url, ts, mime, key) in rows:
        status = None
        for attempt in range(3):
            status, html = fetch_range(fn, off, ln, sess)
            if status == "ok":
                break
            time.sleep((attempt + 1) * (1 + random.random()))
        if status != "ok":
            print(f"  [skip {status}] {url}", flush=True)
            time.sleep(delay)
            continue
        p = parse_product(html)
        if p:
            r_ = record(p)
        else:
            r_ = parse_legacy(html, url)
            if not r_["title"] and r_["price"] is None:
                print(f"  [no product data (ld+json or OG)] {url}", flush=True)
                time.sleep(delay)
                continue
        with LOCK:
            db = sqlite3.connect(db_path)
            db.execute(
                "insert or ignore into warc_item values(?,?,?,?,?,?,?,?,?,?,?)",
                (url, ts, key, r_["guid"], r_["title"], r_["price"], r_["currency"],
                 r_["availability"], r_["brand"], r_["category"], r_["description"]))
            db.execute("insert or ignore into done values(?,?)", (url, ts))
            db.commit()
            db.close()
        time.sleep(delay)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard-file", required=True)
    ap.add_argument("--out-db", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--batch", type=int, default=250)
    ap.add_argument("--max", type=int, default=6000)
    args = ap.parse_args()

    with sqlite3.connect(args.out_db) as db:
        db.executescript(SCHEMA)

    with open(args.shard_file, encoding="utf-8") as f:
        rows = [l.rstrip("\n").split("\t") for l in f if l.strip()]
    rows = [r for r in rows if len(r) >= 7]

    # resume: skip captures already banked in this part db
    with sqlite3.connect(args.out_db) as db:
        done = {(u, t) for (u, t) in db.execute("select url, capture_ts from done")}
    rows = [r for r in rows if (r[3], r[4]) not in done][: args.max]
    print(f"shard {os.path.basename(args.shard_file)}: {len(rows)} to fetch "
          f"(done {len(done)})", flush=True)

    chunks = [rows[i:i + args.batch] for i in range(0, len(rows), args.batch)]
    for i, chunk in enumerate(chunks):
        threads = []
        split = max(1, len(chunk) // args.workers)
        for w in range(args.workers):
            part = chunk[w * split:(w + 1) * split if w < args.workers - 1 else len(chunk)]
            if not part:
                continue
            t = threading.Thread(target=worker, args=(None, part, args.out_db, args.delay))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        print(f"  batch {i}: done ({min((i + 1) * args.batch, len(rows))}/{len(rows)})", flush=True)


if __name__ == "__main__":
    main()
