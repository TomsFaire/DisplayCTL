#!/bin/bash
# Idempotent. Puts Caddy (self-signed internal CA) in front of the dps UI on :443.
# Prereqs: sudo deploy/macos/install.sh already run;  brew install caddy  (as your normal user).
# Usage:   sudo deploy/macos/install-https.sh [IP]        (default: en0 address)
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
SRC="$(cd "$(dirname "$0")" && pwd)"
CONF=/usr/local/etc/caddy; DATA=/usr/local/var/caddy
PLIST=/Library/LaunchDaemons/com.faire.dps-caddy.plist
export DPS_CONFIG=/usr/local/etc/dps/config.json
CADDY=$(command -v caddy || ls /opt/homebrew/bin/caddy /usr/local/bin/caddy 2>/dev/null | head -1 || true)
[ -n "$CADDY" ] || { echo "caddy not found. As your normal user: brew install caddy"; exit 1; }
CADDY=$(cd "$(dirname "$CADDY")" && pwd -P)/$(basename "$CADDY")   # resolve for the plist
dscl . -read /Users/_dps >/dev/null 2>&1 || { echo "run deploy/macos/install.sh first"; exit 1; }
IP=${1:-$(ipconfig getifaddr en0 || true)}
[ -n "$IP" ] || { echo "could not detect IP; pass it as an argument"; exit 1; }
PORT=$(/usr/local/bin/dps check-config | sed -n 's/.*port \([0-9]*\),.*/\1/p')

mkdir -p "$CONF" "$DATA"
sed -e "s|__IP__|$IP|g" -e "s|__PORT__|$PORT|g" "$SRC/Caddyfile.tmpl" > "$CONF/Caddyfile"
"$CADDY" validate --config "$CONF/Caddyfile" --adapter caddyfile >/dev/null
chown -R _dps:staff "$DATA"; chmod 700 "$DATA"

launchctl bootout system "$PLIST" 2>/dev/null || true
sed "s|__CADDY__|$CADDY|" "$SRC/com.faire.dps-caddy.plist" > "$PLIST"
chown root:wheel "$PLIST"; chmod 644 "$PLIST"
launchctl bootstrap system "$PLIST"; launchctl enable system/com.faire.dps-caddy

for i in $(seq 1 20); do curl -ksf -m 3 "https://localhost/healthz" >/dev/null && ok=1 && break; sleep 1; done
[ "${ok:-}" = 1 ] || { echo "!! HTTPS check failed; see $DATA/caddy.log"; exit 1; }
echo "HTTPS OK: https://$IP/"

# Only after HTTPS works: make the plain-HTTP listener loopback-only.
/usr/local/bin/dps set-port "$PORT" --bind 127.0.0.1 >/dev/null
chown _dps:staff "$DPS_CONFIG"
launchctl kickstart -k system/com.faire.dps
echo "dps now bound to 127.0.0.1:$PORT (plain HTTP no longer reachable from the network)."
echo "Distribute this root cert to clients via MDM:  $DATA/pki/authorities/local/root.crt"
