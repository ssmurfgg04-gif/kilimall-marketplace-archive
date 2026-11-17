#!/usr/bin/env bash
# Package data/kilimall.db and publish it to the rolling `live-latest` release.
#
# Guards (any failure => nothing is uploaded):
#   - DB must exist and hold at least one listing or warc_item row
#   - no table may have FEWER rows than when ci_seed.sh seeded it
#     (stops a stale or truncated DB from overwriting the release)
# Writes data/kilimall.db.zip, data/kilimall-live-latest.parquet, data/SHA256SUMS.
# Needs: GH_TOKEN, GITHUB_REPOSITORY.
set -euo pipefail
[ -s data/kilimall.db ] || { echo "[guard] DB missing — refusing to publish"; exit 1; }

python - <<'PY'
import json, os, sqlite3, sys
db = sqlite3.connect('data/kilimall.db')
now = {}
for t in ('listings', 'warc_item', 'price_history'):
    try:
        now[t] = db.execute(f'select count(*) from {t}').fetchone()[0]
    except sqlite3.Error:
        now[t] = 0
seed = json.load(open('data/.seed_counts')) if os.path.exists('data/.seed_counts') else {}
print('publish counts:', now, 'seed counts:', seed)
if now['listings'] + now['warc_item'] == 0:
    sys.exit('[guard] DB has no listings and no warc_item rows — refusing to publish')
shrunk = [t for t in now if now[t] < seed.get(t, 0)]
if shrunk:
    sys.exit(f'[guard] tables shrank vs seed: {shrunk} — refusing to publish')
summ = os.environ.get('GITHUB_STEP_SUMMARY')
if summ:
    with open(summ, 'a') as f:
        f.write('### Published live-latest\n\n| table | seeded | published | added |\n|---|---|---|---|\n')
        for t in now:
            f.write(f'| {t} | {seed.get(t, 0)} | {now[t]} | {now[t] - seed.get(t, 0)} |\n')
PY

mkdir -p data/cold
python - <<'PY'
import sqlite3
s = sqlite3.connect('data/kilimall.db'); d = sqlite3.connect('data/cold/kilimall.db')
s.backup(d); d.execute("PRAGMA wal_checkpoint(TRUNCATE)"); d.close(); s.close()
PY
(cd data/cold && python -c "import zipfile; zipfile.ZipFile('../kilimall.db.zip','w',zipfile.ZIP_DEFLATED).write('kilimall.db')")
rm -rf data/cold
python -c "import duckdb; duckdb.sql(\"install sqlite; load sqlite; copy (select * from sqlite_scan('data/kilimall.db','listings')) to 'data/kilimall-live-latest.parquet' (format parquet)\")"
(cd data && sha256sum kilimall.db.zip kilimall-live-latest.parquet > SHA256SUMS)

if ! gh release view live-latest --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
  gh release create live-latest --repo "$GITHUB_REPOSITORY" --title 'Live dataset (rolling)' --prerelease \
    --notes 'Rolling dataset: live /listing JSON-LD (robots-compliant) + Common Crawl history.'
fi
gh release upload live-latest data/kilimall.db.zip data/kilimall-live-latest.parquet data/SHA256SUMS \
  --clobber --repo "$GITHUB_REPOSITORY"
echo "published live-latest"
