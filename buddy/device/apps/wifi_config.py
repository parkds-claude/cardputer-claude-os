"""apps/wifi_config.py — buddy WiFi 설정 UI.

NVS namespace "buddy" 에 SSID/PW 를 set_blob 으로 저장한다. wifi_event 가
부팅 시 이 값을 우선 읽어 코드 fallback 을 덮는다.

흐름:
  1. 현재 NVS 저장값 표시
  2. Enter: WiFi 스캔 → 목록에서 선택
     T: SSID 직접 입력 (기존 방식)
     ESC: 종료
  3. 선택한 SSID 확인 → PW 입력 (마스킹)
  4. NVS commit 후 reboot 안내
"""

import gc
import time

import M5
import machine
from hardware import MatrixKeyboard

try:
    import esp32
except ImportError:
    esp32 = None

try:
    import network as _network
except ImportError:
    _network = None

# ---- LCD / 색 -------------------------------------------------------
_LCD = M5.Lcd
_W = 240
_H = 135
_BLACK = 0x000000
_DARK = 0x202020
_CREAM = 0xFFE4B5
_ORANGE = 0xFF8800
_GRAY = 0x808080
_GREEN = 0x00C800
_RED = 0xC83232
_BLUE = 0x4488FF

# ---- NVS ------------------------------------------------------------
NVS_NS = "buddy"
NVS_KEY_SSID = "ssid"
NVS_KEY_PSWD = "pswd"

_ROWS_VISIBLE = 5
_ROW_H = 16


def _open_nvs():
    if esp32 is None:
        return None
    try:
        return esp32.NVS(NVS_NS)
    except Exception as e:
        print("wifi_config: NVS open err:", e)
        return None


def _nvs_read(key):
    nvs = _open_nvs()
    if nvs is None:
        return None
    try:
        buf = bytearray(128)
        n = nvs.get_blob(key, buf)
        return bytes(buf[:n]).decode("utf-8")
    except Exception:
        return None


def _nvs_write(ssid, pswd):
    nvs = _open_nvs()
    if nvs is None:
        return False
    try:
        for key, val in ((NVS_KEY_SSID, ssid), (NVS_KEY_PSWD, pswd)):
            try:
                nvs.erase_key(key)
            except Exception:
                pass
            nvs.set_blob(key, val.encode("utf-8"))
        nvs.commit()
        return True
    except Exception as e:
        print("wifi_config: NVS write err:", e)
        return False


# ---- UI 공통 ---------------------------------------------------------
def _font_big():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu12)
    except Exception:
        pass


def _font_small():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception:
        pass


def _draw_chrome(title, hint, hint_color=None):
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _font_big()
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString(title, 6, 4)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(hint_color or _GRAY, _DARK)
    _font_small()
    _LCD.drawString(hint[:38], 4, _H - 13)


def _draw_screen(title, body_lines, hint, hint_color=None):
    _draw_chrome(title, hint, hint_color)
    y = 28
    _font_big()
    _LCD.setTextColor(_CREAM, _BLACK)
    for line in body_lines:
        _LCD.drawString(str(line)[:28], 6, y)
        y += _ROW_H


# ---- KEY HELPERS ----------------------------------------------------
def _printable(k):
    if k is None:
        return None
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            return chr(k)
        return None
    if isinstance(k, str) and len(k) == 1 and 0x20 <= ord(k) <= 0x7E:
        return k
    return None


def _is_enter(k):
    if k is None:
        return False
    if isinstance(k, int):
        return k in (0x0A, 0x0D)
    return isinstance(k, str) and k in ("\n", "\r")


def _is_esc(k):
    if k is None:
        return False
    if isinstance(k, int):
        return k == 0x1B
    return isinstance(k, str) and k == "\x1b"


def _is_backspace(k):
    if k is None:
        return False
    if isinstance(k, int):
        return k in (0x08, 0x7F)
    return isinstance(k, str) and k in ("\x08", "\x7f")


def _scroll_dir(k):
    """; , → up   . / → down  (push_to_claude 와 동일 키맵)"""
    ch = _printable(k)
    if ch is None:
        return None
    if ch in (";", ","):
        return "up"
    if ch in (".", "/"):
        return "down"
    return None


def _is_manual(k):
    ch = _printable(k)
    return ch is not None and ch.lower() == "t"


def _wait_key(kb, pred):
    while True:
        kb.tick()
        k = kb.get_key()
        if k is not None and pred(k):
            return k
        time.sleep_ms(30)


# ---- WiFi 스캔 -------------------------------------------------------
def _rssi_bars(rssi):
    """RSSI → 간단한 신호 표시 문자열 (4단계)."""
    if rssi >= -55:
        return "****"
    if rssi >= -65:
        return "*** "
    if rssi >= -75:
        return "**  "
    return "*   "


