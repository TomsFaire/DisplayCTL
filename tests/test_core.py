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
