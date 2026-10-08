#!/usr/bin/env sh
# Usage: ./set-domain.sh example.com   (no scheme, no trailing slash)
set -eu
[ $# -eq 1 ] || { echo "usage: $0 <domain>"; exit 1; }
OLD="https://prime-bot-site.pages.dev"
NEW="https://$1"
cd "$(dirname "$0")"
grep -rlF "$OLD" --include='*.html' --include='*.xml' --include='*.txt' --include='*.webmanifest' . | while read -r f; do
  sed "s#$OLD#$NEW#g" "$f" > "$f.tmp" && mv "$f.tmp" "$f"
done
echo "Rewrote $OLD -> $NEW"