def _ble_down():
    """BLE 비활성화. ESP32 2.4GHz 라디오 공유로 BLE 켜진 상태에서
    WiFi scan 이 0 결과를 반환하는 간섭 문제 방지."""
    try:
        import bluetooth
        ble = bluetooth.BLE()
        if ble.active():
            ble.active(False)
            time.sleep_ms(150)
    except Exception:
        pass


def _scan_networks():
    """주변 WiFi 스캔 (동기, ~3초). (ssid, rssi, is_open) 리스트 반환."""
    if _network is None:
        return []
    _draw_screen("Scanning...", ["Searching for WiFi...", "", "~3 seconds"], "please wait")
    sta = _network.WLAN(_network.STA_IF)
    # UIFlow 백그라운드 태스크가 disconnect() 후 400ms 안에 재연결해버려
    # scan()이 status=1010 상태에서 0을 반환하는 문제.
    # active(False→True) 로 라디오 자체를 완전 리셋해서 UIFlow 상태기계 우회.
    sta.active(False)
    time.sleep_ms(400)
    sta.active(True)
    time.sleep_ms(1000)
    nets_raw = []
    try:
        nets_raw = sta.scan()
    except Exception as e:
        print("scan err:", e)

    seen = set()
    result = []
    for n in nets_raw:
        try:
            ssid = n[0].decode("utf-8") if isinstance(n[0], (bytes, bytearray)) else str(n[0])
        except Exception:
            try:
                ssid = n[0].decode("latin-1")
            except Exception:
                continue
        ssid = ssid.strip()
        if not ssid or ssid in seen:
            continue
        seen.add(ssid)
        rssi = n[3] if len(n) > 3 else -99
        auth = n[4] if len(n) > 4 else 1
        result.append((ssid, rssi, auth == 0))

    result.sort(key=lambda x: -x[1])
    gc.collect()
    return result[:24]


# ---- 스캔 결과 목록 UI -----------------------------------------------
def _draw_net_list(nets, selected, scroll):
    _draw_chrome("Select WiFi", ";.=move  Enter=ok  T=manual  ESC=back")
    if not nets:
        _font_big()
        _LCD.setTextColor(_GRAY, _BLACK)
        _LCD.drawString("No networks found.", 6, 50)
        return
    end = min(scroll + _ROWS_VISIBLE, len(nets))
    for vi, idx in enumerate(range(scroll, end)):
        ssid, rssi, is_open = nets[idx]
        y = 24 + vi * _ROW_H
        is_sel = (idx == selected)
        bg = _DARK if is_sel else _BLACK
        if is_sel:
            _LCD.fillRect(0, y - 1, _W, _ROW_H, _DARK)
            _LCD.setTextColor(_ORANGE, _DARK)
            _LCD.drawString(">", 3, y)
        # 자물쇠 표시
        lock = " " if is_open else "L"
        _font_small()
        _LCD.setTextColor(_GRAY if is_sel else _GRAY, bg)
        _LCD.drawString(lock, 14, y)
        # SSID (최대 22자)
        _font_big()
        _LCD.setTextColor(_CREAM if is_sel else _CREAM, bg)
        _LCD.drawString(ssid[:21], 24, y)
        # 신호 강도 (우측)
        bars = _rssi_bars(rssi)
        _font_small()
        _LCD.setTextColor(_GREEN if rssi >= -65 else _GRAY, bg)
        bw = _LCD.textWidth(bars)
        _LCD.drawString(bars, _W - bw - 4, y + 3)

    # 스크롤 인디케이터
    if scroll > 0:
        _LCD.fillTriangle(_W - 8, 24, _W - 2, 24, _W - 5, 20, _ORANGE)
    if end < len(nets):
        bottom_y = 24 + (end - scroll - 1) * _ROW_H + 8
        _LCD.fillTriangle(_W - 8, bottom_y, _W - 2, bottom_y, _W - 5, bottom_y + 4, _ORANGE)


def _select_network(kb, nets):
    """목록에서 WiFi 선택. 선택된 ssid 반환, None=취소."""
    if not nets:
        return None
    selected = 0
    scroll = 0
    _draw_net_list(nets, selected, scroll)
    while True:
        kb.tick()
        k = kb.get_key()
        if k is None:
            time.sleep_ms(30)
            continue
        if _is_esc(k):
            return None
        if _is_manual(k):
            return ""  # 빈 문자열 = 직접 입력 신호
        if _is_enter(k) and nets:
            return nets[selected][0]
        d = _scroll_dir(k)
        if d == "up" and selected > 0:
            selected -= 1
            if selected < scroll:
                scroll = selected
            _draw_net_list(nets, selected, scroll)
        elif d == "down" and selected < len(nets) - 1:
            selected += 1
            if selected >= scroll + _ROWS_VISIBLE:
                scroll = selected - _ROWS_VISIBLE + 1
            _draw_net_list(nets, selected, scroll)


