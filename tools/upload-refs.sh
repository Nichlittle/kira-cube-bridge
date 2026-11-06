#!/bin/bash
# upload-refs.sh: one-time upload of Kira's two reference images to xAI's Files API.
# Prints the two file ids; paste them into Render as XAI_REF_FILES (comma-separated).
#
# Put ref1.png and ref2.png (or .jpg/.webp) next to this script, then:
#   bash upload-refs.sh
# Use the Render key (it needs the Files permission); the image-only test key
# will be refused. The key is typed at a hidden prompt and never saved or shown.
# File ids are not secrets.

set -u
cd "$(dirname "$0")" || exit 1

find_img() {
  for ext in png jpg jpeg webp; do [ -f "$1.$ext" ] && { echo "$1.$ext"; return; }; done
}
REF1=$(find_img ref1); REF2=$(find_img ref2)
[ -n "$REF1" ] && [ -n "$REF2" ] || { echo "Put ref1 and ref2 (.png/.jpg/.webp) next to this script."; exit 1; }

printf "Paste the Render API key (hidden), then Enter: "
read -rs XAI_API_KEY; echo
[ -n "$XAI_API_KEY" ] || { echo "No key entered."; exit 1; }

IDS=""
for f in "$REF1" "$REF2"; do
  OUT=$(printf 'header = "Authorization: Bearer %s"\n' "$XAI_API_KEY" | \
        curl -sS --config - --max-time 120 -F "file=@$f" https://api.x.ai/v1/files)
  ID=$(printf '%s' "$OUT" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  if [ -z "$ID" ]; then
    echo "Upload of $f failed. xAI said:"; echo "$OUT"; unset XAI_API_KEY; exit 1
  fi
  echo "$f -> $ID"
  IDS="${IDS:+$IDS,}$ID"
done
unset XAI_API_KEY
echo
echo "Set this on Render (Environment):"
echo "  XAI_REF_FILES=$IDS"
