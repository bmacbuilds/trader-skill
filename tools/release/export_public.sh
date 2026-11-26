#!/usr/bin/env bash
# Builds dist/public/: exactly the files the public bmacbuilds/trader-skill repository holds, from this repository's
# tracked files. The development-only paths in PRIVATE never reach it, and storefront/ supplies what only the public
# repository has: its README, install.sh and the ChatGPT edition (chatgpt/). Prints the exported paths, sorted.
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT=dist/public
PRIVATE='^(README\.md$|handoff/|legal/|tank/|tools/tank/|tests/tank/|tests/export/|storefront/|tools/release/export_public\.sh$|tests/fixtures/timemachine/TSLA-60d5m\.json\.gz$)'
rm -rf "$OUT" && mkdir -p "$OUT"
git ls-files -z | grep -zvE "$PRIVATE" | xargs -0 tar -cf - | tar -xf - -C "$OUT"
cp -R storefront/. "$OUT/"
chmod +x "$OUT/install.sh"
find "$OUT" -type f | sed "s|^$OUT/||" | LC_ALL=C sort
