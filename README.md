# Kilimall marketplace archive

An auditable, provenance-first snapshot of public Kilimall (kilimall.co.ke) product listings for research and analysis — the Kilimall counterpart of `ssmurfgg04-gif/jiji-marketplace-archive`. The repository contains the pipeline and documentation; immutable data snapshots are distributed as GitHub Release assets rather than normal Git objects.

## Pipeline at a glance

| Layer | Script | Source | Touches kilimall.co.ke? |
|---|---|---|---|
| 0 — URL index | `scripts/kilimall_sitemap.py` | official `sitemap-index.xml` → CDN-hosted product/category/store shards | no (CDN only) |
| 1 — live delta | `scripts/kilimall_incremental.py` | public `/listing/{sku}-{slug}` HTML, server-rendered JSON-LD `Product` block | yes, politely |

`robots.txt` **disallows `/api/*`** — this pipeline never calls the API. Listing pages and the sitemap are allowed. One worker, random 3–8s delays, in-flight cap (`--max-items`, default 60). Do not run collectors aggressively.

## Current inventory (2026-11-17)

- Live `listings`: 1,200 (as of release 2026-11-17 14:32 EAT).
- `warc_item`: 17,233 captures (2024-09 → 2026-10). Common Crawl captures of /listing/* pages (2024→2026) —
  ~20,343 captures at first harvest; byte-range fetched from CC's CDN,
  zero requests to kilimall.co.ke. Capture timestamps = original crawl times.
- `price_history`: every observation; `source='commoncrawl'` for CC-mined rows.
- `listing_risk`: views-free price/availability heuristic (experimental).

## Quick start (query a release asset)

```sql
SELECT title, price, availability, capture_ts
FROM warc_item
ORDER BY capture_ts DESC
LIMIT 20;

SELECT count(*), min(capture_ts), max(capture_ts) FROM warc_item;
```

CC-mined SKUs join to listings by listing ID (the numeric part of
`/listing/<id>-...`).

## Repository layout

- `scripts/` — sitemap indexer, polite JSON-LD collector, data-quality maintenance.
- `PIPELINE.md` — collection order, provenance, coverage limits, operational notes.
- `schema/` — SQLite schema reference.

## Source & coverage

- Sitemap products shards are hosted on a CDN bucket (`mkp.obs.af-south-1.myhuaweicloud.com/c/sitemap/ke/`, lastmod 2026-11-14 at first harvest) — first-party index, ~340k product URLs expected.
- Live records carry `first_seen`/`last_seen` (UTC ISO). Every price is attributable to a capture date — no dateless rows.
- Historical coverage (pre-first-harvest prices) is **not** included yet; it requires Common Crawl index mining for `kilimall.co.ke` (see PIPELINE.md → Future layers).

## Ethics / scope

Public, server-rendered listings only; never behind-login endpoints, never the disallowed API, never accounts/checkout/orders. Treat prices and availability as historical observations, not current offers or endorsements. Respect rate limits; review the source platform's terms before commercial redistribution.

## Release integrity

Each release includes `SHA256SUMS`. Verify downloaded assets before analysis.
