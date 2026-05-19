"""apps/wifi_config.py — buddy WiFi 설정 UI.

NVS namespace "buddy" 에 SSID/PW 를 set_blob 으로 저장한다. wifi_event 가
부팅 시 이 값을 우선 읽어 코드 fallback 을 덮는다.

흐름:
  1. 현재 NVS 저장값 표시 (SSID + PW 설정 여부)
  2. Enter: SSID 편집 / ESC: 종료
  3. SSID 입력 (prefill = 현재값) → Enter 다음
  4. PW 입력 (마스킹, 빈 문자열 = open network) → Enter 저장
  5. NVS commit 후 reboot 안내
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

# ---- NVS ------------------------------------------------------------
NVS_NS = "buddy"
NVS_KEY_SSID = "ssid"
NVS_KEY_PSWD = "pswd"


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
            # 기존 키가 다른 type 으로 남아 있으면 get_blob 가
            # NOT_FOUND 를 던질 수 있어 erase 후 set.
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


# ---- UI -------------------------------------------------------------
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


def _draw_screen(title, body_lines, hint, hint_color=None):
    """공통 레이아웃: chrome(상단 20px) + 본문 + 하단 hint(18px)."""
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _font_big()
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString(title, 6, 4)
    y = 28
    _LCD.setTextColor(_CREAM, _BLACK)
    for line in body_lines:
        _LCD.drawString(line[:30], 6, y)
        y += 18
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(hint_color or _GRAY, _DARK)
    _LCD.drawString(hint, 6, _H - 14)


# ---- KEY HELPERS ----------------------------------------------------
def _printable(k):
    if k is None:
        return None
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            return chr(k)
        return None
    if isinstance(k, str) and len(k) == 1:
        oc = ord(k)
        if 0x20 <= oc <= 0x7E:
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


def _input_line(kb, title, hint_top, masked=False, prefill=""):
    """텍스트 한 줄 편집. Enter 확정 → str 반환, ESC 취소 → None."""
    buf = prefill

    def redraw():
        view = ("*" * len(buf)) if masked else buf
        # 끝부분 28자만 보여주고 캐럿 표시
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


def _wait_key(kb, allowed_pred):
    """단일 키 대기 (예: Enter/ESC). allowed_pred(k) True 시 반환."""
    while True:
        kb.tick()
        k = kb.get_key()
        if k is None:
            time.sleep_ms(30)
            continue
        if allowed_pred(k):
            return k


# ---- RUN ------------------------------------------------------------
def run():
    print("wifi_config: run() enter")
    _font_big()
    kb = MatrixKeyboard()
    time.sleep_ms(400)
    # launcher 의 Enter 잔류 키 흡수.
    for _ in range(15):
        kb.tick()
        kb.get_key()
        time.sleep_ms(20)

    cur_ssid = _nvs_read(NVS_KEY_SSID) or ""
    cur_pswd = _nvs_read(NVS_KEY_PSWD)
    cur_ssid_display = cur_ssid if cur_ssid else "(none)"
    if cur_pswd is None:
        pwd_state = "(none)"
    elif cur_pswd == "":
        pwd_state = "(open net)"
    else:
        pwd_state = "set"

    _draw_screen(
        "WiFi Config",
        [
            "SSID: " + cur_ssid_display[:24],
            "Pswd: " + pwd_state,
            "",
            "Enter to edit",
            "ESC to leave",
        ],
        "Enter=edit  ESC=back",
    )
    k = _wait_key(kb, lambda x: _is_enter(x) or _is_esc(x))
    if _is_esc(k):
        machine.reset()
        return

    ssid = _input_line(
        kb,
        "Edit SSID",
        "(current: " + cur_ssid_display[:18] + ")",
        masked=False,
        prefill=cur_ssid,
    )
    if ssid is None:
        _draw_screen("Cancelled", ["No change."], "any key=back",
                     hint_color=_GRAY)
        time.sleep_ms(1200)
        machine.reset()
        return
    if not ssid:
        _draw_screen("Error", ["SSID cannot be empty."], "any key=back",
                     hint_color=_RED)
        time.sleep_ms(1500)
        machine.reset()
        return

    pswd = _input_line(
        kb,
        "Edit password",
        "(empty = open network)",
        masked=True,
        prefill="",
    )
    if pswd is None:
        _draw_screen("Cancelled", ["No change."], "any key=back",
                     hint_color=_GRAY)
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
        "Saved",
        [
            "SSID: " + ssid[:24],
            "Pswd len: " + str(len(pswd)),
            "",
            "Reboot to apply.",
        ],
        "Enter=reboot  ESC=stay",
        hint_color=_GREEN,
    )
    k = _wait_key(kb, lambda x: _is_enter(x) or _is_esc(x))
    # 어느 쪽이든 launcher 로 복귀하려면 machine.reset() 가 표준 패턴.
    machine.reset()


gc.collect()
run()
machine.reset()
