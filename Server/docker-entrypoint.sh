#!/bin/sh
# Seed the mounted volume with the default menu on first boot so the operator
# has something to edit. Existing menus are never overwritten.
set -e

mkdir -p "${DATA_DIR:-/data}"
if [ ! -f "${DATA_DIR:-/data}/menu.csv" ]; then
    echo "[entrypoint] seeding ${DATA_DIR:-/data}/menu.csv"
    cp /seed/menu.csv "${DATA_DIR:-/data}/menu.csv"
fi

exec "$@"
