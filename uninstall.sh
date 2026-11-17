#!/bin/sh
# Zmod AFC Full: run by DISABLE_PLUGIN name=zmod_afc_full (Klipper restarts after it).
# Removes this plugin's links from Klipper's extras and the generated AFC config.
# AFC's saved lane state (mod_data/AFC/AFC.var*) stays for a later reinstall.
[ -f /usr/data/zmod/zmod/.shell/0.sh ] && . /usr/data/zmod/zmod/.shell/0.sh

set -u

PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
MOD_DATA=$(CDPATH= cd -- "$PLUGIN_DIR/../.." && pwd -P)

for EXTRAS_DIR in \
    /usr/prog/klipper/klippy/extras \
    /usr/data/zmod/klipper/klippy/extras \
    ${KLIPPER_DIR:+"$KLIPPER_DIR/klippy/extras"}
do
    [ -d "$EXTRAS_DIR" ] || continue
    for f in "$EXTRAS_DIR"/*.py; do
        [ -L "$f" ] || continue
        case "$(readlink "$f")" in
            "$PLUGIN_DIR"/*|*/plugins/zmod_afc_full/*) rm -f "$f" ;;
        esac
    done
    echo "Zmod AFC Full links removed from $EXTRAS_DIR"
done

rm -f "$MOD_DATA/AFC/zmod_afc_full_active.cfg"
echo "Zmod AFC Full removed; Zmod's built-in AFC panel is back after the restart."
