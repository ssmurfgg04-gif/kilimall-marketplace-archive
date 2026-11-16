#!/usr/bin/env python3
"""Cross-fill listings.category from CC-mined warc_item breadcrumbs (additive, idempotent)."""
import os
import sqlite3

BASE = os.environ.get("KILIMALL_ROOT", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.path.join(BASE, "data", "kilimall.db")


def main():
    db = sqlite3.connect(DB, timeout=120)
    filled = db.execute("""
        UPDATE listings
           SET category = (SELECT category FROM warc_item
                            WHERE warc_item.sku = listings.guid
                              AND category IS NOT NULL AND category != ''
                            ORDER BY capture_ts DESC LIMIT 1)
         WHERE (category IS NULL OR category = '')
    """).rowcount
    db.commit()
    have = db.execute("select count(*) from listings where category IS NOT NULL AND category != ''").fetchone()[0]
    total = db.execute("select count(*) from listings").fetchone()[0]
    print(f"backfilled {filled}; listings with category: {have}/{total}")


if __name__ == "__main__":
    main()
