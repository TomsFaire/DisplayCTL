"""Display drivers: Samsung MDC and LG signage over TCP. Each call connects, sends, reads, closes.

# VERIFY: all constants below against the actual displays during commissioning.
"""
import os
import socket
import time
from dataclasses import dataclass


@dataclass
class Result:
    ok: bool
    power: str = "UNKNOWN"  # ON | OFF | UNKNOWN
    verified: bool = False
    tx: bytes = b""
    rx: bytes = b""
    error: str = ""
    latency_ms: int = 0


def _txrx(host, port, data, timeout):
    t0 = time.time()
    src = os.environ.get("DPS_SOURCE_IP")  # optional: force the local address (multi-homed hosts)
    with socket.create_connection((host, port), timeout=timeout,
                                  source_address=(src, 0) if src else None) as s:
        s.settimeout(timeout)
        s.sendall(data)
        try:
            rx = s.recv(256)
        except socket.timeout:
            rx = b""
    return rx, int((time.time() - t0) * 1000)


# --- Samsung MDC --------------------------------------------------------------
def mdc_frame(cmd, disp_id, data=b""):
    body = bytes([cmd, disp_id, len(data)]) + data
    return b"\xaa" + body + bytes([sum(body) & 0xFF])


def mdc_parse(rx):
    """Return (ack, cmd, value_bytes) from the first valid frame; scans past garbage."""
    for i in range(len(rx)):
        if rx[i] != 0xAA or len(rx) < i + 4:
            continue
        ln = rx[i + 3]
        end = i + 4 + ln
        if len(rx) < end + 1:
            continue
        if sum(rx[i + 1 : end]) & 0xFF != rx[end]:
            continue
        d = rx[i + 4 : end]
        if rx[i + 1] == 0xFF and len(d) >= 2:
            return d[0] == 0x41, d[1], d[2:]
    return None


def _mdc(drv, on=None, timeout=5):
    did = drv["display_id"]
    frame = mdc_frame(0x11, did, b"" if on is None else bytes([1 if on else 0]))
    try:
        rx, ms = _txrx(drv["ip"], drv["port"], frame, timeout)
    except OSError as e:
        return Result(False, tx=frame, error=str(e))
    p = mdc_parse(rx)
    if p is None:
        return Result(False, tx=frame, rx=rx, latency_ms=ms,
                      error="no/invalid response" if not rx else "unparseable response")
    ack, _cmd, val = p
    if not ack:
        return Result(False, tx=frame, rx=rx, latency_ms=ms, error="display NAK")
    power = "UNKNOWN"
    if val:
        power = "ON" if val[0] == 1 else "OFF"
    return Result(True, power=power, verified=on is None, tx=frame, rx=rx, latency_ms=ms)


# --- LG signage ---------------------------------------------------------------
def lg_cmd(set_id, data):
    return f"ka {set_id:02x} {data}\r".encode()


def lg_parse(rx):
    s = rx.decode(errors="replace").strip().rstrip("x").strip()
    parts = s.split()
    if len(parts) >= 3 and parts[0] == "a":
        status = parts[2][:2]
        val = parts[2][2:]
        if status == "OK":
            return True, ("ON" if val.endswith("01") else "OFF" if val.endswith("00") else "UNKNOWN")
        return False, "UNKNOWN"
    return None


def _lg(drv, on=None, timeout=5):
    cmd = lg_cmd(drv["display_id"], "ff" if on is None else ("01" if on else "00"))
    try:
        rx, ms = _txrx(drv["ip"], drv["port"], cmd, timeout)
    except OSError as e:
        return Result(False, tx=cmd, error=str(e))
    p = lg_parse(rx)
    if p is None:
        return Result(False, tx=cmd, rx=rx, latency_ms=ms, error="no/invalid response")
    ok, power = p
    return Result(ok, power=power, verified=on is None and ok, tx=cmd, rx=rx, latency_ms=ms,
                  error="" if ok else "display NG")


def wol(mac, broadcast="255.255.255.255"):
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    pkt = b"\xff" * 6 + raw * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(pkt, (broadcast, 9))


# --- public API ---------------------------------------------------------------
def get_power(drv, timeout=5):
    return (_mdc if drv["driver"] == "samsung_mdc" else _lg)(drv, None, timeout)


def set_power(drv, on, timeout=5, retries=2, backoff=(2, 5), verify_s=45):
    """Send explicit on/off (never toggle), retry, then confirm with a status query."""
    fn = _mdc if drv["driver"] == "samsung_mdc" else _lg
    res = Result(False, error="not attempted")
    for attempt in range(retries + 1):
        if on and drv.get("mac"):
            try:
                wol(drv["mac"])
            except OSError:
                pass
        res = fn(drv, on, timeout)
        if res.ok:
            break
        if attempt < retries:
            time.sleep(backoff[min(attempt, len(backoff) - 1)])
    if res.ok:
        # Panels take a while to boot/shut down and may refuse connections meanwhile:
        # poll until the display reports the target state (or give up -> unverified).
        want = "ON" if on else "OFF"
        deadline = time.time() + (verify_s if on else verify_s / 2)
        seen = "UNKNOWN"
        while time.time() < deadline:
            time.sleep(3)
            chk = get_power(drv, timeout)
            if chk.ok and chk.power in ("ON", "OFF"):
                seen = chk.power
                if seen == want:
                    break
        res.power = seen if seen != "UNKNOWN" else want
        res.verified = seen == want
        if seen == "UNKNOWN":
            res.error = "command accepted but status could not be confirmed"
        elif not res.verified:
            res.error = f"sent {want} but display reports {seen}"
    return res
