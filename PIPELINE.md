# Kilimall Snapshot Pipeline

Two layers, cheapest-first: nothing below touches kilimall.co.ke unless the
layer above can't answer. Crawler posture mirrors `jiji-marketplace-archive`.
robots.txt (verified 2026-11-16) allows the sitemap and `/listing/*`; it
disallows `/api/*`, `/product*`, `/skuPrice*`, `/item*` etc. — **never call the
API**. Pages are server-rendered and embed schema.org JSON-LD (`ItemPage` with
`mainEntity` → `Product`), which is the extraction target.

## Layer 0 — URL index (zero kilimall.co.ke packets)

`scripts/kilimall_sitemap.py`:

1. Fetch `https://www.kilimall.co.ke/sitemap-index.xml` (3.7 KB, 23 shards).
2. Keep `products|categories|stores` shards (skip `home`, `search`, `promotions`).
   Products: `sitemap-products-01..17.xml` ≈ 4 MB each (~57 MB total), lastmod
   per URL. All downloads are resumeable (`data/cache/`, `--refresh` to redo).
3. Parse `<url>` → SKU from `/listing/(\d+)-` → `data/url_index.tsv`
   (sku, url, lastmod, shard). Sorted by lastmod desc.

## Layer 1 — Live incremental (least-touch path)

`scripts/kilimall_incremental.py --mode delta`:

```bash
python scripts/kilimall_incremental.py --mode delta --max-items 60   # daily delta
python scripts/kilimall_incremental.py --skus-file seed.txt          # explicit SKUs
```

- Candidates = URLs whose sitemap `lastmod` > db max(last_seen) OR unseen SKU;
  sorted by lastmod desc; capped by `--max-items` (default 60).
- Extraction = JSON-LD only (`<script type="application/ld+json">`,
  `@type: ItemPage`/`Product`): `sku`, `name`, `description`, `brand.name`,
  `offers.price`, `priceCurrency`, `availability`, `itemCondition`, `offers.url`.
- One worker, random 3–8s delays (`--dmin/--dmax`), 404/410 → mark `died_at`,
  transient errors → 2 retries, then skip.
- BLAKE3 over canonical record (guid|title|price|availability|currency) —
  only changed rows touch `price_history`.
- Daily snapshot: `data/snapshot-YYYY-MM-DD.parquet` via DuckDB `sqlite_scan`.
- Outputs under `data/` (gitignored). Never commit snapshots.

## Current coverage (2026-11-16)

Fresh build. `url_index.tsv` catalogued from the product/category/store
sitemap shards (lastmod 2026-11-14). Live rows accumulate from first run
(2026-11-16). Historical backfill is explicitly **not** done — see future layers.

## Linux local runner (systemd user timer)

No GitHub-hosted workflow touches kilimall.co.ke (shared runner IPs are a
bot-wall risk; see jiji ledger L-031). Instead:

- `~/.local/bin/kilimall-live-daily.sh` — daily 09:45 EAT
  (`kilimall-live-daily.timer`): pulls repo, refreshes `url_index.tsv` if older
  than 7 days, runs `--mode delta`, data-quality weekly slot, packages and (if
  `KILLIMALL_RELEASE_TOKEN` or `JIJI_RELEASE_TOKEN` resolvable) publishes the
  rolling `live-latest` release.
- `~/.local/bin/kilimall-fraud-watch` equivalent: not yet ported — Kilimall
  pages do not expose views; risk heuristics need redesign (see TODO).

Logs: `logs/` (gitignored). All paths env-overridable via `KILIMALL_ROOT`.

## Future layers

| Layer | Source | Notes |
|---|---|---|
| Historical delta | Common Crawl index for `kilimall.co.ke` (CDX API on GH Actions; data from allowed network) | mirror of jiji Layer 0; requires `cc_index` collector port |
| Deal hunting | price < k × trailing category average | needs category derivation from breadcrumb metadata (not in JSON-LD) |
| Risk/views | seller/store metadata from `store-01` sitemap + listing pages | views not exposed server-side; proxies only |

## Ops

- Secrets: reuse `~/.config/jiji-release.env`-style local env (repo-scoped PAT,
  mode 600). Never commit. KeePass vault entry `jiji-marketplace-archive-github-token`
  pattern; create `kilimall-marketplace-archive-github-token`.
- Never run from shared IPs at volume; daily delta only.
- FX pass-through: prices in KES; no FX table included yet.
