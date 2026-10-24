#!/usr/bin/env bash
# Push this repository's working tree to GitHub with the Contents API.
#
# The authoring sandbox denies the pipes `git push` needs, so every file is
# written through `gh api` instead. Use it when working from that sandbox:
#
#   SYNC_BRANCH=sync-test bash scripts/push-to-github.sh
#   bash scripts/push-to-github.sh            # main
#
# Requires: gh authenticated, python3, base64, sha1sum (or shasum).
set -euo pipefail

REPO="${SYNC_REPO:-ZXYGIT123/dsh-linux-arm64-deb}"
BRANCH="${SYNC_BRANCH:-main}"
ROOT="${SYNC_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"
WORK="$ROOT/.sync-work"
rm -rf "$WORK"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

export SYNC_BRANCH="$BRANCH"

echo "== resolve remote tree ($REPO@$BRANCH) =="
: > "$WORK/remote-tree.tsv"
if gh api "repos/$REPO/git/refs/heads/$BRANCH" --jq .object.sha > "$WORK/ref.txt" 2>/dev/null; then
  SHA="$(cat "$WORK/ref.txt")"
  echo "remote head: $SHA"
  gh api "repos/$REPO/git/trees/$SHA?recursive=1" \
    --jq '.tree[] | select(.type == "blob") | "\(.path)\t\(.sha)"' > "$WORK/remote-tree.tsv"
else
  echo "no remote branch yet; creating files from scratch"
fi
echo "remote files: $(wc -l < "$WORK/remote-tree.tsv")"

echo "== plan =="
python3 scripts/github_sync.py plan "$ROOT" "$WORK/remote-tree.tsv" "$WORK/plan"

urlencode_path() {
  python3 -c 'import sys, urllib.parse; print("/".join(urllib.parse.quote(part, safe="") for part in sys.argv[1].split("/")))' "$1"
}

if [ -s "$WORK/plan/puts.tsv" ]; then
  echo "== put =="
  while IFS=$'\t' read -r path payload; do
    payload="${payload%$'\r'}"
    path="${path%$'\r'}"
    encoded="$(urlencode_path "$path")"
    if gh api --method PUT "repos/$REPO/contents/$encoded" --input - <"$payload" >/dev/null 2>"$WORK/err.txt"; then
      echo "  put    $path"
    else
      echo "  FAILED $path"; cat "$WORK/err.txt"; exit 1
    fi
  done < "$WORK/plan/puts.tsv"
fi

if [ -s "$WORK/plan/deletes.tsv" ]; then
  echo "== delete =="
  while IFS=$'\t' read -r path sha; do
    path="${path%$'\r'}"
    sha="${sha%$'\r'}"
    encoded="$(urlencode_path "$path")"
    body="$(python3 -c 'import json,sys; print(json.dumps({"message": "Remove " + sys.argv[1], "sha": sys.argv[2], "branch": sys.argv[3]}))' "$path" "$sha" "$BRANCH")"
    printf '%s' "$body" > "$WORK/delete.json"
    if gh api --method DELETE "repos/$REPO/contents/$encoded" --input - <"$WORK/delete.json" >/dev/null 2>"$WORK/err.txt"; then
      echo "  delete $path"
    else
      echo "  FAILED $path"; cat "$WORK/err.txt"; exit 1
    fi
  done < "$WORK/plan/deletes.tsv"
fi

echo "== done: https://github.com/$REPO/tree/$BRANCH =="
