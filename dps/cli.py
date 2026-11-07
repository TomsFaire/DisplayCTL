"""dps command line. Config-editing commands work on the file directly; the daemon hot-reloads."""
import argparse
import getpass
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path

from . import __version__
from . import config as cfgmod
from . import drivers


def _load(args):
    p = cfgmod.config_path(args.config)
    cfg = cfgmod.load(p)
    drivers.SOURCE_IP = cfg["network"]["source_ip"]
    return p, cfg


def _find(cfg, key):
    k = key.lower()
    for d in cfg["displays"]:
        if k in (d["id"].lower(), d["name"].lower(), d["ip"]):
            return d
    sys.exit(f"no display matching {key!r} (try `dps list`)")


def _targets(cfg, key):
    return cfg["displays"] if key == "all" else [_find(cfg, key)]


def cmd_run(a):
    from .service import Service
    from .web import serve

    path, _ = _load(a)
    logdir = Path(os.environ.get("DPS_LOG_DIR", path.parent))
    handlers = [logging.StreamHandler()]
    try:
        logdir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(logdir / "dps.log", maxBytes=2_000_000, backupCount=3))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, handlers=handlers, format="%(asctime)s %(levelname)s %(message)s")
    svc = Service(path)
    if not svc.cfg["pin_hash"]:
        logging.warning("No PIN set - nobody can log in. Run: sudo dps set-pin")
    port = a.port or (int(os.environ["DPS_PORT"]) if os.environ.get("DPS_PORT") else None)
    srv = serve(svc, a.bind, port)
    threading.Thread(target=svc.run_forever, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def cmd_check(a):
    p, cfg = _load(a)
    print(f"OK: {p}  ({len(cfg['displays'])} displays, port {cfg['web']['port']}, "
          f"PIN {'set' if cfg['pin_hash'] else 'NOT SET'})")


def cmd_set_pin(a):
    p, cfg = _load(a)
    pin = a.pin or getpass.getpass("New PIN (4-12 digits): ")
    if not a.pin and getpass.getpass("Repeat PIN: ") != pin:
        sys.exit("PINs do not match")
    try:
        cfg["pin_hash"] = cfgmod.hash_pin(pin)
    except ValueError as e:
        sys.exit(str(e))
    cfgmod.save(p, cfg)
    print("PIN updated (takes effect immediately).")


def cmd_set_port(a):
    p, cfg = _load(a)
    if a.port == "auto":
        cur = cfg["web"]["port"]
        port = cfgmod.find_free_port(a.start, own=cur)
        if port == a.start or port == cur:
            print(f"Using web port {port}.")
        else:
            print(f"Port {a.start} is in use by another program; using free port {port}.")
        a.port = port
    else:
        try:
            a.port = int(a.port)
        except ValueError:
            sys.exit("port must be a number or 'auto'")
    cfg["web"]["port"] = a.port
    if a.bind:
        cfg["web"]["bind"] = a.bind
    cfgmod.validate(cfg)
    cfgmod.save(p, cfg)
    print(f"Web port set to {a.port}. Restart the service to apply (macOS: sudo launchctl kickstart -k system/com.faire.dps; Linux: sudo systemctl restart dps).")


def cmd_timezone(a):
    p, cfg = _load(a)
    if a.zone is None:
        print(f"timezone: {cfg['site']['timezone']}  (system: {cfgmod.system_timezone()})")
        return
    cfg["site"]["timezone"] = cfgmod.system_timezone() if a.zone == "auto" else a.zone
    try:
        cfgmod.validate(cfg)
    except ValueError as e:
        sys.exit(str(e))
    cfgmod.save(p, cfg)
    print(f"Timezone set to {cfg['site']['timezone']} (applies on the next config reload, no restart needed).")


def cmd_source_ip(a):
    p, cfg = _load(a)
    if a.ip is not None:
        cfg["network"]["source_ip"] = "" if a.ip == "auto" else a.ip
        try:
            cfgmod.validate(cfg)
        except ValueError as e:
            sys.exit(str(e))
        cfgmod.save(p, cfg)
    print("source ip:", cfg["network"]["source_ip"] or "(OS default)")


def cmd_list(a):
    _, cfg = _load(a)
    for d in cfg["displays"]:
        print(f"{d['id']:20} {d['name']:24} {d['ip']}:{d['port']:<5} {d['driver']}")
    if not cfg["displays"]:
        print("(no displays; add with `dps add`)")


def cmd_add(a):
    p, cfg = _load(a)
    data = {"name": a.name, "ip": a.ip, "driver": a.driver, "port": a.port, "mac": a.mac}
    if a.display_id is not None:
        data["display_id"] = a.display_id
    d = cfgmod.make_display(cfg, data)
    cfg["displays"].append(d)
    cfgmod.validate(cfg)
    cfgmod.save(p, cfg)
    print(f"added {d['id']}")


def cmd_remove(a):
    p, cfg = _load(a)
    d = _find(cfg, a.display)
    cfg["displays"].remove(d)
    cfgmod.save(p, cfg)
    print(f"removed {d['id']}")


def cmd_status(a):
    _, cfg = _load(a)
    for d in _targets(cfg, a.display):
        r = drivers.get_power(d)
        print(f"{d['name']:24} {d['ip']:16} {r.power if r.ok else 'UNREACHABLE: ' + r.error}")


def cmd_power(a):
    _, cfg = _load(a)
    rc = 0
    for d in _targets(cfg, a.display):
        r = drivers.set_power(d, a.state == "on")
        flag = "verified" if r.verified else ("sent, unverified" if r.ok else "FAILED: " + r.error)
        print(f"{d['name']:24} {a.state.upper():4} {flag}")
        rc |= not r.ok
    return rc


def cmd_schedule(a):
    p, cfg = _load(a)
    if a.set:
        days = cfgmod.DAYS if a.days == "all" else (cfgmod.DAYS[:5] if a.days == "weekdays" else
               cfgmod.DAYS[5:] if a.days == "weekend" else a.days.split(","))
        for d in days:
            if d not in cfgmod.DAYS:
                sys.exit(f"bad day {d!r}")
            cfg["schedule"][d] = ({"enabled": False, "on": cfg["schedule"][d]["on"], "off": cfg["schedule"][d]["off"]}
                                  if a.set == "off-all" else
                                  {"enabled": True, "on": a.on, "off": a.off})
        try:
            cfgmod.validate(cfg)
        except ValueError as e:
            sys.exit(str(e))
        cfgmod.save(p, cfg)
    for d in cfgmod.DAYS:
        s = cfg["schedule"][d]
        print(f"{d}  {s['on']} - {s['off']}" if s["enabled"] else f"{d}  (off all day)")


def cmd_automation(a):
    p, cfg = _load(a)
    if a.state:
        cfg["automation"]["enabled"] = a.state == "on"
        cfgmod.save(p, cfg)
    print("automation:", "ENABLED" if cfg["automation"]["enabled"] else "PAUSED")


def cmd_probe(a):
    d = {"driver": a.driver, "ip": a.host, "port": a.port or cfgmod.DEFAULT_PORTS[a.driver],
         "display_id": a.id, "mac": ""}
    r = drivers.get_power(d)
    print(f"ok={r.ok} power={r.power} {r.latency_ms}ms err={r.error}\nTX {r.tx.hex(' ')}\nRX {r.rx.hex(' ')}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="dps", description="Display power scheduler")
    ap.add_argument("--config", help="config file (default: $DPS_CONFIG, /usr/local/etc/dps/config.json, ./config.json)")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run", help="run scheduler + web UI"); s.add_argument("--port", type=int); s.add_argument("--bind"); s.set_defaults(f=cmd_run)
    sub.add_parser("check-config", help="validate config").set_defaults(f=cmd_check)
    s = sub.add_parser("set-pin", help="set the web UI PIN"); s.add_argument("pin", nargs="?"); s.set_defaults(f=cmd_set_pin)
    s = sub.add_parser("set-port", help="set web UI port ('auto' = first free port from --start, default 8000)"); s.add_argument("port", help="number or 'auto'"); s.add_argument("--start", type=int, default=8000); s.add_argument("--bind"); s.set_defaults(f=cmd_set_port)
    s = sub.add_parser("set-timezone", help="show/set the schedule timezone ('auto' = follow this machine)"); s.add_argument("zone", nargs="?", help="IANA name e.g. America/Los_Angeles, or 'auto'"); s.set_defaults(f=cmd_timezone)
    s = sub.add_parser("source-ip", help="show/set local IP used to reach displays (or 'auto')"); s.add_argument("ip", nargs="?"); s.set_defaults(f=cmd_source_ip)
    sub.add_parser("list", help="list displays").set_defaults(f=cmd_list)
    s = sub.add_parser("add", help="add a display"); s.add_argument("name"); s.add_argument("ip")
    s.add_argument("--driver", choices=cfgmod.DRIVERS, default="samsung_mdc"); s.add_argument("--port", type=int)
    s.add_argument("--display-id", type=int); s.add_argument("--mac", default=""); s.set_defaults(f=cmd_add)
    s = sub.add_parser("remove", help="remove a display"); s.add_argument("display"); s.set_defaults(f=cmd_remove)
    s = sub.add_parser("status", help="query power state"); s.add_argument("display", nargs="?", default="all"); s.set_defaults(f=cmd_status)
    s = sub.add_parser("power", help="power on|off"); s.add_argument("state", choices=["on", "off"]); s.add_argument("display", help="id, room name, IP, or 'all'"); s.set_defaults(f=cmd_power)
    s = sub.add_parser("schedule", help="show/set weekly schedule")
    s.add_argument("--days", default="weekdays", help="all|weekdays|weekend|mon,tue,..."); s.add_argument("--on", default="06:00"); s.add_argument("--off", default="20:00")
    s.add_argument("--set", nargs="?", const="hours", choices=["hours", "off-all"], help="apply hours (or disable) for --days"); s.set_defaults(f=cmd_schedule)
    s = sub.add_parser("automation", help="show/pause/resume all scheduled actions"); s.add_argument("state", nargs="?", choices=["on", "off"]); s.set_defaults(f=cmd_automation)
    s = sub.add_parser("probe", help="ad-hoc test of a display"); s.add_argument("--driver", choices=cfgmod.DRIVERS, required=True)
    s.add_argument("--host", required=True); s.add_argument("--port", type=int); s.add_argument("--id", type=int, default=0); s.set_defaults(f=cmd_probe)
    a = ap.parse_args(argv)
    return a.f(a) or 0
