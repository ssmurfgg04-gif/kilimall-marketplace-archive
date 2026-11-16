#!/usr/bin/env python3
"""Common Crawl index collector for kilimall.co.ke (/listing/* only).

Resumable, retry-friendly, and driven by a watchdog timer.
- Enumerates CC-MAIN crawl collections from collinfo.json (or --crawl).
- Per crawl, paginates the CDXJ index (pageSize=1500), keeps records whose
  url contains '/listing/' and which have status:200 (API-side filter).
- Raw records -> cc_index/kilimall.co.ke.jsonl (append-only).
- Completed crawls leave markers cc_index/.done-<crawl>; they're skipped forever.
- Prints a progress line per crawl; exits 0 always (completion != failure).

Usage:
  python scripts/cc_index_collect.py                 # one pass over everything
  python scripts/cc_index_collect.py --crawl CC-MAIN-2026-30   # single crawl
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

BASE = os.environ.get("KILIMALL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = Path(BASE) / "logs" / ".." / "cc_index"
OUT_DIR = (Path(BASE) / "cc_index").resolve()
OUT = OUT_DIR / "kilimall.co.ke.jsonl"
DONE = OUT_DIR / ".done"
UA = {"User-Agent": "kilimall-archive-miner/0.1 (+https://github.com/ssmurfgg04-gif/kilimall-marketplace-archive)"}
SESSION = requests.Session()
SESSION.headers.update(UA)

LISTING_RE = re.compile(r"/listing/\d+-")
MIN_YEAR = 2024


def get_json(coll, page, page_size=500, retries=6):
    url = f"https://index.commoncrawl.org/{coll}-index"
    params = {"url": "kilimall.co.ke/listing/*", "output": "json",
              "page": page, "pageSize": page_size,
              "filter": "=status:200"}
    wait = 2
    for i in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=90)
            if r.status_code == 200:
                recs = []
                for line in r.text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        j = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(j, dict) and j.get("filename"):
                        if LISTING_RE.search(j.get("url", "")):
                            recs.append(j)
                return recs
            if r.status_code == 429:
                time.sleep(15)
                continue
            if r.status_code in (502, 503, 504):
                time.sleep(wait)
                wait = min(wait * 2, 60)
                continue
            if r.status_code == 404:
                return None
            print(f"  !! {coll} page {page}: HTTP {r.status_code}", flush=True)
            return []
        except requests.RequestException as e:
            print(f"  !! {coll} page {page}: {e}", flush=True)
            time.sleep(wait)
            wait = min(wait * 2, 60)
    return []


def crawl_list(single=None):
    if single:
        return [single]
    r = SESSION.get("https://index.commoncrawl.org/collinfo.json", timeout=60)
    cols = r.json()
    out = []
    for c in cols:
        mid = c.get("id", "")
        if not mid.startswith("CC-MAIN"):
            continue
        try:
            year = int(mid.split("-")[2])
        except Exception:
            continue
        if year >= MIN_YEAR:
            out.append(mid)
    return out


def run_one(coll):
    DONE.mkdir(parents=True, exist_ok=True)
    marker = DONE / coll
    if marker.exists():
        print(f"{coll}: cached — skip", flush=True)
        return True
    print(f"{coll}: collecting...", flush=True)
    total_added = 0
    page = 0
    while True:
        recs = get_json(coll, page)
        if not recs:
            if recs is None:
                print(f"{coll}: no captures (404)", flush=True)
            break
        with OUT.open("a", encoding="utf-8") as f:
            for j in recs:
                f.write(json.dumps(j, separators=(",", ":")) + "\n")
        total_added += len(recs)
        print(f"{coll} page {page}: +{len(recs)}", flush=True)
        page += 1
        time.sleep(1.5)
    marker.touch()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.touch(exist_ok=True)
    print(f"{coll}: DONE (+{total_added} records)", flush=True)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--crawl", help="run a single crawl collection instead of all")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    crawls = crawl_list(args.crawl)
    print(f"{len(crawls)} crawls to process: {crawls[0]} ... {crawls[-1] if crawls else ''}", flush=True)
    for coll in crawls:
        run_one(coll)
    # never exit non-zero: the watchdog keeps re-polling and this gate decides "done"
    done = len(list(DONE.glob("CC-MAIN-*"))) if DONE.exists() else 0
    print(f"watchdog: {done} crawl(s) collected", flush=True)


if __name__ == "__main__":
    main()
