import threading, socketserver
from datetime import datetime
from zoneinfo import ZoneInfo
from dps import config as C, drivers as D, service as S

def test_mdc_frame_checksum():
    assert D.mdc_frame(0x11, 0, b"\x01") == bytes([0xAA, 0x11, 0x00, 0x01, 0x01, 0x13])
    ack = D.mdc_frame(0xFF, 0, b"A\x11\x01")
    assert D.mdc_parse(b"\x00\x05" + ack) == (True, 0x11, b"\x01")
    assert D.mdc_parse(ack[:-1]) is None

def test_lg_parse():
    assert D.lg_cmd(1, "ff") == b"ka 01 ff\r"
    assert D.lg_parse(b"a 01 OK01x") == (True, "ON")
    assert D.lg_parse(b"a 01 NG00x")[0] is False

def test_pin():
    h = C.hash_pin("1234")
    assert C.check_pin("1234", h) and not C.check_pin("9999", h)

def test_schedule_and_dst():
    cfg = C.default_config()
    tz = ZoneInfo("America/New_York")
    assert S.intended(cfg, datetime(2026, 11, 2, 7, 10, tzinfo=tz))      # Mon 07:10 on
    assert not S.intended(cfg, datetime(2026, 11, 2, 20, 0, tzinfo=tz))  # Mon 20:00 off
    assert not S.intended(cfg, datetime(2026, 11, 7, 12, 0, tzinfo=tz))  # Sat disabled
    nc = S.next_change(cfg, datetime(2026, 11, 6, 21, 0, tzinfo=tz))     # Fri night -> Mon ON
    assert nc[1] == "ON" and nc[0].weekday() == 0
    # spring-forward Sunday: schedule that "turns on" at 02:30 still resolves after the gap
    cfg["schedule"]["sun"] = {"enabled": True, "on": "02:30", "off": "20:00"}
    assert S.intended(cfg, datetime(2027, 3, 14, 3, 5, tzinfo=tz))

def test_driver_against_fake_mdc():
    class H(socketserver.BaseRequestHandler):
        def handle(self):
            req = self.request.recv(64)
            self.request.sendall(D.mdc_frame(0xFF, req[2], b"A\x11" + (b"\x01" if req[3] == 0 else req[4:5])))
    srv = socketserver.TCPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
    d = {"driver": "samsung_mdc", "ip": "127.0.0.1", "port": srv.server_address[1], "display_id": 0}
    r = D.get_power(d); assert r.ok and r.power == "ON" and r.verified
    srv.shutdown()
    assert not D.get_power(d, timeout=1).ok  # refused -> failure, not crash

def test_automation_pause(tmp_path):
    p = tmp_path / "c.json"
    cfg = C.default_config(); cfg["automation"]["enabled"] = False
    cfg["displays"] = [{"id": "a", "name": "A", "ip": "127.0.0.1", "driver": "samsung_mdc", "port": 1, "display_id": 0}]
    C.save(p, cfg)
    svc = S.Service(p); calls = []
    svc.apply = lambda *a: calls.append(a)
    svc.tick(); svc.pool.shutdown(wait=True)
    assert calls == []
    svc.mutate(lambda c: c["automation"].__setitem__("enabled", True))
    svc.pool = __import__("concurrent.futures").futures.ThreadPoolExecutor(2)
    svc.tick(); svc.pool.shutdown(wait=True)
    assert len(calls) == 1

def test_source_ip_validation():
    import pytest
    cfg = C.default_config(); cfg["network"]["source_ip"] = "10.7.130.62"; C.validate(cfg)
    cfg["network"]["source_ip"] = "nope"
    with pytest.raises(ValueError): C.validate(cfg)

def test_proxy_headers_and_secure_cookie(tmp_path):
    import http.client, json
    from dps import web
    p = tmp_path / "c.json"
    cfg = C.default_config(); cfg["web"]["allowed_cidrs"] = ["10.7.0.0/16"]
    cfg["pin_hash"] = C.hash_pin("123456"); C.save(p, cfg)
    svc = S.Service(p)
    import socket
    with socket.socket() as t:
        t.bind(("127.0.0.1", 0)); port = t.getsockname()[1]
    srv = web.serve(svc, "127.0.0.1", port); threading.Thread(target=srv.serve_forever, daemon=True).start()

    def post(headers):
        c = http.client.HTTPConnection("127.0.0.1", port)
        c.request("POST", "/api/login", json.dumps({"pin": "123456"}), headers)
        r = c.getresponse(); r.read(); return r.status, r.getheader("Set-Cookie") or ""
    try:
        assert post({"X-Forwarded-For": "8.8.8.8"})[0] == 403           # client outside allowed_cidrs, seen via proxy
        st, ck = post({"X-Forwarded-For": "10.7.1.1", "X-Forwarded-Proto": "https"})
        assert st == 200 and "Secure" in ck and "HttpOnly" in ck
        # a client-supplied XFF chain: only the address the proxy appended counts
        assert post({"X-Forwarded-For": "10.7.1.1, 8.8.8.8"})[0] == 403
        # lockout is per forwarded client, not global for the proxy
        bad = lambda ip: http.client.HTTPConnection("127.0.0.1", port)
        for _ in range(5):
            c = bad(0); c.request("POST", "/api/login", json.dumps({"pin": "x"}), {"X-Forwarded-For": "10.7.2.2"}); c.getresponse().read()
        assert post({"X-Forwarded-For": "10.7.3.3"})[0] == 200
        assert post({"X-Forwarded-For": "10.7.2.2"})[0] == 429
    finally:
        srv.shutdown()


def test_find_free_port_skips_busy_ports():
    import socket
    held = []
    try:
        s = socket.socket(); s.bind(("0.0.0.0", 0)); s.listen(); held.append(s)
        busy = s.getsockname()[1]
        assert C.port_in_use(busy)
        got = C.find_free_port(busy)
        assert got > busy and not C.port_in_use(got)
        s2 = socket.socket(); s2.bind(("127.0.0.1", got)); s2.listen(); held.append(s2)   # loopback-only listener (e.g. another app)
        assert C.find_free_port(busy) > got
    finally:
        for h in held: h.close()


def test_set_port_auto_moves_off_busy_port(tmp_path):
    import socket, argparse
    from dps import cli
    p = tmp_path / "config.json"
    cfg = C.default_config(); C.save(p, cfg)
    s = socket.socket(); s.bind(("0.0.0.0", 0)); s.listen()
    busy = s.getsockname()[1]
    try:
        cli.cmd_set_port(argparse.Namespace(config=str(p), port="auto", start=busy, bind=None))
        assert C.load(p)["web"]["port"] > busy
    finally:
        s.close()
