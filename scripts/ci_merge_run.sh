#!/usr/bin/env bash
# Merge every part DB from one `CC body drain` run into the release, then tag it.
#   usage: scripts/ci_merge_run.sh <drain_run_id>
# Handles both kinds of legs:
#   warc-part-*  Common Crawl history  -> warc_merge.py
#   live-part-*  live /listing pages   -> live_merge.py
# Idempotent: safe to re-run; the tag cc-merge-done-<id> marks success.
# Needs: GH_TOKEN, GITHUB_REPOSITORY.
set -euo pipefail
rid="$1"
rm -rf /tmp/warc-parts /tmp/live-parts
mkdir -p /tmp/warc-parts /tmp/live-parts
gh run download "$rid" --repo "$GITHUB_REPOSITORY" --pattern "warc-part-*" --dir /tmp/warc-parts || echo "no warc parts"
gh run download "$rid" --repo "$GITHUB_REPOSITORY" --pattern "live-part-*" --dir /tmp/live-parts || echo "no live parts"
nw=$(find /tmp/warc-parts -name '*.db' | wc -l); nl=$(find /tmp/live-parts -name '*.db' | wc -l)
echo "run $rid: $nw CC part DBs, $nl live part DBs"
if [ "$nw" -eq 0 ] && [ "$nl" -eq 0 ]; then echo "::error::no part DBs in run $rid"; exit 1; fi

bash scripts/ci_seed.sh
[ "$nw" -gt 0 ] && python scripts/warc_merge.py --part-dir /tmp/warc-parts
[ "$nl" -gt 0 ] && python scripts/live_merge.py --part-dir /tmp/live-parts
python scripts/backfill_categories.py || echo "::warning::category backfill failed"
bash scripts/ci_publish.sh

git config user.email "github-actions[bot]@users.noreply.github.com"
git config user.name "github-actions[bot]"
tag="cc-merge-done-$rid"
if git ls-remote --tags origin | grep -q "refs/tags/$tag\$"; then
  echo "$tag already exists"
else
  git tag -a "$tag" -m "Merged drain run $rid ($nw CC parts, $nl live parts)"
  git push origin "$tag"
fi
