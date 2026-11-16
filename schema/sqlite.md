# SQLite schema reference (data/kilimall.db)

`listings` — one row per public /listing/{sku} product page.
| column | type | notes |
|---|---|---|
| guid | TEXT PK | SKU from the product page / sitemap |
| title | TEXT | Product name (schema.org Product.name) |
| price | REAL | Offers.price (KES) |
| currency | TEXT | Offers.priceCurrency (KES) |
| url | TEXT | Canonical /listing/{sku}-{slug} URL |
| brand | TEXT | Offers/Product brand (often "Kilimall" private label) |
| availability | TEXT | schema.org URI (InStock / OutOfStock …) |
| item_condition | TEXT | schema.org URI (NewCondition) |
| first_seen / last_seen | TEXT | UTC ISO, this pipeline's capture time |
| last_hash | TEXT | BLAKE3 of canonical record — drives price_history |
| description | TEXT | Product description (as embedded) |
| died_at | TEXT | last_seen when unseen > N days (additive) |
| source | TEXT | `live` (HTML JSON-LD) |

`price_history` — guid, seen_at, price, hash, source. One row per observation; BLAKE3 change detection means every observation is recorded but only changed records move history semantics.

`runs` — id, started_at, mode, new, changed. One row per collector run.
