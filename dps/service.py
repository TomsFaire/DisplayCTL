"""Scheduler + runtime state. Reconciling: intended state is computed from the schedule;
commands are sent only on transitions and once at startup."""
import collections
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import config as cfgmod
from . import drivers

log = logging.getLogger("dps")
POLL_EVERY_S = 60
TICK_S = 15


def _at(day, hhmm, tz):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=tz)


def intended(cfg, now):
    """True if displays should be ON at `now` (tz-aware)."""
    s = cfg["schedule"][cfgmod.DAYS[now.weekday()]]
    if not s["enabled"]:
        return False
    tz = now.tzinfo
    return _at(now.date(), s["on"], tz) <= now < _at(now.date(), s["off"], tz)


def next_change(cfg, now):
    """(datetime, 'ON'|'OFF') of the next scheduled transition, or None."""
    tz = now.tzinfo
    cur = intended(cfg, now)
    best = None
    for off in range(0, 9):
        day = now.date() + timedelta(days=off)
        s = cfg["schedule"][cfgmod.DAYS[day.weekday()]]
        if not s["enabled"]:
            continue
        for t, state in ((s["on"], "ON"), (s["off"], "OFF")):
            dt = _at(day, t, tz)
            if dt > now and (state == "ON") != cur and (best is None or dt < best[0]):
                best = (dt, state)
        if best:
            return best
    return None


class Service:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.cfg = cfgmod.load(path)
        self._mtime = path.stat().st_mtime
        self.state = {}
        self.last_intended = {}
        self.attempts = collections.defaultdict(int)
        self.events = collections.deque(maxlen=300)
        self.last_tick = None
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=8)
        self._last_poll = 0

    def now(self):
        return datetime.now(ZoneInfo(self.cfg["site"]["timezone"]))

    def event(self, actor, disp, action, result, error=""):
        self.events.appendleft({"t": self.now().strftime("%Y-%m-%d %H:%M:%S"), "actor": actor,
                                "display": disp, "action": action, "result": result, "error": error})
        log.info("%s | %s | %s -> %s %s", actor, disp, action, result, error)

    def maybe_reload(self):
        try:
            m = self.path.stat().st_mtime
            if m != self._mtime:
                new = cfgmod.load(self.path)
                with self.lock:
                    self.cfg, self._mtime = new, m
                log.info("config reloaded")
        except Exception as e:  # bad edit: keep running on the last good config
            log.error("config reload failed, keeping previous: %s", e)

    def mutate(self, fn):
        """Apply fn(cfg) to a fresh copy, validate, save. Raises ValueError on bad input."""
        with self.lock:
            new = cfgmod.load(self.path)
            fn(new)
            cfgmod.validate(new)
            cfgmod.save(self.path, new)
            self.cfg, self._mtime = new, self.path.stat().st_mtime
            # a schedule edit may change what's intended right now: reconcile next tick
            self.last_intended.clear()
            self.attempts.clear()

    def display(self, did):
        for d in self.cfg["displays"]:
            if d["id"] == did:
                return d

    def _st(self, did):
        return self.state.setdefault(did, {"power": "UNKNOWN", "status": "UNKNOWN", "last_action": "",
                                           "last_time": "", "error": "", "reachable": None})

    def apply(self, d, on, actor):
        st = self._st(d["id"])
        act = "ON" if on else "OFF"
        r = drivers.set_power(d, on, timeout=5)
        st.update(last_action=act, last_time=self.now().strftime("%H:%M:%S"), error=r.error,
                  reachable=r.ok)
        if r.ok:
            st["power"] = r.power
            st["status"] = "OK" if r.verified else "UNVERIFIED"
        else:
            st["status"] = "FAILED"
        self.event(actor, d["name"], act, st["status"], r.error)
        return r

    def poll(self, d):
        st = self._st(d["id"])
        r = drivers.get_power(d, timeout=4)
        st["reachable"] = r.ok
        if r.ok:
            st.update(power=r.power, error="")
            if st["status"] == "FAILED":
                st["status"] = "OK"
        else:
            st.update(power="UNKNOWN", error=r.error)
        return r

    def manual(self, did, on, actor="user"):
        d = self.display(did)
        if not d:
            raise KeyError(did)
        return self.apply(d, on, actor)

    def tick(self):
        self.maybe_reload()
        now = self.now()
        want = intended(self.cfg, now)
        todo = []
        if not self.cfg["automation"]["enabled"]:
            self.last_intended.clear()  # on re-enable, reconcile to the current intended state
            todo_disabled = True
        else:
            todo_disabled = False
        for d in ([] if todo_disabled else self.cfg["displays"]):
            did = d["id"]
            if not d.get("enabled", True):
                continue
            if self.last_intended.get(did) != want:
                self.last_intended[did] = want
                self.attempts[did] = 0
                todo.append(d)
            elif self.state.get(did, {}).get("status") == "FAILED" and self.attempts[did] < 3:
                todo.append(d)
        for d in todo:
            self.attempts[d["id"]] += 1
            self.pool.submit(self.apply, d, want, "scheduler")
        if time.time() - self._last_poll > POLL_EVERY_S:
            self._last_poll = time.time()
            for d in self.cfg["displays"]:
                self.pool.submit(self.poll, d)
        self.last_tick = now.isoformat(timespec="seconds")

    def run_forever(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            self.stop.wait(TICK_S)

    def snapshot(self):
        now = self.now()
        nc = next_change(self.cfg, now)
        want = "ON" if intended(self.cfg, now) else "OFF"
        rows = [{**d, **self._st(d["id"]), "intended": want if d.get("enabled", True) else "—"}
                for d in self.cfg["displays"]]
        return {"site": self.cfg["site"], "automation": self.cfg["automation"]["enabled"], "now": now.strftime("%a %H:%M"), "schedule": self.cfg["schedule"],
                "next_change": nc and {"at": nc[0].strftime("%a %H:%M"), "state": nc[1]},
                "displays": rows, "events": list(self.events)[:50], "last_tick": self.last_tick}
