#!/bin/bash
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
id dps >/dev/null 2>&1 || useradd --system --home /var/lib/dps --shell /usr/sbin/nologin dps
mkdir -p /opt/dps /etc/dps /var/lib/dps
[ -d /opt/dps/venv ] || python3 -m venv /opt/dps/venv
/opt/dps/venv/bin/pip install --quiet --upgrade "$SRC"
ln -sf /opt/dps/venv/bin/dps /usr/local/bin/dps
DPS_CONFIG=/etc/dps/config.json dps check-config >/dev/null
chown -R dps:dps /etc/dps /var/lib/dps; chmod 600 /etc/dps/config.json
[ -z "${1:-}" ] || DPS_CONFIG=/etc/dps/config.json dps set-pin "$1"
install -m 644 "$SRC/deploy/linux/dps.service" /etc/systemd/system/dps.service
systemctl daemon-reload && systemctl enable --now dps
