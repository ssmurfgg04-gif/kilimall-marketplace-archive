#!/usr/bin/env python3
"""Fetch Common Crawl WARC bodies for kilimall /listing/* captures and parse them.

Byte-range requests only to data.commoncrawl.org (never kilimall.co.ke).
Body -> JSON-LD (same ItemPage/Product extractor as the live collector) ->
part SQLite /tmp/warc_part_<shard>.db (table warc_item).

Speed/robustness (2026-11-17):
  - --skip-db: skip captures already in the archive (warc_item / cc_done), so a
    re-run only fetches what is still missing.
  - Shared adaptive throttle: 429/5xx doubles the gap between requests for ALL
    workers (honours Retry-After); successes slowly shrink it again.
  - 8 attempts with exponential backoff + jitter, then a slow final retry pass.
  - `done` records status ('ok' | 'nodata') so captures with no product data
    are not re-fetched forever.
  - --deadline-min: stop cleanly before the job timeout so the part DB is kept.

Usage: python scripts/warc_fetch.py --shard-file data/warc_shards/kilimall_shard_00.tsv \
        --out-db /tmp/warc_part_00.db [--skip-db data/kilimall.db] [--workers 2] [--deadline-min 280]
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
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) kilimall-archive-miner/0.3 (+https://github.com/ssmurfgg04-gif/kilimall-marketplace-archive)"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS warc_item(
  url TEXT, capture_ts TEXT, digest TEXT, sku TEXT, title TEXT, price REAL,
  currency TEXT, availability TEXT, brand TEXT, category TEXT, description TEXT,
  PRIMARY KEY (url, capture_ts)
);
CREATE TABLE IF NOT EXISTS done(url TEXT, capture_ts TEXT, PRIMARY KEY(url, capture_ts));
"""
LOCK = threading.Lock()
STOP = threading.Event()
STATS = {"ok": 0, "nodata": 0, "retry_429": 0, "retry_5xx": 0, "fail": 0, "deferred": 0}


class Throttle:
    """Pacing shared by every worker in this process.

    CC's CDN throws intermittent 503s / broken reads (~30% at times) that an
    immediate retry usually fixes, so single errors are NOT punished globally.
    The gap only grows while the recent error RATE is high (sustained
    throttling), and shrinks quickly once it drops. Retry-After is honoured.
    """

    def __init__(self, base, cap=20.0, window=40):
        self.base, self.cap, self.gap = base, cap, base
        self.until = 0.0
        self.recent = []
        self.window = window
        self.lock = threading.Lock()

    def wait(self):
        while True:
            with self.lock:
                now = time.monotonic()
                if now >= self.until:
                    self.until = now + self.gap * random.uniform(0.7, 1.3)
                    return
                pause = self.until - now
            time.sleep(min(pause, 5))

    def _note(self, ok):
        self.recent.append(ok)
        if len(self.recent) > self.window:
            self.recent.pop(0)
        if len(self.recent) < 10:
            return
        err = 1 - sum(self.recent) / len(self.recent)
        if err > 0.75:
            self.gap = min(self.cap, self.gap * 1.5)
        elif err < 0.55:
            self.gap = max(self.base, self.gap * 0.7)

    def ok(self):
        with self.lock:
            self._note(True)

    def backoff(self, retry_after=None):
        with self.lock:
            self._note(False)
            if retry_after:
                self.until = max(self.until, time.monotonic() + min(retry_after, 120))


def fetch_range(fn, off, ln, sess):
    off, ln = int(off), int(ln)
    try:
        r = sess.get(CC + fn, headers={"Range": f"bytes={off}-{off + ln - 1}", **UA}, timeout=60)
    except requests.RequestException:
        return "retry_5xx", None, None
    if r.status_code == 429 or r.status_code in (500, 502, 503, 504):
        ra = r.headers.get("Retry-After")
        try:
            ra = float(ra) if ra else None
        except ValueError:
            ra = None
        return ("retry_429" if r.status_code == 429 else "retry_5xx"), None, ra
    if r.status_code not in (200, 206):
        return "fail", None, None
    raw = r.content
    try:
        html = gzip.GzipFile(fileobj=io.BytesIO(raw)).read().decode("utf-8", "replace")
    except Exception:
        try:
            import zlib
            html = zlib.decompress(raw).decode("utf-8", "replace")
        except Exception:
            return "fail", None, None
    return "ok", html, None


def save(db_path, url, ts, key, r_, status):
    with LOCK:
        db = sqlite3.connect(db_path, timeout=60)
        if r_ is not None:
            db.execute(
                "insert or ignore into warc_item values(?,?,?,?,?,?,?,?,?,?,?)",
                (url, ts, key, r_["guid"], r_["title"], r_["price"], r_["currency"],
                 r_["availability"], r_["brand"], r_["category"], r_["description"]))
        db.execute("insert or replace into done(url, capture_ts, status) values(?,?,?)", (url, ts, status))
        db.commit()
        db.close()


