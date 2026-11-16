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

## GitHub-hosted runner path (canonical live path, 2026-11-16)

robots.txt allows the sitemap + `/listing/*` (disallows `/api/*` — never used),
and Kilimall does not bot-wall hosted runner IPs, so `.github/workflows/live-delta.yml`
runs the polite delta 6×/day EAT (08/11/14/17/20/23 EAT) and on `workflow_dispatch`
(flash-sale refresh; ~2–4 min):

- Restores sitemap index + DB cache (actions/cache, prefix `kilimall-data-v1-`);
  seeds `data/kilimall.db` from the rolling `live-latest` release if absent.
- Monday: `kilimall_sitemap.py --refresh` (skip-downloads cached shards).
- Collects changed/unseen items (`--mode delta`, `--max-items 60`) **plus the
  15 stalest known listings** (`--refresh-oldest 15`) — without that, old prices
  would freeze forever (sitemap lastmod < our capture date ⇒ skipped forever).
- Views-free risk heuristic (`scripts/listing_risk.py`): price vs trailing
  SKU median, availability flip count, discount persistence → `listing_risk`
  table. Experimental, NOT a fraud verdict.
- Integrity guards: refuses to publish an empty/missing DB; every run writes a
  `GITHUB_STEP_SUMMARY` (listings/priced/died/newest run new+changed).
- 1st of month: `scripts/robots_check.py` (asserts OUR policy: /api still
  disallowed, sitemap + /listing usable) **and** an immutable rollup release
  `vYYYY-MM-DD` (assets clobbered only on the rolling tag, never on rollups).
- `.github/workflows/backfill.yml` — manual deep sweep, `--offset` pages the
  344k-url index; `offset=250, 500, …` per dispatch. 250/night ≈ 3.8 years for
  full coverage — delta is the right tool for tracking; this is for rescue.

A local residential timer was considered but NOT kept — single writer rule.

## Legacy Linux local runner (disabled, kept for reference)

`~/.local/bin/kilimall-live-daily.sh` + `kilimall-live-daily.timer` (local)
predates the hosted-runner path and was never enabled. Kept as documented history.
Logs: `logs/` (gitignored). All paths env-overridable via `KILIMALL_ROOT`.

## Future layers

| Layer | Source | Notes |
|---|---|---|
| Historical delta | Common Crawl index for `kilimall.co.ke` (CDX API on GH Actions; data from allowed network) | mirror of jiji Layer 0; requires `cc_index` collector port |
| Deal hunting | price < k × trailing category average | needs category derivation from breadcrumb metadata (not in JSON-LD) |
| Risk/views | seller/store metadata from `store-01` sitemap + listing pages | views not exposed server-side; proxies only |

## Provenance notes (2026-11-16)

- Seeded 2026-11-16: 40 newest listings (all priced KES, all InStock in sample).
- First GH-hosted delta run id 43817963960 (after requirements.txt pip-cache fix).
- Coverage = sitemap index (344,841 URLs, lastmod 2026-11-14) + delta accumulation.
  Historical (pre-2026-11-16) prices NOT included; requires Common Crawl mining.
- Every price carries first_seen/last_seen; every observation lands in price_history.

## Ops

- Secrets: reuse `~/.config/jiji-release.env`-style local env (repo-scoped PAT,
  mode 600). Never commit. KeePass vault entry `jiji-marketplace-archive-github-token`
  pattern; create `kilimall-marketplace-archive-github-token`.
- Never run from shared IPs at volume; daily delta only.
- FX pass-through: prices in KES; no FX table included yet.

## Common Crawl campaign (2026-11-16 — the "how Jiji did millions in <1 month" play)

Phase 1 — index (running NOW): `.github/workflows/cc-mine.yml` — 32 CC-MAIN
crawl legs (2024→2026), `max-parallel: 20`, each leg paginates
`index.commoncrawl.org` CDXJ for prefix `kilimall.co.ke/listing/*` + `status:200`,
keeps records to `cc_index/kilimall.co.ke.jsonl` and uploads a gzipped shard
(`cc-index-<crawl>.jsonl.gz`) to the rolling `cc-index` release. Zero requests
to kilimall.co.ke. Re-polls every 3h; legs no-op once their asset exists.
Local resumable collector: `scripts/cc_index_collect.py` (markers in cc_index/.done/).

Phase 2 — manifest + shards (offline):
```bash
for f in `gh release download cc-index --pattern '*.jsonl.gz' -O /tmp/`; do :; done
gh release download cc-index --repo ssmurfgg04-gif/kilimall-marketplace-archive --pattern '*.jsonl.gz' --dir cc_index/
for f in cc_index/*.jsonl.gz; do gunzip -k "$f"; done
python scripts/cc_layer0.py --min-year 2024   # -> cc_manifest/manifest.parquet
python scripts/cc_partition.py               # -> data/warc_shards/kilimall_shard_00..19.tsv
git add data/warc_shards && git commit -m 'shards: kilimall CC capture ranges' && git push
```

Phase 3 — body drain (hosted legs): `.github/workflows/cc-drain.yml` (manual,
`shards=all`) — 20 legs × 4 workers, byte-range GETs to `data.commoncrawl.org`
(CC CDN), JSON-LD parsed inline (same extractor as live collector), part DBs
uploaded as artifacts (`if: always()` — dead legs bank progress).

Phase 4 — merge (local):
```bash
for i in $(seq 0 19); do gh run download RUN_ID --name "warc-part-$i" --dir /tmp/warc-parts; done
python scripts/warc_merge.py --part-dir /tmp/warc-parts
```
→ `warc_item` table (capture_ts = original CC crawl time = true historical
prices) + `price_history` rows tagged `source='commoncrawl'`.

Provenance note: capture_ts is the ARCHIVE time, not our fetch time — every
historical row is attributable to its crawl. Zero kilimall.co.ke requests
across ALL phases.
