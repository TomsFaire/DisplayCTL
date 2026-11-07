"""Stdlib HTTP server: JSON API + single-page UI. PIN login, signed cookie sessions."""
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import config as cfgmod
from . import drivers

log = logging.getLogger("dps.web")
SESSION_S = 12 * 3600
_fails = {}  # ip -> (count, first_ts)


def make_token(secret, ttl=SESSION_S):
    exp = str(int(time.time()) + ttl)
    sig = hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_token(secret, tok):
    try:
        exp, sig = tok.split(".")
        good = hmac.new(secret.encode(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except Exception:
        return False


def make_handler(svc):
    page = resources.files("dps").joinpath("ui.html").read_bytes()

    class H(BaseHTTPRequestHandler):
        server_version = "dps"

        def log_message(self, fmt, *a):
            log.debug(fmt, *a)

        # -- plumbing
        def _send(self, code, body=b"", ctype="application/json", headers=()):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 65536:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(n) or b"{}")

        def _allowed(self):
            cidrs = svc.cfg["web"].get("allowed_cidrs") or []
            if not cidrs:
                return True
            ip = ipaddress.ip_address(self.client_address[0])
            return any(ip in ipaddress.ip_network(c, strict=False) for c in cidrs)

        def _authed(self):
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return "dps" in c and check_token(svc.cfg["session_secret"], c["dps"].value)

        def _dispatch(self, method):
            try:
                if not self._allowed():
                    return self._send(403, {"error": "forbidden"})
                path = self.path.split("?")[0]
                if method == "GET" and path == "/":
                    return self._send(200, page, "text/html; charset=utf-8")
                if method == "GET" and path == "/healthz":
                    return self._send(200, {"ok": True, "last_tick": svc.last_tick})
                if method == "POST" and path == "/api/login":
                    return self._login()
                if not self._authed():
                    return self._send(401, {"error": "auth required"})
                if method == "POST" and self.headers.get("X-DPS") != "1":  # CSRF guard
                    return self._send(403, {"error": "bad request"})
                return self._api(method, path)
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                self._send(400, {"error": str(e)})
            except Exception:
                log.exception("request failed")
                self._send(500, {"error": "internal error"})

        do_GET = lambda self: self._dispatch("GET")
        do_POST = lambda self: self._dispatch("POST")

        # -- routes
        def _login(self):
            ip = self.client_address[0]
            n, t0 = _fails.get(ip, (0, 0))
            if n >= 5 and time.time() - t0 < 60:
                return self._send(429, {"error": "too many attempts, wait a minute"})
            if n >= 5:
                n = 0
            pin = str(self._json().get("pin", ""))
            if cfgmod.check_pin(pin, svc.cfg["pin_hash"]):
                _fails.pop(ip, None)
                cookie = f"dps={make_token(svc.cfg['session_secret'])}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_S}"
                return self._send(200, {"ok": True}, headers=[("Set-Cookie", cookie)])
            _fails[ip] = (n + 1, t0 if n else time.time())
            time.sleep(0.5)
            return self._send(401, {"error": "wrong PIN" if svc.cfg["pin_hash"] else "no PIN set: run `dps set-pin` on the host"})

        def _api(self, method, path):
            if method == "GET" and path == "/api/state":
                return self._send(200, svc.snapshot())
            if method == "POST" and path == "/api/logout":
                return self._send(200, {"ok": True}, headers=[("Set-Cookie", "dps=; Path=/; Max-Age=0")])
            if method == "POST" and path == "/api/schedule":
                body = self._json()
                def f(cfg):
                    for d in cfgmod.DAYS:
                        if d in body:
                            s = body[d]
                            cfg["schedule"][d] = {"enabled": bool(s["enabled"]), "on": s["on"], "off": s["off"]}
                svc.mutate(f)
                return self._send(200, {"ok": True})
            if method == "POST" and path == "/api/displays":
                body = self._json()
                def f(cfg):
                    cfg["displays"].append(cfgmod.make_display(cfg, body))
                svc.mutate(f)
                return self._send(200, {"ok": True})
            m = re.fullmatch(r"/api/displays/([^/]+)/(delete|update|on|off|status|test)", path)
            if method == "POST" and m:
                did, act = m.groups()
                d = svc.display(did)
                if not d:
                    return self._send(404, {"error": "no such display"})
                if act == "delete":
                    svc.mutate(lambda c: c.__setitem__("displays", [x for x in c["displays"] if x["id"] != did]))
                    svc.state.pop(did, None)
                    return self._send(200, {"ok": True})
                if act == "update":
                    body = self._json()
                    def f(cfg):
                        new = cfgmod.make_display(cfg, {**body, "id": did})
                        cfg["displays"] = [new if x["id"] == did else x for x in cfg["displays"]]
                    svc.mutate(f)
                    return self._send(200, {"ok": True})
                if act in ("on", "off"):
                    r = svc.manual(did, act == "on")
                    return self._send(200, {"ok": r.ok, "error": r.error})
                r = svc.poll(d) if act == "status" else drivers.get_power(d)
                return self._send(200, {"ok": r.ok, "power": r.power, "error": r.error,
                                        "tx": r.tx.hex(" "), "rx": r.rx.hex(" "), "ms": r.latency_ms})
            return self._send(404, {"error": "not found"})

    return H


def serve(svc, bind=None, port=None):
    web = svc.cfg["web"]
    addr = (bind or web["bind"], port or web["port"])
    srv = ThreadingHTTPServer(addr, make_handler(svc))
    srv.daemon_threads = True
    log.info("web UI on http://%s:%s", *addr)
    return srv
