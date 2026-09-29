#!/bin/zsh
# NUT network server that fronts this driver's dummy-ups replay file.
#
# NUT's usbhid-ups driver cannot open a HID device the kernel already holds
# open on some platforms (notably macOS), so upsd.py (this repo's daemon)
# acts as the driver itself: it writes a dummy-ups .dev file (set nut_dev_path
# in config.json) and NUT's own dummy-ups driver replays it; NUT's upsd then
# serves it on the standard network port (3493) to upsmon clients elsewhere
# on the network. This script starts that pair.
#
# Requires NUT installed (e.g. `brew install nut`) and a dummy-ups device
# configured in ups.conf pointing at the .dev file upsd.py writes.
set -e
export NUT_QUIET_INIT_UPSNOTIFY=true

NUT_PREFIX="${NUT_PREFIX:-/opt/homebrew}"
NUT_STATE_DIR="${NUT_STATE_DIR:-$NUT_PREFIX/var/state/ups}"

mkdir -p "$NUT_STATE_DIR"
"$NUT_PREFIX/sbin/upsdrvctl" stop >/dev/null 2>&1 || true
"$NUT_PREFIX/sbin/upsdrvctl" start || exit 1
exec "$NUT_PREFIX/sbin/upsd" -D
