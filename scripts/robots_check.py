#!/usr/bin/env python3
"""Monthly policy check (day 01 of each month, run from the delta workflow).

NOTE: this asserts OUR archival policy, not Kilimall's ToS:
  - /api/* must still be disallowed (we never call it — and we want to notice
    if the directive disappears, because our own ban stands regardless),
  - the sitemap must exist and /listing pages must not be disallowed
    (our extraction path).
Exit 0 = policy intact; exit 1 = re-check the ToS posture before continuing.
"""
import re
import sys
import urllib.request

URL = "https://www.kilimall.co.ke/robots.txt"


def main():
    t = urllib.request.urlopen(URL, timeout=30).read().decode("utf-8", "replace")
    api_disallowed = bool(re.search(r"(?im)^\s*disallow:\s*/api/", t))
    has_sitemap = bool(re.search(r"(?im)^\s*sitemap:", t))
    listing_allowed = bool(re.search(r"(?im)^\s*disallow:\s*/listing", t)) is False
    print(f"robots.txt: api-disallowed={api_disallowed} sitemap={has_sitemap} listing-allowed={listing_allowed}")
    if not (api_disallowed and has_sitemap and listing_allowed):
        print("policy check FAILED — review PIPELINE.md / ToS before running collectors")
        sys.exit(1)
    print("policy check OK — no /api use, sitemap + /listing available for archival posture")


if __name__ == "__main__":
    main()