# ---- 텍스트 입력 (기존 방식 유지) ------------------------------------
def _input_line(kb, title, hint_top, masked=False, prefill=""):
    """텍스트 한 줄 편집. Enter 확정 → str, ESC 취소 → None."""
    buf = prefill

    def redraw():
        view = ("*" * len(buf)) if masked else buf
        tail = ("> " + view + "_")[-30:]
        _draw_screen(
            title,
            [hint_top, "", tail],
            "Enter=ok  ESC=cancel  BS=del",
        )

    redraw()
    while True:
        kb.tick()
        k = kb.get_key()
        if k is None:
            time.sleep_ms(30)
            continue
        if _is_esc(k):
            return None
        if _is_enter(k):
            return buf
        if _is_backspace(k):
            if buf:
                buf = buf[:-1]
                redraw()
            continue
        ch = _printable(k)
        if ch is None:
            continue
        if len(buf) >= 60:
            continue
        buf += ch
        redraw()


# ---- RUN ------------------------------------------------------------
def run():
    print("wifi_config: run() enter")
    _font_big()
    kb = MatrixKeyboard()
    time.sleep_ms(400)
    for _ in range(15):
        kb.tick()
        kb.get_key()
        time.sleep_ms(20)

    cur_ssid = _nvs_read(NVS_KEY_SSID) or ""
    cur_pswd = _nvs_read(NVS_KEY_PSWD)
    cur_ssid_disp = cur_ssid if cur_ssid else "(none)"
    pwd_state = "(none)" if cur_pswd is None else ("(open)" if cur_pswd == "" else "set")

    _draw_screen(
        "WiFi Config",
        [
            "SSID: " + cur_ssid_disp[:22],
            "Pswd: " + pwd_state,
            "",
            "Enter=scan  T=manual",
            "ESC=back",
        ],
        "Enter=scan  T=type  ESC=back",
    )
    k = _wait_key(kb, lambda x: _is_enter(x) or _is_esc(x) or _is_manual(x))
    if _is_esc(k):
        machine.reset()
        return

    if _is_enter(k):
        # 스캔 모드
        nets = _scan_networks()
        if not nets:
            _draw_screen("No Networks", ["No WiFi found.", "Try manual entry."],
                         "T=manual  ESC=back")
            k2 = _wait_key(kb, lambda x: _is_esc(x) or _is_manual(x))
            if _is_esc(k2):
                machine.reset()
                return
            ssid = _input_line(kb, "Enter SSID", "(WiFi name)", masked=False, prefill=cur_ssid)
        else:
            chosen = _select_network(kb, nets)
            if chosen is None:
                machine.reset()
                return
            if chosen == "":
                # T=manual 선택
                ssid = _input_line(kb, "Enter SSID", "(WiFi name)", masked=False, prefill=cur_ssid)
            else:
                ssid = chosen
    else:
        # T 키 = 직접 입력
        ssid = _input_line(kb, "Enter SSID", "(WiFi name)", masked=False, prefill=cur_ssid)

    if ssid is None:
        _draw_screen("Cancelled", ["No change."], "any key=back", hint_color=_GRAY)
        time.sleep_ms(1200)
        machine.reset()
        return
    if not ssid:
        _draw_screen("Error", ["SSID cannot be empty."], "any key=back", hint_color=_RED)
        time.sleep_ms(1500)
        machine.reset()
        return

    pswd = _input_line(
        kb,
        "Password",
        "SSID: " + ssid[:22],
        masked=False,
        prefill="",
    )
    if pswd is None:
        _draw_screen("Cancelled", ["No change."], "any key=back", hint_color=_GRAY)
        time.sleep_ms(1200)
        machine.reset()
        return

    ok = _nvs_write(ssid, pswd)
    if not ok:
        _draw_screen(
            "Save FAILED",
            ["NVS write error.", "Settings not saved."],
            "any key=back",
            hint_color=_RED,
        )
        time.sleep_ms(1800)
        machine.reset()
        return

    _draw_screen(
        "Saved!",
        [
            "SSID: " + ssid[:22],
            "Pswd: " + ("(open)" if not pswd else "*" * min(len(pswd), 8) + "..."),
            "",
            "Reboot to connect.",
        ],
        "Enter=reboot  ESC=stay",
        hint_color=_GREEN,
    )
    _wait_key(kb, lambda x: _is_enter(x) or _is_esc(x))
    machine.reset()


gc.collect()
run()
machine.reset()
