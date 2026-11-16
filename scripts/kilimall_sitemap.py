#!/usr/bin/env python3
"""Layer 0 — official sitemap index of Kilimall's public storefront.

robots.txt (https://www.kilimall.co.ke/robots.txt, 2026-11-16) allows the
sitemap and /listing/* pages, and disallows /api/*. This module downloads
the product/category/store sitemap shards (hosted on a CDN bucket) and
builds a local URL index. It never hits kilimall.co.ke HTML/API.

Usage: python scripts/kilimall_sitemap.py [--refresh]
Output: data/url_index.tsv — sku, url, lastmod, shard
"""
import argparse
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

BASE = os.environ.get("KILIMALL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA = Path(BASE) / "data"
CACHE = DATA / "cache"
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
SHARDS_RE = re.compile(r"^sitemap-(products|categories|stores)-\d+\.xml$")
SKU_RE = re.compile(r"/listing/(\d+)-")
UA = {"User-Agent": "kilimall-marketplace-archive/0.1 (+https://github.com/ssmurfgg04-gif/kilimall-marketplace-archive)"}


def fetch(url, path, refresh):
    if path.exists() and not refresh:
        return False  # cached
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        path.write_bytes(r.read())
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="re-download cached shards")
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)

    fetch("https://www.kilimall.co.ke/sitemap-index.xml", CACHE / "sitemap-index.xml", True)
    idx = ET.parse(CACHE / "sitemap-index.xml")
    shards = [e.text for e in idx.findall(".//sm:loc", NS) if SHARDS_RE.search(e.text.rsplit("/", 1)[-1])]
    print(f"{len(shards)} shards")

    rows, kept = [], 0
    for url in shards:
        name = url.rsplit("/", 1)[-1]
        dest = CACHE / name
        changed = fetch(url, dest, args.refresh)
        kept += 0 if changed else 1
        root = ET.parse(dest)
        n = 0
        for e in root.findall(".//sm:url", NS):
            loc, lastmod = e.find("sm:loc", NS).text, (e.find("sm:lastmod", NS).text if e.find("sm:lastmod", NS) is not None else "")
            m = SKU_RE.search(loc)
            sku = m.group(1) if m else ""
            rows.append((sku, loc, lastmod, name))
            n += 1
        print(f"  {name}: {n} urls ({'downloaded' if changed else 'cached'})")

    rows.sort(key=lambda r: (r[2] or "", r[1]), reverse=True)
    out = DATA / "url_index.tsv"
    with out.open("w", encoding="utf-8") as f:
        f.write("sku\turl\tlastmod\tshard\n")
        for sku, url, lastmod, shard in rows:
            f.write(f"{sku}\t{url}\t{lastmod}\t{shard}\n")
    print(f"url_index: {len(rows)} urls -> {out}")


if __name__ == "__main__":
    main()
