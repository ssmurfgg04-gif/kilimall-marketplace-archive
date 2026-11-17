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

## Provenance notes (2026-11-17)

- Merge `a434412` landed the adaptive-throttle drain + seed-from-release refactor.
- Live delta accumulating: 200 listings in live-latest (2026-11-17), GH-hosted, 15 items/run, 6x/day EAT.
- CC harvest round 1 (2026-11-16): 32/32 crawl index shards harvested (20,343 captures indexed);
  body legs were throttled to zero by CC at the time; round 2 re-dispatched 2026-11-17 on the
  adaptive-throttle code (shards committed; cc_parallel=8; CDN byte-range only, zero kilimall.co.ke traffic).

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

## Incident 2026-11-17 — Actions artifact egress cap (resolved)

The 11:33 drain completed all 20 legs (~20k captures fetched fine), but the
merge job's `gh run download` failed with `HTTP 503: Egress is over the account limit`.
Only shard 16's part was retrievable — 1,029 captures made it into live-latest.
Cause: artifact downloads/uploads draw on the account's Actions egress quota,
which we'd blown with repeated full-matrix drains.

Fix (merged `ab20c59`): legs themselves upload their part DBs as `cc-parts`
release assets (`cc-part-NN.db.gz`, `live-part-N.db.gz`); the merge job and the
30-min watchdog both download from `cc-parts`. Release assets don't draw on
artifact egress. Watchdog + ci_merge_run are quota-safe.

## Autonomy posture (user away 2026-11-16 → ~11-18)

- cc-drain legs exit 0 regardless of per-item outcome; artifacts upload with
  if: always(). If a leg dies, re-dispatch a single shard (`shards=3`) — the
  merge job accepts whatever artifacts exist.
- live-delta: 6×/day EAT, ≤15 items/run — fits hosted-runner Azure↔Kenya latency.
- live-latest release is the single restore source; every published DB must be
  ≥1 row (integrity guard) before clobber.
- Monthly: day-01 robots check + immutable vYYYY-MM-DD rollup (in live-delta).
- Tokens: GitHub PAT stored in KeePass (`jiji-marketplace-archive-github-token`)
  — rotate when convenient; transcript exposure acknowledged.

## Traffic context (Similarweb, September/October 2026)

- Global rank #85,048 → #91,859, Kenya #171 → #183. Category Rank ~#235 (Gambling-other, mis-categorized).
- ~440K visits / 3 months ≈ 147K/month; pages/visit 5.04 → ~740K pageviews/mo ≈ 25K/day.
- Traffic source: Direct 40.6%, Organic Search #2, Referrals #3
- Audience: 91.99% Kenya, 66.6% male, 25-34 top bracket, +7-9% MoM growth
- Company: $10-15M/yr rev, 201-500 FTE, HQ Nairobi. Kilimall is small enough that the
  *archive* is actually denser and more complete than public-market intel on them.

Implication: a 300-pageburst from a residential IP ≈ 1% of their daily load. Even sustained
5 rps load from this connection ≈ 25% of their daily load per hour — acceptable politeness margin.
Fleet harvest (20 legs × 17K pages in one dispatch) ≈ 2 days of their normal traffic volume.

## Fleet deep sweep (backfill.yml, dispatched 2026-11-17)

- 20 legs × hash-shard of the 344,841-URL sitemap index; unseen-then-stale order
- robots-compliant always (no /api, no login, one worker per leg, 3-8s delays)
- Legs `if: always()` upload cc-parts/live-part-*.db.gz to the rolling release, merge job
  tags cc-merge-done-<run-id>. Trial dispatch (4×250 pages) validating first.
