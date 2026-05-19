"""Optional WiFi auto-connect on boot.

Set ``SSID`` / ``PASSWORD`` below to your own network if you want
the launcher to come up online (the Push-to-Claude voice/chat app
needs WiFi). Leave them empty to skip the auto-connect — the
launcher will display ``WiFi: offline`` and continue normally.

To disable the auto-connect entirely, remove the
``wifi_event.connect_with_splash(...)`` call from ``main.py``.

The module deliberately does NOT touch NVS. UIFlow's startup reads
WiFi creds from NVS keys (``ssid0``, ``pswd0``, ``net_mode``,
etc.); we set ``boot_option=2`` to bypass UIFlow's launcher, so
those keys may or may not be honored depending on UIFlow's exact
boot path. Doing the connect in pure Python from our own ``main.py``
is deterministic regardless of that.
"""

# --- WIFI CREDENTIALS ---------------------------------------------------
# Fallback (NVS 미설정 시 사용). 운영 자격증명은 NVS 에 저장하고
# 코드에는 빈 값만 둔다 (git 누출 방지). NVS 미설정 시 connect() 가
# 'no SSID configured' err 반환하고 launcher 는 'WiFi: offline' 표시.
# SSID/PW 입력은 디바이스에서 apps/wifi_config 앱으로 진행.
SSID = ""
PASSWORD = ""

# NVS-first override. esp32 모듈 import 실패나 NVS 미존재 시 조용히 fallback.
try:
    import esp32 as _esp32

    _nvs = _esp32.NVS("buddy")
    _buf = bytearray(128)
    try:
        _n = _nvs.get_blob("ssid", _buf)
        _v = bytes(_buf[:_n]).decode("utf-8")
        if _v:
            SSID = _v
    except Exception:
        pass
    try:
        _n = _nvs.get_blob("pswd", _buf)
        # 빈 문자열도 허용 (open network).
        PASSWORD = bytes(_buf[:_n]).decode("utf-8")
    except Exception:
        pass
except Exception:
    pass
# -----------------------------------------------------------------------

# How long to wait for an IP before giving up. The venue network is
# 2.4 GHz; on a fresh boot the WLAN chip needs a few seconds to scan
# and associate. 8 s is generous without being annoying if the
# network isn't actually present (e.g. running this code at home).
CONNECT_TIMEOUT_MS = 8000


def connect(timeout_ms=CONNECT_TIMEOUT_MS):
    """Try to connect to the event WiFi. Returns a status dict.

    On success:
      {"ok": True, "ssid": <str>, "ip": <str>, "rssi": <int|None>,
       "elapsed_ms": <int>}

    On failure:
      {"ok": False, "ssid": <str>, "err": <str>, "elapsed_ms": <int>}

    Idempotent: if the STA is already connected (e.g. retried after
    a soft reboot that didn't drop the link), returns success
    immediately without re-connecting.
    """
    import network
    import time

    if not SSID:
        return {
            "ok": False,
            "ssid": "",
            "err": "no SSID configured (edit buddy/device/wifi_event.py)",
            "elapsed_ms": 0,
        }

    sta = network.WLAN(network.STA_IF)
    if not sta.active():
        sta.active(True)

    if sta.isconnected():
        info = sta.ifconfig()
        return {
            "ok": True,
            "ssid": SSID,
            "ip": info[0],
            "rssi": _safe_rssi(sta),
            "elapsed_ms": 0,
        }

    t0 = time.ticks_ms()
    try:
        sta.connect(SSID, PASSWORD)
    except Exception as e:
        return {
            "ok": False,
            "ssid": SSID,
            "err": "connect call failed: {}".format(e),
            "elapsed_ms": time.ticks_diff(time.ticks_ms(), t0),
        }

    while not sta.isconnected():
        if time.ticks_diff(time.ticks_ms(), t0) > timeout_ms:
            return {
                "ok": False,
                "ssid": SSID,
                "err": "no IP within {}ms".format(timeout_ms),
                "elapsed_ms": time.ticks_diff(time.ticks_ms(), t0),
            }
        time.sleep_ms(200)

    info = sta.ifconfig()
    return {
        "ok": True,
        "ssid": SSID,
        "ip": info[0],
        "rssi": _safe_rssi(sta),
        "elapsed_ms": time.ticks_diff(time.ticks_ms(), t0),
    }


def is_connected():
    """Lightweight query for code that wants to render a status pip
    without re-attempting the connect. Returns True iff the STA
    currently reports an active link."""
    try:
        import network
        return network.WLAN(network.STA_IF).isconnected()
    except Exception:
        return False


def _safe_rssi(sta):
    """``sta.status('rssi')`` is supported on most builds but not
    universally. Wrap so a missing implementation doesn't crash the
    caller."""
    try:
        return sta.status("rssi")
    except Exception:
        return None
