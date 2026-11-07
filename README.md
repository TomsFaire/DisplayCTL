# DisplayCTL (dps) — display power scheduler, MVP

Turns conference-room displays (Samsung MDC, LG signage) on/off on a weekly schedule, with a web UI and an SSH-friendly CLI.
Standard library only — no third-party runtime dependencies.

## Install (macOS, starts at boot before login)
```
sudo deploy/macos/install.sh 1234        # 1234 = the UI PIN (omit to set later)
```
Creates a hidden `_dps` user, installs to `/opt/dps`, config in `/usr/local/etc/dps/config.json`, logs in `/usr/local/var/dps/`,
and registers a LaunchDaemon (`com.faire.dps`) with RunAtLoad + KeepAlive. Linux: `sudo deploy/linux/install.sh 1234`.

Open `http://<mac-hostname>:8085`, enter the PIN.

## Web UI
* **Displays** — room, IP, power state, status, last action; On / Off / Test (shows raw TX/RX bytes) / Edit / remove; All On / All Off.
* **Add display** — room name + IP (+ driver, port, Display/Set ID, MAC for LG Wake-on-LAN).
* **Weekly schedule** — enable/disable and on/off time for each day of the week.
* **Activity** — recent scheduler and manual actions.

## CLI (over SSH). Use `sudo` so it can edit the service config; set `DPS_CONFIG=/usr/local/etc/dps/config.json` or `--config`.
```
dps set-pin [PIN]                 # change UI PIN (live immediately)
dps set-port 8090 [--bind ADDR]   # then: sudo launchctl kickstart -k system/com.faire.dps
dps add "Hudson 4A" 10.20.70.31 [--driver lg_signage --display-id 1 --mac AA:BB:..]
dps list | remove "Hudson 4A"
dps status [all|NAME|ID|IP]
dps power on|off NAME|all
dps schedule                      # show
dps schedule --set --days weekdays --on 06:00 --off 20:00
dps schedule --set off-all --days sat,sun
dps probe --driver samsung_mdc --host 10.20.70.31 --id 0    # ad-hoc test, no config
dps check-config
```
Port can also be set by `dps run --port N`, `DPS_PORT`, or `web.port` in the config. Config edits (file or UI) hot-reload; port/bind changes need a restart.

## How scheduling works
Every 15 s the service computes whether displays *should* be on (per-day window, site timezone) and sends explicit ON/OFF only when that
changes — and once at startup, so a 07:10 reboot still turns displays ON. Failed commands are retried (bounded) and shown as FAILED.
Manual On/Off holds until the next scheduled change.

## Commissioning checklist (every item is `# VERIFY`)
- Samsung: enable **Network Standby** so IP power-on works. Newer firmware may require secured MDC (TLS+PIN) — **not implemented in MVP**.
- Samsung Display ID (often 0 or 1); LG Set ID (usually 1). LG TCP port 9761 assumed; many LG panels need Wake-on-LAN to power on (enter MAC).
- Use **Test** in the UI / `dps probe` on each model before relying on the schedule.
- macOS: allow incoming connections for the Python binary if the Application Firewall prompts; confirm outbound access to displays after a reboot with nobody logged in; keep NTP on.
- Global Caché serial adapters and per-day exceptions/holidays are **not in the MVP**.

## Security notes
PIN login (scrypt-hashed, 5 attempts/min/IP lockout), signed HttpOnly session cookie, CSRF header on all writes, optional `web.allowed_cidrs`.
Plain HTTP — put it behind TLS/Caddy if the network isn't trusted. `/healthz` is unauthenticated and returns no secrets.

## Dependencies / updates
Runtime has none. Dev deps and GitHub Actions are kept current by Dependabot (`.github/dependabot.yml`).
