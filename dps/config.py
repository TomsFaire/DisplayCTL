"""Config load/save. Plain JSON, zero third-party deps (safe to run before login)."""
import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DRIVERS = ("samsung_mdc", "lg_signage")
DEFAULT_PORTS = {"samsung_mdc": 1515, "lg_signage": 9761}
_lock = threading.RLock()


def default_config():
    return {
        "site": {"name": "Office", "timezone": "America/New_York"},
        "web": {"bind": "0.0.0.0", "port": 8085, "allowed_cidrs": []},
        "automation": {"enabled": True},
        "network": {"source_ip": ""},   # local address used to reach displays ("" = OS default)
        "pin_hash": None,
        "session_secret": secrets.token_hex(32),
        "schedule": {d: {"enabled": d not in ("sat", "sun"), "on": "06:00", "off": "20:00"} for d in DAYS},
        "displays": [],
    }


def config_path(explicit=None):
    if explicit:
        return Path(explicit)
    if os.environ.get("DPS_CONFIG"):
        return Path(os.environ["DPS_CONFIG"])
    system = Path("/usr/local/etc/dps/config.json")
    if system.exists():
        return system
    return Path("config.json")


def valid_time(s):
    return isinstance(s, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", s) is not None


def validate(cfg):
    """Raise ValueError with a clear message if the config is bad."""
    from zoneinfo import ZoneInfo

    try:
        ZoneInfo(cfg["site"]["timezone"])
    except Exception:
        raise ValueError(f"bad timezone: {cfg['site'].get('timezone')!r}")
    src = cfg["network"].get("source_ip", "")
    if src:
        import ipaddress
        try:
            ipaddress.ip_address(src)
        except ValueError:
            raise ValueError(f"bad network.source_ip: {src!r}")
    port = cfg["web"]["port"]
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError(f"bad web port: {port!r}")
    for d in DAYS:
        s = cfg["schedule"].get(d)
        if not s or not valid_time(s.get("on")) or not valid_time(s.get("off")):
            raise ValueError(f"{d}: times must be HH:MM (24h)")
        if s.get("enabled") and s["off"] <= s["on"]:
            raise ValueError(f"{d}: off time must be after on time")
    seen = set()
    for disp in cfg["displays"]:
        if disp["id"] in seen:
            raise ValueError(f"duplicate display id {disp['id']}")
        seen.add(disp["id"])
        if disp["driver"] not in DRIVERS:
            raise ValueError(f"{disp['id']}: unknown driver {disp['driver']!r}")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", str(disp["ip"])):
            raise ValueError(f"{disp['id']}: bad IP/host")
    return cfg


def load(path):
    path = Path(path)
    with _lock:
        if not path.exists():
            cfg = default_config()
            save(path, cfg)
            return cfg
        cfg = json.loads(path.read_text())
    base = default_config()
    base["session_secret"] = cfg.get("session_secret") or base["session_secret"]
    for k in ("site", "web"):
        base[k].update(cfg.get(k, {}))
    base["schedule"].update(cfg.get("schedule", {}))
    base["automation"].update(cfg.get("automation", {}))
    base["network"].update(cfg.get("network", {}))
    base["pin_hash"] = cfg.get("pin_hash")
    base["displays"] = cfg.get("displays", [])
    return validate(base)


def save(path, cfg):
    path = Path(path)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".cfg-")
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(tmp, 0o600)  # holds the PIN hash + session secret
        try:  # `sudo dps set-pin` must not leave a root-owned file the daemon can't read
            st = path.stat()
            os.chown(tmp, st.st_uid, st.st_gid)
        except (OSError, AttributeError):
            pass
        os.replace(tmp, path)


def new_display_id(cfg, name):
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "display"
    ids = {d["id"] for d in cfg["displays"]}
    cand, n = base, 2
    while cand in ids:
        cand, n = f"{base}-{n}", n + 1
    return cand


def make_display(cfg, data):
    """Build + sanity check a display dict from user input."""
    driver = data.get("driver", "samsung_mdc")
    name = str(data.get("name", "")).strip()
    if not name:
        raise ValueError("room name required")
    ip = str(data.get("ip", "")).strip()
    if not ip:
        raise ValueError("IP required")
    d = {
        "id": data.get("id") or new_display_id(cfg, name),
        "name": name,
        "ip": ip,
        "driver": driver,
        "port": int(data.get("port") or DEFAULT_PORTS.get(driver, 0)),
        "display_id": int(data.get("display_id", 1 if driver == "lg_signage" else 0)),
        "mac": (data.get("mac") or "").strip(),
        "enabled": bool(data.get("enabled", True)),
    }
    return d


# --- PIN handling -------------------------------------------------------------
def hash_pin(pin):
    if not re.fullmatch(r"\d{4,12}", pin):
        raise ValueError("PIN must be 4-12 digits")
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pin.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_pin(pin, stored):
    if not stored:
        return False
    try:
        _, salt, h = stored.split("$")
        got = hashlib.scrypt(pin.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1)
        return hmac.compare_digest(got.hex(), h)
    except Exception:
        return False
