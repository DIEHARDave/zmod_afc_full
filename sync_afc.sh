#!/bin/sh
# Update the vendored AFC-Klipper-Add-On: ./sync_afc.sh <AFC-Klipper-Add-On clone> [commit]
set -e
SRC=$1
REV=${2:-HEAD}
DST=$(cd "$(dirname "$0")" && pwd)/AFC
[ -e "$SRC/.git" ] || { echo "usage: $0 <AFC-Klipper-Add-On clone> [commit]"; exit 1; }
COMMIT=$(git -C "$SRC" rev-parse "$REV")
rm -rf "$DST/extras"
git -C "$SRC" -c core.autocrlf=false archive "$COMMIT" extras LICENSE | tar xf - -C "$DST"
echo "$COMMIT" > "$DST/AFC_COMMIT"
echo "AFC $COMMIT"
