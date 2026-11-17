#!/bin/sh
# Zmod AFC Full: run by ENABLE_PLUGIN name=zmod_afc_full (Klipper restarts after it).
# Links zmod_afc_full.py into Klipper's extras; zmod_afc_full.py links the AFC
# modules and writes the AFC config at every Klipper start.
[ -f /usr/data/zmod/zmod/.shell/0.sh ] && . /usr/data/zmod/zmod/.shell/0.sh

set -eu

PLUGIN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
MODULE=zmod_afc_full.py
SOURCE="$PLUGIN_DIR/$MODULE"
MOD_DATA=$(dirname "$PLUGIN_DIR")/..
MOD_DATA=$(CDPATH= cd -- "$MOD_DATA" && pwd -P)

if [ -f /usr/data/zmod/zmod/.shell/AFC_IFS.py ]; then
    echo "Zmod is on the full-AFC test branch (feat/full-afc). Switch it back to" >&2
    echo "ghzserg's 1.7 branch first, see the plugin README." >&2
    exit 1
fi

# The AD5X can have both the native Klipper and Zmod's Klipper 13 installed, so
# link into every extras directory rather than guessing which one is running.
linked=0
for EXTRAS_DIR in \
    /usr/prog/klipper/klippy/extras \
    /usr/data/zmod/klipper/klippy/extras \
    ${KLIPPER_DIR:+"$KLIPPER_DIR/klippy/extras"}
do
    [ -d "$EXTRAS_DIR" ] || continue
    TARGET="$EXTRAS_DIR/$MODULE"
    # A dangling link (e.g. to a path that only exists inside Moonraker's
    # chroot) cannot be a working module, so it is safe to replace.
    if [ -L "$TARGET" ] && [ ! -e "$TARGET" ]; then
        rm "$TARGET"
    fi
    if [ -L "$TARGET" ]; then
        if [ "$(readlink -f "$TARGET")" != "$SOURCE" ]; then
            echo "Refusing to replace another symlink: $TARGET -> $(readlink "$TARGET")" >&2
            exit 1
        fi
    elif [ -e "$TARGET" ]; then
        echo "Refusing to replace an existing Klipper module: $TARGET" >&2
        exit 1
    else
        ln -s "$SOURCE" "$TARGET"
    fi
    linked=$((linked + 1))
    echo "Zmod AFC Full linked in $EXTRAS_DIR"
done

if [ "$linked" -eq 0 ]; then
    echo "Could not find a supported Zmod Klipper extras directory" >&2
    exit 1
fi

mkdir -p "$MOD_DATA/AFC"

# zmod_afcplus extends Zmod's AFC emulation, which this plugin turns off
if grep -q '^\[include plugins/zmod_afcplus/' "$MOD_DATA/plugins.cfg" 2>/dev/null; then
    sed -i '/^\[include plugins\/zmod_afcplus\//d' "$MOD_DATA/plugins.cfg"
    echo "zmod_afcplus turned off (ENABLE_PLUGIN name=zmod_afcplus after removing this plugin)"
fi

if ! grep -q '^display_off = 1' "$MOD_DATA/variables.cfg" 2>/dev/null; then
    echo "Note: full AFC stays off while the native screen is on (run DISPLAY_OFF)."
fi

echo "Zmod AFC Full installed. Klipper restarts twice: once to load the plugin,"
echo "once to load AFC."
