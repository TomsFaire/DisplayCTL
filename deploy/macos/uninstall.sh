#!/bin/bash
# Idempotent. Keeps config/data unless --purge.
set -u
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
launchctl bootout system /Library/LaunchDaemons/com.faire.dps.plist 2>/dev/null
rm -f /Library/LaunchDaemons/com.faire.dps.plist /usr/local/bin/dps
rm -rf /opt/dps
if [ "${1:-}" = "--purge" ]; then rm -rf /usr/local/etc/dps /usr/local/var/dps; dscl . -delete /Users/_dps 2>/dev/null; fi
echo "Uninstalled."
