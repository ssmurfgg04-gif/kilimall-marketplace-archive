#!/usr/bin/env bash
# Seed data/kilimall.db from the rolling `live-latest` release — ALWAYS.
#
# The release is the single source of truth. The actions cache may hold an
# older DB (e.g. one without merged Common Crawl rows); publishing that would
# silently clobber newer data, so any cached DB is replaced here.
#
# Records the seeded row counts in data/.seed_counts so ci_publish.sh can
# refuse to publish a DB that is smaller than what it started from.
# Needs: GH_TOKEN, GITHUB_REPOSITORY.
set -euo pipefail
mkdir -p data
rm -f data/kilimall.db data/kilimall.db-wal data/kilimall.db-shm data/kilimall.db.zip

if gh release view live-latest --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
  ok=0
  for i in 1 2 3; do
    if gh release download live-latest --repo "$GITHUB_REPOSITORY" -p kilimall.db.zip -D data --clobber; then ok=1; break; fi
    echo "release download failed (attempt $i) — retrying"; sleep $((i * 10))
  done
  [ "$ok" = 1 ] || { echo "::error::live-latest exists but its DB could not be downloaded — refusing to start from empty"; exit 1; }
  python -c "import zipfile; zipfile.ZipFile('data/kilimall.db.zip').extractall('data')"
  rm -f data/kilimall.db.zip
  echo "seeded from rolling release"
else
  echo "::warning::no live-latest release yet — bootstrapping an empty archive"
fi

python - <<'PY'
import json, os, sqlite3
c = {}
if os.path.exists('data/kilimall.db'):
    db = sqlite3.connect('data/kilimall.db')
    for t in ('listings', 'warc_item', 'price_history'):
        try:
            c[t] = db.execute(f'select count(*) from {t}').fetchone()[0]
        except sqlite3.Error:
            c[t] = 0
json.dump(c, open('data/.seed_counts', 'w'))
print('seed counts:', c)
PY
