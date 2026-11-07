#!/bin/bash
# Idempotent. Run from the repo root:  sudo deploy/macos/install.sh [PIN]
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
PLIST=/Library/LaunchDaemons/com.faire.dps.plist
CONF=/usr/local/etc/dps; DATA=/usr/local/var/dps

# dedicated non-admin service user (hidden, no login)
if ! dscl . -read /Users/_dps >/dev/null 2>&1; then
  UID_NEW=$(( $(dscl . -list /Users UniqueID | awk '$2>=300 && $2<400 {print $2}' | sort -n | tail -1 || echo 299) + 1 ))
  [ "$UID_NEW" -lt 300 ] && UID_NEW=300
  dscl . -create /Users/_dps
  dscl . -create /Users/_dps UserShell /usr/bin/false
  dscl . -create /Users/_dps UniqueID "$UID_NEW"
  dscl . -create /Users/_dps PrimaryGroupID 20
  dscl . -create /Users/_dps NFSHomeDirectory /var/empty
  dscl . -create /Users/_dps IsHidden 1
fi

mkdir -p /opt/dps "$CONF" "$DATA"
PY=${PYTHON:-$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3)}
[ -d /opt/dps/venv ] || "$PY" -m venv /opt/dps/venv
/opt/dps/venv/bin/pip install --quiet --upgrade --force-reinstall --no-deps "$SRC"
ln -sf /opt/dps/venv/bin/dps /usr/local/bin/dps

[ -f "$CONF/config.json" ] || DPS_CONFIG="$CONF/config.json" /opt/dps/venv/bin/dps check-config >/dev/null
chown -R _dps:staff "$CONF" "$DATA"; chmod 700 "$CONF"; chmod 600 "$CONF/config.json"
export DPS_CONFIG="$CONF/config.json"
if [ -n "${1:-}" ]; then dps set-pin "$1"; fi

launchctl bootout system "$PLIST" 2>/dev/null || true
install -m 644 -o root -g wheel "$SRC/deploy/macos/com.faire.dps.plist" "$PLIST"
launchctl bootstrap system "$PLIST"
launchctl enable system/com.faire.dps
PORT=$(dps check-config | sed -n 's/.*port \([0-9]*\),.*/\1/p')
echo "Installed. UI: http://$(hostname):$PORT"
dps check-config | grep -q "PIN set" || echo "!! No PIN yet. Run: sudo DPS_CONFIG=$CONF/config.json dps set-pin"
