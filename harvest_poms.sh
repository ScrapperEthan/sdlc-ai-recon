#!/usr/bin/env bash
# harvest_poms.sh
# Download ONLY pom.xml files from every repo in a GitHub org/owner, without
# cloning. Produces a tree:  <OUT>/<repo>/<path>/pom.xml  which feeds
# recon_maven_graph.py directly.
#
# Why: for dependency-graph recon you do not need source or git history — just
# the poms (KB each). This pulls a few MB total instead of cloning gigabytes.
#
# Usage:
#   ./harvest_poms.sh <ORG_OR_OWNER> [OUT_DIR]
#
# Requires: gh (authenticated), bash, base64, tr.
set -uo pipefail

ORG="${1:?usage: harvest_poms.sh <ORG_OR_OWNER> [OUT_DIR]}"
OUT="${2:-poms}"
mkdir -p "$OUT"

echo "Listing repos in '$ORG' ..."
mapfile -t REPOS < <(gh repo list "$ORG" --no-archived --limit 1000 --json name -q '.[].name')
total=${#REPOS[@]}
echo "Found $total repos. Harvesting pom.xml only ..."

i=0
for repo in "${REPOS[@]}"; do
  i=$((i+1))
  branch=$(gh api "repos/$ORG/$repo" --jq '.default_branch' 2>/dev/null) \
    || { echo "[$i/$total] $repo: skip (no access)"; continue; }

  # List every pom.xml path via the git tree (one API call per repo).
  paths=$(gh api "repos/$ORG/$repo/git/trees/$branch?recursive=1" \
            --jq '.tree[] | select(.path | endswith("pom.xml")) | .path' 2>/dev/null)

  if [ -z "$paths" ]; then
    echo "[$i/$total] $repo: no pom.xml (non-Maven?)"
    continue
  fi

  n=0
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    dest="$OUT/$repo/$p"
    mkdir -p "$(dirname "$dest")"
    if gh api "repos/$ORG/$repo/contents/$p?ref=$branch" --jq '.content' 2>/dev/null \
         | tr -d '\n' | base64 -d > "$dest" 2>/dev/null; then
      n=$((n+1))
    fi
  done <<< "$paths"
  echo "[$i/$total] $repo: $n pom(s)"
done

echo
echo "Done -> $OUT/"
echo "Next:  python recon_maven_graph.py $OUT"