def process(row, sess, thr, db_path, attempts):
    fn, off, ln, url, ts, _mime, key = row[:7]
    status = "fail"
    for attempt in range(attempts):
        if STOP.is_set():
            return "stopped"
        thr.wait()
        status, html, ra = fetch_range(fn, off, ln, sess)
        if status == "ok":
            thr.ok()
            break
        with LOCK:
            STATS[status] = STATS.get(status, 0) + 1
        if status == "fail":
            break
        thr.backoff(ra)
        time.sleep(min(30, 0.5 * (2 ** attempt) * random.uniform(0.5, 1.5)))
    if status != "ok":
        return status
    p = parse_product(html)
    r_ = record(p) if p else parse_legacy(html, url)
    if not p and not r_["title"] and r_["price"] is None:
        save(db_path, url, ts, key, None, "nodata")
        with LOCK:
            STATS["nodata"] += 1
        return "nodata"
    save(db_path, url, ts, key, r_, "ok")
    with LOCK:
        STATS["ok"] += 1
    return "ok"


def run_pool(rows, workers, thr, db_path, attempts, deadline):
    q = list(rows)
    deferred = []
    qlock = threading.Lock()

    def loop():
        sess = requests.Session()
        while not STOP.is_set():
            if time.monotonic() > deadline:
                STOP.set()
                break
            with qlock:
                if not q:
                    return
                row = q.pop()
            res = process(row, sess, thr, db_path, attempts)
            if res not in ("ok", "nodata", "stopped"):
                with qlock:
                    deferred.append(row)

    ts = [threading.Thread(target=loop, daemon=True) for _ in range(max(1, workers))]
    for t in ts:
        t.start()
    total = len(rows)
    while any(t.is_alive() for t in ts):
        time.sleep(30)
        with LOCK:
            print(f"  progress: left={len(q)}/{total} gap={thr.gap:.2f}s {STATS}", flush=True)
    return deferred


def load_skip(path):
    if not path or not os.path.exists(path):
        return set()
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    have = set()
    for t in ("warc_item", "cc_done"):
        try:
            have |= set(db.execute(f"select url, capture_ts from {t}"))
        except sqlite3.Error:
            pass
    db.close()
    return have


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shard-file", required=True)
    ap.add_argument("--out-db", required=True)
    ap.add_argument("--skip-db", help="archive DB; captures already in it are skipped")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--delay", type=float, default=0.5, help="base gap between requests (shared)")
    ap.add_argument("--attempts", type=int, default=8)
    ap.add_argument("--max", type=int, default=100000)
    ap.add_argument("--deadline-min", type=float, default=280)
    ap.add_argument("--batch", type=int, default=0, help="ignored (kept for old callers)")
    args = ap.parse_args()
    deadline = time.monotonic() + args.deadline_min * 60

    with sqlite3.connect(args.out_db) as db:
        db.executescript(SCHEMA)
        if "status" not in {r[1] for r in db.execute("pragma table_info(done)")}:
            db.execute("alter table done add column status TEXT default 'ok'")

    with open(args.shard_file, encoding="utf-8") as f:
        rows = [l.rstrip("\n").split("\t") for l in f if l.strip()]
    rows = [r for r in rows if len(r) >= 7]
    with sqlite3.connect(args.out_db) as db:
        done = set(db.execute("select url, capture_ts from done"))
    skip = load_skip(args.skip_db)
    todo = [r for r in rows if (r[3], r[4]) not in done and (r[3], r[4]) not in skip][: args.max]
    random.shuffle(todo)  # spread requests over many WARC files
    print(f"shard {os.path.basename(args.shard_file)}: {len(rows)} captures, "
          f"{len(skip & {(r[3], r[4]) for r in rows})} already archived, {len(done)} done here, "
          f"{len(todo)} to fetch", flush=True)

    thr = Throttle(args.delay)
    deferred = run_pool(todo, args.workers, thr, args.out_db, args.attempts, deadline)
    if deferred and not STOP.is_set():
        print(f"retry pass: {len(deferred)} deferred captures, 1 worker, slower", flush=True)
        time.sleep(30)
        thr.gap = max(thr.gap, args.delay * 4)
        deferred = run_pool(deferred, 1, thr, args.out_db, args.attempts + 2, deadline)
    STATS["deferred"] = len(deferred)
    print(f"done: {STATS} stopped_by_deadline={STOP.is_set()}", flush=True)
    summ = os.environ.get("GITHUB_STEP_SUMMARY")
    if summ:
        with open(summ, "a") as f:
            f.write(f"### {os.path.basename(args.shard_file)}\n\n`{STATS}` deadline_hit={STOP.is_set()}\n")


if __name__ == "__main__":
    main()
