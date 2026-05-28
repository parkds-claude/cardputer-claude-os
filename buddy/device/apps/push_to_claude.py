"""Push-to-Claude — speak to ask Claude, get a reply on the LCD.

Tap SPACE to start, tap SPACE again to stop. Audio streams straight
to ``/flash/last.wav`` as it's captured so RAM use stays flat
regardless of clip length, then we POST the WAV body to a Cloudflare
Worker that runs Whisper for STT and Claude Haiku 4.5 for the reply.

State machine:
  IDLE       → SPACE      → RECORDING
  RECORDING  → SPACE      → UPLOADING  (also auto-stops at MAX_SECONDS)
  UPLOADING  → reply      → SHOWING
  UPLOADING  → error      → ERROR
  SHOWING    → SPACE      → IDLE
  ERROR      → SPACE      → IDLE
  any        → Q / ESC    → exit (machine.reset)

There's no visible timer; recording feels open-ended. The MAX_SECONDS
cap below is purely a safety bound on disk + upload size, not part
of the UX.
"""

import gc
import os
import struct
import time

import M5
import machine
from hardware import MatrixKeyboard


# ---- DEPLOYMENT-SPECIFIC CONSTANTS ----------------------------------
# Loaded from buddy/device/apps/config.py at runtime. That file is
# gitignored — copy ``config.example.py`` to ``config.py`` and fill in
# your own Cloudflare Worker URL + device secret. See ``worker/README.md``
# for how to deploy your own relay.
try:
    from . import config as _cfg  # type: ignore
except Exception:
    try:
        import config as _cfg  # type: ignore
    except Exception:
        _cfg = None

_WORKER_BASE = (getattr(_cfg, "WORKER_BASE", "") if _cfg else "").rstrip("/")
DEVICE_SECRET = getattr(_cfg, "DEVICE_SECRET", "") if _cfg else ""
WORKER_URL = _WORKER_BASE + "/ask"             # voice (raw WAV body)
WORKER_TEXT_URL = _WORKER_BASE + "/ask-text"   # text (JSON {prompt})
WORKER_RESET_URL = _WORKER_BASE + "/reset"     # clear server-side history
WORKER_TTS_URL = _WORKER_BASE + "/tts"         # tts (JSON {text} -> wav)
WORKER_MENU_URL = _WORKER_BASE + "/menu"       # briefing menu (GET)
WORKER_PLAY_URL = _WORKER_BASE + "/play"       # briefing card play (POST)

# TTS playback target on the internal flash. Capped via server-side
# TTS_MAX_CHARS so the file stays well under the available /flash space.
_TTS_PATH = "/flash/.tts.wav"
# ---------------------------------------------------------------------


# 16 kHz / 16-bit signed / mono. The Cardputer-Adv's PDM mic appears
# to be hardware-locked to 16 kHz on this firmware — calling
# setSampleRate(8000) was accepted silently but the recorded data
# stayed at 16 kHz, producing WAV files that Whisper interpreted as
# slowed-down audio (or hung on entirely). Stay at 16 kHz and bound
# the cap instead.
_RATE = 16000
_BITS = 16
_CHANNELS = 1
_BYTES_PER_SAMPLE = _BITS // 8 * _CHANNELS

# Recording duration. With M5.Mic.recordWavFile the firmware does the
# capture into a file directly — fixed duration (no tap-to-stop), but
# actual audio data we can transcribe (vs. the silent -8 samples my
# hand-rolled record() loop was producing).
_MAX_SECONDS = 6

# Chunk granularity for the mic capture loop. 50 ms = 400 samples at
# 8 kHz; small enough to keep keyboard-stop responsive, large enough
# to avoid chunk-setup overhead.
_CHUNK_SAMPLES = _RATE // 20
_CHUNK_BYTES = _CHUNK_SAMPLES * _BYTES_PER_SAMPLE

_AUDIO_PATH = "/flash/last.wav"


# Theme — matches the rest of the bundle.
_BLACK = 0x000000
_ORANGE = 0xCC785C
_CREAM = 0xF0EEE6
_DARK = 0x1F1F1F
_GRAY_MID = 0x777777
_GREEN = 0x00FF00
_RED = 0xFF0000

_LCD = M5.Lcd
_W = 240
_H = 135


# ---- UI HELPERS -----------------------------------------------------

def _set_font():
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu9)
    except Exception as e:
        print("p2c: setFont fallback:", e)


def _draw_chrome(title="Push to Claude", hint="SPACE record  Q/ESC back"):
    _LCD.fillScreen(_BLACK)
    _LCD.fillRect(0, 0, _W, 20, _DARK)
    _LCD.fillRect(0, 20, _W, 1, _ORANGE)
    _LCD.setTextSize(1)
    _LCD.setTextColor(_ORANGE, _DARK)
    _LCD.drawString(title, 6, 5)

    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    _LCD.drawString(hint, (_W - _LCD.textWidth(hint)) // 2, _H - 14)


def _draw_centered(text, y, color=_CREAM, size=1):
    _LCD.setTextSize(size)
    _LCD.setTextColor(color, _BLACK)
    _LCD.drawString(text, (_W - _LCD.textWidth(text)) // 2, y)


def _wrap_lines(text, max_w_px, char_size=1):
    """Greedy word-wrap for the 240 px content area."""
    _LCD.setTextSize(char_size)
    words = (text or "").split()
    lines = []
    cur = ""
    for w in words:
        cand = w if not cur else cur + " " + w
        if _LCD.textWidth(cand) <= max_w_px:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
            while _LCD.textWidth(cur) > max_w_px and len(cur) > 1:
                cut = len(cur) - 1
                while cut > 1 and _LCD.textWidth(cur[:cut]) > max_w_px:
                    cut -= 1
                lines.append(cur[:cut])
                cur = cur[cut:]
    if cur:
        lines.append(cur)
    return lines


def _draw_idle(wifi_ok, status_msg=None):
    _draw_chrome(hint="SPACE  T  M menu  N new  Q")
    _draw_centered("Ask Claude", 30, _CREAM, 2)
    _draw_centered("SPACE = voice    T = text", 56, _GRAY_MID, 1)
    _draw_centered("M = briefing menu", 70, _GRAY_MID, 1)
    _draw_centered("N = new chat (clear memory)", 84, _GRAY_MID, 1)
    if status_msg:
        _draw_centered(status_msg, 102, _GREEN, 1)
    elif wifi_ok:
        _draw_centered("WiFi: online", 102, _GREEN, 1)
    else:
        _draw_centered("WiFi: OFFLINE", 102, _RED, 1)


def _draw_typing(buf, cursor_on):
    """Render the text-input screen with the buffer wrapped, plus a
    blinking cursor at the end of the last line. Called every ~250 ms
    to drive the blink and on every key press to reflect the buffer."""
    _draw_chrome(title="Type to Claude", hint="Enter send  Esc back")
    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    _LCD.drawString("> ", 6, 28)
    _LCD.setTextColor(_CREAM, _BLACK)

    lines = _wrap_lines(buf or " ", _W - 24, 1) or [""]
    # Limit to 5 visible lines; truncate from the start if longer so
    # the user always sees what they're currently typing.
    if len(lines) > 5:
        lines = lines[-5:]
    y = 28
    for line in lines:
        _LCD.fillRect(18, y, _W - 24, 12, _BLACK)
        _LCD.drawString(line, 18, y)
        y += 12

    # Blinking caret at the end of the last drawn line.
    last_line = lines[-1] if lines else ""
    cur_x = 18 + _LCD.textWidth(last_line)
    cur_y = y - 12
    if cursor_on:
        _LCD.fillRect(cur_x, cur_y + 1, 6, 10, _ORANGE)
    else:
        _LCD.fillRect(cur_x, cur_y + 1, 6, 10, _BLACK)


# Pulsing dots — five orange circles that "breathe" left-to-right while
# recording. Replaces the literal countdown the user didn't want.
_DOT_COUNT = 5
_DOT_RADIUS = 5
_DOT_SPACING = 22


def _draw_recording_initial():
    _LCD.fillRect(0, 21, _W, _H - 21 - 18, _BLACK)
    _draw_centered("Recording", 36, _ORANGE, 2)
    _draw_centered("speak now ({}s)".format(_MAX_SECONDS), 96, _GRAY_MID, 1)
    _LCD.fillRect(0, 60, _W, 24, _BLACK)
    _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
    _LCD.setTextColor(_GRAY_MID, _DARK)
    h = "Q/ESC abort"
    _LCD.drawString(h, (_W - _LCD.textWidth(h)) // 2, _H - 14)


def _draw_recording_dots(phase):
    """Animate a single bright dot moving across DOT_COUNT positions.
    Cheap, eye-catching, and gives an at-a-glance heartbeat without
    a numeric timer."""
    total_w = (_DOT_COUNT - 1) * _DOT_SPACING + _DOT_RADIUS * 2
    x0 = (_W - total_w) // 2 + _DOT_RADIUS
    y = 72
    _LCD.fillRect(0, y - _DOT_RADIUS - 2, _W, _DOT_RADIUS * 2 + 4, _BLACK)
    for i in range(_DOT_COUNT):
        cx = x0 + i * _DOT_SPACING
        if i == phase:
            _LCD.fillCircle(cx, y, _DOT_RADIUS, _ORANGE)
        else:
            _LCD.fillCircle(cx, y, _DOT_RADIUS - 2, _DARK)


def _draw_uploading(stage="thinking", detail=""):
    _LCD.fillRect(0, 21, _W, _H - 21 - 18, _BLACK)
    _draw_centered(stage, 50, _ORANGE, 2)
    if detail:
        _draw_centered(detail, 80, _GRAY_MID, 1)
    else:
        _draw_centered("uploading + asking Claude", 80, _GRAY_MID, 1)


def _ascii_safe(s, fallback):
    # The bundled DejaVu9 font has no Hangul/CJK glyphs, so any
    # non-ASCII content renders as a row of empty boxes. We play the
    # reply through TTS instead — on screen, swap the unrenderable
    # text for an English placeholder so the user sees a clean state.
    if not s:
        return s
    for c in s:
        if ord(c) > 0x7E:
            return fallback
    return s


def _result_layout(transcript, response):
    """Pre-wrap both halves of a result so scrolling can pick a window
    without re-wrapping every redraw. Returns (transcript_lines,
    response_lines, response_y, max_visible)."""
    _LCD.setTextSize(1)
    safe_t = _ascii_safe(transcript, "(voice captured)")
    safe_r = _ascii_safe(response, "Playing reply through speaker...")
    t_lines = _wrap_lines("you: " + (safe_t or "(silent)"), _W - 12, 1)[:2]
    response_y = 24 + len(t_lines) * 12 + 10  # +10 covers the hairline gap
    max_visible = max(1, (_H - 18 - response_y) // 12)
    r_lines = _wrap_lines(safe_r or "(empty)", _W - 12, 1)
    return t_lines, r_lines, response_y, max_visible


def _draw_result(transcript, response, scroll=0):
    """Render a result with optional scroll offset into the response.
    ``scroll`` is the index of the first response line to show; the
    caller bounds it to ``[0, len(r_lines) - max_visible]``."""
    t_lines, r_lines, response_y, max_visible = _result_layout(
        transcript, response,
    )
    can_scroll = len(r_lines) > max_visible
    hint = "SPACE voice  T text  Q back"
    if can_scroll:
        hint = "; . scroll  SPACE  T  Q"
    _draw_chrome(hint=hint)

    _LCD.setTextSize(1)
    _LCD.setTextColor(_GRAY_MID, _BLACK)
    y = 24
    for line in t_lines:
        _LCD.drawString(line, 6, y)
        y += 12
    _LCD.fillRect(6, y + 2, _W - 12, 1, _DARK)

    _LCD.setTextColor(_CREAM, _BLACK)
    visible = r_lines[scroll:scroll + max_visible]
    y = response_y
    for line in visible:
        _LCD.drawString(line, 6, y)
        y += 12

    # Scroll indicators on the right edge — small orange triangles
    # only when there's something above/below the viewport.
    if can_scroll:
        if scroll > 0:
            _LCD.fillTriangle(
                _W - 8, response_y + 2,
                _W - 2, response_y + 2,
                _W - 5, response_y - 3,
                _ORANGE,
            )
        if scroll + max_visible < len(r_lines):
            bottom_y = response_y + (len(visible) - 1) * 12
            _LCD.fillTriangle(
                _W - 8, bottom_y + 6,
                _W - 2, bottom_y + 6,
                _W - 5, bottom_y + 11,
                _ORANGE,
            )


def _draw_error(msg):
    _draw_chrome(hint="SPACE retry  Q/ESC back")
    _LCD.setTextSize(1)
    _LCD.setTextColor(_RED, _BLACK)
    _LCD.drawString("Error", 6, 28)
    _LCD.setTextColor(_CREAM, _BLACK)
    for i, line in enumerate(_wrap_lines(msg, _W - 12, 1)[:6]):
        _LCD.drawString(line, 6, 46 + i * 12)


# ---- BRIEFING MENU --------------------------------------------------

# Visible rows in the menu list. DejaVu12 폰트 (≈1.3× DejaVu9) 가독성용:
# 5 rows × 18 px = 90 px, top chrome 21 px + footer 18 px = 39 px, 화면 135 px.
_MENU_ROW_H = 18
_MENU_ROWS_VISIBLE = 5
_MENU_Y0 = 24


def _draw_menu(items, selected, scroll, now_text, jump_buf, status_msg=None):
    """Render the briefing menu — list of cards with the selected one
    highlighted. ``items`` is the list from GET /menu (each has key,
    label, last_updated, stale, always_fresh). ``now_text`` is the
    short HH:MM string painted top-right so the user knows the relay's
    clock (the Cardputer RTC may not be set)."""
    # 노안 가독성: 메뉴 화면만 DejaVu12 (~1.3×). 함수 종료 시 DejaVu9 복원.
    try:
        _LCD.setFont(_LCD.FONTS.DejaVu12)
    except Exception:
        pass
    _draw_chrome(title="Briefing", hint="; . move  letter  Enter")
    if now_text:
        _LCD.setTextColor(_GRAY_MID, _DARK)
        _LCD.drawString(now_text, _W - 6 - _LCD.textWidth(now_text), 5)

    # Wipe content area between chrome and footer before redraw.
    _LCD.fillRect(0, 21, _W, _H - 21 - 18, _BLACK)

    if not items:
        _draw_centered(status_msg or "(loading...)", 64, _GRAY_MID, 1)
        _set_font()
        return

    n = len(items)
    end = min(scroll + _MENU_ROWS_VISIBLE, n)
    for vi, idx in enumerate(range(scroll, end)):
        c = items[idx]
        y = _MENU_Y0 + vi * _MENU_ROW_H
        is_sel = (idx == selected)
        bg = _DARK if is_sel else _BLACK
        if is_sel:
            _LCD.fillRect(0, y - 1, _W, _MENU_ROW_H, _DARK)
            _LCD.setTextColor(_ORANGE, _DARK)
            _LCD.drawString(">", 4, y)
        label = "[{}] {}".format(c.get("key", "?"), c.get("label", ""))
        if len(label) > 26:
            label = label[:25] + "."
        _LCD.setTextColor(_CREAM if is_sel else _GRAY_MID, bg)
        _LCD.drawString(label, 14, y)
        # Right-edge state marker.
        if c.get("stale"):
            _LCD.setTextColor(_RED, bg)
            _LCD.drawString("!", _W - 14, y)
        elif c.get("always_fresh"):
            _LCD.setTextColor(_GREEN, bg)
            _LCD.drawString("~", _W - 14, y)

    # Scroll indicators on the right edge.
    if scroll > 0:
        _LCD.fillTriangle(_W - 8, _MENU_Y0, _W - 2, _MENU_Y0,
                          _W - 5, _MENU_Y0 - 5, _ORANGE)
    if end < n:
        ly = _MENU_Y0 + (end - scroll - 1) * _MENU_ROW_H + 8
        _LCD.fillTriangle(_W - 8, ly, _W - 2, ly, _W - 5, ly + 5, _ORANGE)

    # Footer: jump buffer (orange) or status message (green) overrides
    # the default hint to give the user feedback on shortcut typing.
    if jump_buf or status_msg:
        _LCD.fillRect(0, _H - 18, _W, 18, _DARK)
        msg = ("jump: " + jump_buf) if jump_buf else status_msg
        _LCD.setTextColor(_ORANGE if jump_buf else _GREEN, _DARK)
        _LCD.drawString(msg, (_W - _LCD.textWidth(msg)) // 2, _H - 14)

    # 폰트 복원 — 다른 화면(idle/typing/showing 등)은 DejaVu9 가정.
    _set_font()


# ---- KEY HELPERS ----------------------------------------------------

def _is_exit(k):
    if k is None:
        return False
    if isinstance(k, int):
        if k == 0x1B:
            return True
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return False
    return isinstance(k, str) and k and k.lower() == "q"


def _is_space(k):
    if k is None:
        return False
    if isinstance(k, int):
        if k == 0x20:
            return True
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return False
    return isinstance(k, str) and k == " "


def _is_new_chat(k):
    """`n` or `N` → clear conversation history and start fresh."""
    if k is None:
        return False
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return False
    return isinstance(k, str) and k.lower() == "n"


def _is_menu_trigger(k):
    """`m` or `M` while idle → enter briefing-menu mode."""
    if k is None:
        return False
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return False
    return isinstance(k, str) and k.lower() == "m"


def _is_text_trigger(k):
    """`t` or `T` while idle → enter text-input mode."""
    if k is None:
        return False
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return False
    return isinstance(k, str) and k.lower() == "t"


def _is_enter(k):
    if k is None:
        return False
    if isinstance(k, int) and k in (0x0A, 0x0D):
        return True
    return isinstance(k, str) and k in ("\r", "\n")


def _is_backspace(k):
    if k is None:
        return False
    # 0x08 (BS) or 0x7F (DEL); UIFlow's MatrixKeyboard has been
    # observed to use both depending on firmware vintage.
    if isinstance(k, int) and k in (0x08, 0x7F):
        return True
    return isinstance(k, str) and k in ("\b", "\x7f")


def _scroll_intent(k):
    """Return 'up' / 'down' for the Cardputer-Adv arrow cluster.
    Same key mapping the launcher uses (; / , = up; . / / = down)
    so users have one mental model for "scroll" across the bundle."""
    if k is None:
        return None
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            k = chr(k)
        else:
            return None
    if not isinstance(k, str) or not k:
        return None
    ch = k.lower()
    if ch in (";", ","):
        return "up"
    if ch in (".", "/"):
        return "down"
    return None


def _printable_char(k):
    """Return a single printable ASCII char to append to the input
    buffer, or None if the key isn't a regular character."""
    if k is None:
        return None
    if isinstance(k, int):
        if 0x20 <= k <= 0x7E:
            return chr(k)
        return None
    if isinstance(k, str) and k and 0x20 <= ord(k[0]) <= 0x7E:
        return k[0]
    return None


# ---- AUDIO ----------------------------------------------------------

def _wav_header(num_samples):
    data_size = num_samples * _BYTES_PER_SAMPLE
    byte_rate = _RATE * _BYTES_PER_SAMPLE
    block_align = _BYTES_PER_SAMPLE
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_size, b"WAVE",
        b"fmt ", 16,
        1,
        _CHANNELS,
        _RATE,
        byte_rate,
        block_align,
        _BITS,
        b"data", data_size,
    )


def _free_internal_ram():
    """Tear down NimBLE and force-collect to release internal-RAM
    pressure ahead of an HTTPS upload.

    Background: the launcher (main.py) calls `bluetooth.BLE().active(True)`
    early so claude_buddy can advertise reliably. NimBLE on this build
    holds ~30 KB of internal RAM permanently while active. mbedTLS
    needs contiguous internal RAM (no PSRAM fallback in this firmware)
    for its working buffers during the TLS handshake; with NimBLE
    holding territory, even a 96 KB body OOMs at handshake time. We
    drop BLE here, get internal RAM back, and the launcher reactivates
    BLE on its next boot anyway."""
    try:
        import bluetooth
        ble = bluetooth.BLE()
        if ble.active():
            ble.active(False)
    except Exception as e:
        print("p2c: ble teardown warn:", e)
    gc.collect()
    gc.collect()


def _ensure_wifi():
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        if not sta.active():
            sta.active(True)
        if sta.isconnected():
            return True
        try:
            import wifi_event
            res = wifi_event.connect()
            return bool(res.get("ok"))
        except Exception as e:
            print("p2c: wifi_event err:", e)
            return False
    except Exception as e:
        print("p2c: ensure_wifi err:", e)
        return False


def _speaker_reinit():
    """Force a full Speaker re-init. Cardputer's mic and speaker share
    the same I2S bus; after M5.Mic.end() the bus is left in mic-side
    state and a plain Speaker.begin() doesn't reclaim it cleanly —
    symptom is "first playback works, every subsequent one is silent
    or crackles". end() + begin() with a short gap forces the driver
    to fully reset the I2S peripheral."""
    try: M5.Speaker.end()
    except Exception: pass
    time.sleep_ms(60)
    try: M5.Speaker.begin()
    except Exception as e: print("p2c: spk.begin warn:", e)
    try: M5.Speaker.setVolume(180)
    except Exception: pass


def _beep_start():
    """Short tone right before mic capture begins so the user has an
    unambiguous "start speaking now" cue. M5.Mic.begin() inside
    _record_to_file is what flips the device from "showing the dots
    while UI catches up" to "actually capturing samples"; without an
    audible marker users tend to start talking too early and the
    first syllable is lost. We stop the speaker before returning so
    the amp is idle by the time the mic powers up."""
    try:
        _speaker_reinit()
        M5.Speaker.tone(1200, 80)
        time.sleep_ms(120)
        try: M5.Speaker.stop()
        except Exception: pass
    except Exception as e:
        print("p2c: beep err:", e)


def _record_to_file(kb):
    """Capture audio to ``_AUDIO_PATH`` using ``M5.Mic.recordWavFile``.

    Why we use recordWavFile rather than chunked record():
    on-device probing showed that ``M5.Mic.record(buf, rate, True)``
    returns essentially silent buffers (every sample == -8) on this
    UIFlow build — likely a binding mismatch with the underlying
    M5Unified API. ``recordWavFile`` does the capture inside the
    firmware and writes a properly-formed 16-bit PCM mono WAV file
    that Whisper transcribes correctly.

    Trade-off: recordWavFile is fixed-duration. There's no clean way
    to stop early and end up with a valid WAV (truncating the file
    leaves the header's data-size field wrong). So tap-to-stop is
    gone; we record for ``_MAX_SECONDS`` and the user just waits.

    Returns the number of audio samples captured (0 on error)."""
    try:
        os.remove(_AUDIO_PATH)
    except OSError:
        pass

    M5.Mic.begin()
    try:
        try:
            M5.Mic.setSampleRate(_RATE)
        except Exception as e:
            print("p2c: setSampleRate warn:", e)

        # Kick off the recording. recordWavFile is async — it returns
        # immediately and isRecording() reports completion.
        # NOTE: firmware interprets (path, duration, rate) NOT (path, rate, duration).
        # Passing (path, _RATE=16000, _MAX_SECONDS=6) wrote rate=6 Hz in the
        # WAV header because the firmware read the 2nd arg as duration and the
        # 3rd as sample_rate. Correct order is (path, duration_s, sample_rate).
        try:
            M5.Mic.recordWavFile(_AUDIO_PATH, _MAX_SECONDS, _RATE)
        except Exception as e:
            print("p2c: recordWavFile err:", e)
            return 0

        # Drive the LCD heartbeat while the firmware records. Poll
        # isRecording every ~120 ms; bail if it runs long over the
        # expected duration (firmware may have failed silently).
        deadline = time.ticks_add(
            time.ticks_ms(), (_MAX_SECONDS + 2) * 1000,
        )
        last_phase = -1
        last_ms = 0
        while M5.Mic.isRecording():
            now = time.ticks_ms()
            if time.ticks_diff(now, deadline) > 0:
                # Force-stop a runaway recording.
                try:
                    M5.Mic.end()
                except Exception:
                    pass
                break
            if time.ticks_diff(now, last_ms) >= 120:
                # Phase derived from elapsed-ish time so the dot
                # walks at a steady pace regardless of firmware
                # cadence.
                phase = (now // 360) % _DOT_COUNT
                if phase != last_phase:
                    _draw_recording_dots(phase)
                    last_phase = phase
                last_ms = now
            # Honor Q/ESC even mid-recording — better to abort and
            # let the user retry than to wedge the device.
            kb.tick()
            k = kb.get_key()
            if k is not None and _is_exit(k):
                try:
                    M5.Mic.end()
                except Exception:
                    pass
                raise KeyboardInterrupt()
            time.sleep_ms(40)
    finally:
        try:
            M5.Mic.end()
        except Exception as e:
            print("p2c: mic.end warn:", e)

    try:
        size = os.stat(_AUDIO_PATH)[6]
    except OSError:
        return 0
    # Subtract 44-byte WAV header to get sample-count estimate.
    return max(0, (size - 44) // _BYTES_PER_SAMPLE)


def _https_post_file_stream(url, file_path, headers, chunk_size=2048, timeout_s=60):
    """File-streamed HTTPS POST.

    The audio body never lives in a Python bytes object; we read it
    from disk in fixed-size chunks straight into a reusable buffer
    and write that to the SSL socket. Memory peak during upload is
    roughly ``chunk_size + response_buffer + TLS_state`` ≈ 35 KB.

    Critical detail confirmed by an on-device probe: kwargs DO work
    on ``ssl.wrap_socket`` on this UIFlow build (a previous TypeError
    we observed must have come from elsewhere). So SNI lands cleanly
    via ``server_hostname=host`` and Cloudflare routes the request.

    Returns ``(status, body_bytes)``.
    """
    import socket
    import ssl as _ssl

    if not url.startswith("https://"):
        raise RuntimeError("only https supported")
    rest = url[len("https://"):]
    slash = rest.find("/")
    if slash == -1:
        host_port, http_path = rest, "/"
    else:
        host_port, http_path = rest[:slash], rest[slash:]
    if ":" in host_port:
        host, port_str = host_port.split(":", 1)
        port = int(port_str)
    else:
        host, port = host_port, 443

    file_size = os.stat(file_path)[6]

    # Force a clean heap before mbedTLS allocates its working memory.
    gc.collect()
    gc.collect()

    addr = socket.getaddrinfo(host, port)[0][-1]
    s = socket.socket()
    try:
        s.settimeout(timeout_s)
    except Exception:
        pass
    s.connect(addr)
    ss = _ssl.wrap_socket(s, server_hostname=host)

    try:
        head = (
            "POST {} HTTP/1.1\r\n"
            "Host: {}\r\n"
            "User-Agent: m5-cardputer\r\n"
            "Content-Length: {}\r\n"
            "Connection: close\r\n"
        ).format(http_path, host, file_size)
        for k, v in headers.items():
            head += "{}: {}\r\n".format(k, v)
        head += "\r\n"
        ss.write(head.encode())

        buf = bytearray(chunk_size)
        with open(file_path, "rb") as f:
            while True:
                got = f.readinto(buf)
                if not got:
                    break
                if got < chunk_size:
                    ss.write(memoryview(buf)[:got])
                else:
                    ss.write(buf)

        # Read response — small JSON, cap at 8 KB.
        resp = bytearray()
        rb = bytearray(512)
        while len(resp) < 8192:
            try:
                g = ss.readinto(rb)
            except OSError:
                break
            if not g:
                break
            resp += rb[:g]
        raw = bytes(resp)
    finally:
        try:
            ss.close()
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass

    sep = raw.find(b"\r\n\r\n")
    if sep == -1:
        raise RuntimeError("malformed http response")
    head_text = raw[:sep].decode("utf-8", "replace")
    body_bytes = raw[sep + 4:]
    first_line = head_text.split("\r\n", 1)[0]
    parts = first_line.split(" ", 2)
    if len(parts) < 2:
        raise RuntimeError("bad status line: " + first_line)
    return int(parts[1]), body_bytes



def _post_reset():
    """Clear server-side conversation history for this device. Quick
    fire-and-forget; uses requests since the body is empty."""
    _free_internal_ram()
    import requests
    headers = {"x-device-secret": DEVICE_SECRET}
    try:
        r = requests.post(WORKER_RESET_URL, headers=headers, timeout=15)
        try:
            return r.status_code == 200
        finally:
            try:
                r.close()
            except Exception:
                pass
    except Exception as e:
        print("p2c: reset err:", e)
        return False


def _post_text(prompt):
    """POST a typed prompt to /ask-text. Returns the parsed JSON dict.
    Body is tiny so the OOM concern doesn't really apply, but we still
    free internal RAM first to keep behavior consistent."""
    _free_internal_ram()
    import json as _json
    body = _json.dumps({"prompt": prompt}).encode()
    gc.collect()

    import requests
    headers = {
        "content-type": "application/json",
        "x-device-secret": DEVICE_SECRET,
    }
    r = requests.post(WORKER_TEXT_URL, data=body, headers=headers, timeout=45)
    try:
        if r.status_code != 200:
            raise RuntimeError(
                "worker {}: {}".format(r.status_code, r.text[:120]),
            )
        return r.json()
    finally:
        try:
            r.close()
        except Exception:
            pass


def _get_menu():
    """GET /menu → parsed dict {ok, now, cards}. Response is ~2 KB so
    requests.get is fine here (no streaming needed). Raises on error."""
    _free_internal_ram()
    import requests
    headers = {"x-device-secret": DEVICE_SECRET}
    r = requests.get(WORKER_MENU_URL, headers=headers, timeout=20)
    try:
        if r.status_code != 200:
            raise RuntimeError(
                "menu {}: {}".format(r.status_code, r.text[:120]),
            )
        return r.json()
    finally:
        try:
            r.close()
        except Exception:
            pass


def _post_play_meta(key):
    """POST /play with ``inline_audio=False`` so the response stays small
    (a few hundred bytes). We deliberately do NOT receive audio_base64
    inline — materializing a ~650 KB JSON body in the MicroPython heap
    would OOM. Actual playback goes through tts_helper.play_tts which
    streams the WAV file directly to /flash."""
    _free_internal_ram()
    import json as _json
    body = _json.dumps({"key": key, "inline_audio": False}).encode()
    gc.collect()
    import requests
    headers = {
        "content-type": "application/json",
        "x-device-secret": DEVICE_SECRET,
    }
    r = requests.post(WORKER_PLAY_URL, data=body, headers=headers, timeout=30)
    try:
        if r.status_code != 200:
            raise RuntimeError(
                "play {}: {}".format(r.status_code, r.text[:120]),
            )
        return r.json()
    finally:
        try:
            r.close()
        except Exception:
            pass


def _menu_fire(card_meta, kb=None):
    """Play one briefing card end-to-end. Best-effort; never raises.
    Returns True on a successful playback.

    kb: 호출자의 MatrixKeyboard. tts_helper 가 같은 인스턴스로 cancel
        키 폴링해야 매트릭스 큐 분기 없이 ESC/Q 잡힌다.
    """
    key = card_meta.get("key", "")
    label = card_meta.get("label", "")
    _draw_uploading("Loading", label[:30])
    try:
        data = _post_play_meta(key)
    except Exception as e:
        msg = "play err: {}".format(str(e)[:80])
        print("p2c:", msg)
        _draw_error(msg)
        time.sleep_ms(1500)
        return False
    speak_ko = (data.get("speak_ko") or "").strip()
    if not speak_ko:
        _draw_uploading("(no content)", label[:30])
        time.sleep_ms(900)
        return False
    _draw_uploading("Playing - ESC/Q cancel", label[:24])
    played_ok = False
    try:
        import tts_helper
        # play_tts 는 정상 완료 True / 사용자 ESC·Q 취소 False / 실패 False
        played_ok = bool(tts_helper.play_tts(
            WORKER_TTS_URL, speak_ko, DEVICE_SECRET, cancel_kb=kb))
    except Exception as e:
        print("p2c: menu tts err:", e)
    gc.collect()
    return played_ok


def _post_recording():
    """Read the captured WAV and POST it. Returns the parsed JSON dict
    on success; raises on any failure (including an empty file).

    mbedTLS on this MicroPython build draws its working memory from
    internal RAM only, regardless of PSRAM availability. A few rounds
    of gc.collect() before opening the TLS connection meaningfully
    shrinks the heap fragmentation that otherwise OOMs us during the
    handshake."""
    try:
        size = os.stat(_AUDIO_PATH)[6]
    except OSError as e:
        raise RuntimeError("no audio file: {}".format(e))
    if size <= 44:
        raise RuntimeError("empty recording")

    _free_internal_ram()

    file_size = os.stat(_AUDIO_PATH)[6]
    _draw_uploading("uploading", "{} KB".format(file_size // 1024))

    headers = {
        "content-type": "audio/wav",
        "x-device-secret": DEVICE_SECRET,
    }
    _draw_uploading("transcribing", "speaking to Whisper")
    status, resp_body = _https_post_file_stream(
        WORKER_URL, _AUDIO_PATH, headers, chunk_size=2048, timeout_s=60,
    )
    gc.collect()
    _draw_uploading("got reply", "decoding")

    if status != 200:
        snippet = resp_body[:160].decode("utf-8", "replace")
        raise RuntimeError("worker {}: {}".format(status, snippet))
    import json as _json
    return _json.loads(resp_body)


# ---- MAIN -----------------------------------------------------------

def run():
    print("p2c: run() enter")
    _set_font()
    if not _WORKER_BASE or not DEVICE_SECRET:
        _draw_error(
            "Not configured.\n"
            "Copy apps/config.example.py\nto apps/config.py\n"
            "and fill in WORKER_BASE\n+ DEVICE_SECRET."
        )
        kb = MatrixKeyboard()
        while True:
            kb.tick()
            if _is_exit(kb.get_key()):
                return
            time.sleep_ms(50)
    wifi_ok = _ensure_wifi()
    print("p2c: wifi_ok=", wifi_ok)
    _draw_idle(wifi_ok)
    kb = MatrixKeyboard()
    time.sleep_ms(400)
    # Drain any keys still in the matrix queue from the launcher's
    # Enter press / wifi splash — without this, a stale ESC/Enter
    # fires the first iteration of the main loop and returns instantly,
    # which the user observes as "selected the app, saw wifi message,
    # then back to launcher menu".
    for _drain_i in range(25):
        kb.tick()
        _stale = kb.get_key()
        if _stale is not None:
            print("p2c: drained stale key:", repr(_stale))
        time.sleep_ms(20)

    state = "idle"
    text_buf = ""
    cursor_on = True
    last_blink_ms = 0
    # Showing state: keep the result around so we can re-render at a
    # different scroll offset without a fresh API round-trip.
    last_transcript = ""
    last_response = ""
    scroll = 0
    # Briefing-menu state. menu_items=None means "needs a /menu fetch
    # on next loop iteration" — used both for initial entry and for
    # refresh after a card finishes playing.
    menu_items = None
    menu_selected = 0
    menu_scroll = 0
    menu_now = ""
    menu_status = None
    jump_buf = ""
    jump_ts = 0

    try:
        while True:
            kb.tick()
            k = kb.get_key()

            # ESC always exits — but ONLY in non-typing/non-menu states.
            # In typing mode ESC returns to idle, and in menu mode ESC
            # returns to idle, so the user can recover from either
            # without a full reboot.
            if state not in ("typing", "menu") and _is_exit(k):
                print("p2c: exit via _is_exit, state=", state, "k=", repr(k))
                return

            if state == "idle":
                if _is_space(k):
                    state = "recording"
                    gc.collect()
                    _draw_recording_initial()
                    _draw_recording_dots(0)
                    _beep_start()
                    try:
                        _record_to_file(kb)
                    except KeyboardInterrupt:
                        return
                    state = "uploading"
                    _draw_uploading()
                    try:
                        result = _post_recording()
                        last_transcript = result.get("transcript", "")
                        last_response = result.get("response", "")
                        scroll = 0
                        state = "showing"
                        _draw_result(last_transcript, last_response, scroll)
                    except Exception as e:
                        msg = str(e)[:200]
                        print("p2c: post err:", msg)
                        state = "error"
                        _draw_error(msg)
                    # Clean up the file regardless. Frees ~1 MB.
                    try:
                        os.remove(_AUDIO_PATH)
                    except OSError:
                        pass
                    gc.collect()
                    if state == "showing" and last_response:
                        try:
                            import tts_helper
                            tts_helper.play_tts(WORKER_TTS_URL, last_response, DEVICE_SECRET)
                        except Exception as _te:
                            print("p2c: tts skip:", _te)
                        gc.collect()

                elif _is_text_trigger(k):
                    state = "typing"
                    text_buf = ""
                    cursor_on = True
                    last_blink_ms = time.ticks_ms()
                    _draw_typing(text_buf, cursor_on)

                elif _is_menu_trigger(k):
                    state = "menu"
                    menu_items = None
                    menu_selected = 0
                    menu_scroll = 0
                    menu_status = None
                    jump_buf = ""
                    jump_ts = 0

                elif _is_new_chat(k):
                    cleared = _post_reset()
                    msg = "memory cleared" if cleared else "reset failed"
                    _draw_idle(wifi_ok, status_msg=msg)
                    time.sleep_ms(900)
                    _draw_idle(wifi_ok)

            elif state == "typing":
                # ESC in typing mode → back to idle (NOT exit).
                if k is not None and isinstance(k, int) and k == 0x1B:
                    state = "idle"
                    text_buf = ""
                    wifi_ok = _ensure_wifi()
                    _draw_idle(wifi_ok)
                elif _is_enter(k):
                    if text_buf.strip():
                        state = "uploading"
                        _draw_uploading()
                        try:
                            result = _post_text(text_buf.strip())
                            last_transcript = result.get("transcript", "")
                            last_response = result.get("response", "")
                            scroll = 0
                            state = "showing"
                            _draw_result(last_transcript, last_response, scroll)
                        except Exception as e:
                            msg = str(e)[:200]
                            print("p2c: text-post err:", msg)
                            state = "error"
                            _draw_error(msg)
                        text_buf = ""
                        gc.collect()
                        if state == "showing" and last_response:
                            try:
                                import tts_helper
                                tts_helper.play_tts(WORKER_TTS_URL, last_response, DEVICE_SECRET)
                            except Exception as _te:
                                print("p2c: tts skip:", _te)
                            gc.collect()
                elif _is_backspace(k):
                    if text_buf:
                        text_buf = text_buf[:-1]
                        _draw_typing(text_buf, cursor_on)
                else:
                    ch = _printable_char(k)
                    if ch is not None and len(text_buf) < 240:
                        text_buf += ch
                        _draw_typing(text_buf, cursor_on)

                # Blink the caret on a 500 ms cycle. ONLY redraw if
                # we're still in typing state — Enter triggers a state
                # change to uploading/showing/error inside this branch
                # and we must not overwrite the result/error screen on
                # the way out.
                if state == "typing":
                    now = time.ticks_ms()
                    if time.ticks_diff(now, last_blink_ms) >= 500:
                        cursor_on = not cursor_on
                        last_blink_ms = now
                        _draw_typing(text_buf, cursor_on)

            elif state == "menu":
                now = time.ticks_ms()
                # First entry (or refresh after a play): pull the card
                # list from the server. menu_items=None is the sentinel.
                if menu_items is None:
                    try:
                        data = _get_menu()
                        menu_items = data.get("cards", []) or []
                        ts = (data.get("now", "") or "")
                        # ISO timestamp → "HH:MM" (positions 11..16).
                        menu_now = ts[11:16] if len(ts) >= 16 else ""
                    except Exception as e:
                        print("p2c: menu fetch err:", e)
                        menu_items = []
                        menu_status = "menu err: " + str(e)[:24]
                    # Keep selection within bounds after a refresh.
                    if menu_selected >= len(menu_items):
                        menu_selected = max(0, len(menu_items) - 1)
                    if menu_scroll > max(0, len(menu_items) - _MENU_ROWS_VISIBLE):
                        menu_scroll = max(0, len(menu_items) - _MENU_ROWS_VISIBLE)
                    _draw_menu(menu_items, menu_selected, menu_scroll,
                               menu_now, jump_buf, menu_status)
                    time.sleep_ms(40)
                    continue

                # ESC → back to idle.
                if k is not None and isinstance(k, int) and k == 0x1B:
                    state = "idle"
                    menu_items = None
                    jump_buf = ""
                    menu_status = None
                    wifi_ok = _ensure_wifi()
                    _draw_idle(wifi_ok)
                    time.sleep_ms(40)
                    continue

                handled = False
                if k is not None:
                    intent = _scroll_intent(k)
                    if intent is not None:
                        if intent == "up" and menu_selected > 0:
                            menu_selected -= 1
                        elif intent == "down" and menu_selected < len(menu_items) - 1:
                            menu_selected += 1
                        # Keep the selected row inside the scroll window.
                        if menu_selected < menu_scroll:
                            menu_scroll = menu_selected
                        elif menu_selected >= menu_scroll + _MENU_ROWS_VISIBLE:
                            menu_scroll = menu_selected - _MENU_ROWS_VISIBLE + 1
                        jump_buf = ""
                        menu_status = None
                        _draw_menu(menu_items, menu_selected, menu_scroll,
                                   menu_now, jump_buf, menu_status)
                        handled = True
                    elif _is_enter(k):
                        if menu_items:
                            picked = menu_items[menu_selected]
                            label = picked.get("label", "")
                            ok = _menu_fire(picked, kb=kb)
                            menu_items = None  # force refresh next loop
                            jump_buf = ""
                            menu_status = (
                                "played: " if ok else "cancelled: "
                            ) + label[:18]
                        handled = True

                if not handled and k is not None:
                    ch = _printable_char(k)
                    if ch is not None:
                        lo = ch.lower()
                        # Only alpha chars feed the shortcut buffer —
                        # card keys are letter-only (mb, mr, dr, de, g, e,
                        # t, w, wr, we). Other printables are ignored.
                        if "a" <= lo <= "z":
                            jump_buf = (jump_buf + lo)[-2:]
                            jump_ts = now
                            keys = [c.get("key", "") for c in menu_items]
                            matches = [
                                i for i, k0 in enumerate(keys)
                                if k0.startswith(jump_buf)
                            ]
                            if matches:
                                menu_selected = matches[0]
                                if menu_selected < menu_scroll:
                                    menu_scroll = menu_selected
                                elif menu_selected >= menu_scroll + _MENU_ROWS_VISIBLE:
                                    menu_scroll = menu_selected - _MENU_ROWS_VISIBLE + 1
                                # Unambiguous + exact key match → fire
                                # immediately. (Just one prefix-match
                                # alone isn't enough: 'w' uniquely
                                # prefixes 'wr'/'we' too, so we must
                                # also check equality.)
                                if len(matches) == 1 and jump_buf in keys:
                                    picked = menu_items[menu_selected]
                                    label = picked.get("label", "")
                                    ok = _menu_fire(picked, kb=kb)
                                    menu_items = None
                                    jump_buf = ""
                                    menu_status = (
                                        "played: " if ok else "cancelled: "
                                    ) + label[:18]
                            else:
                                # No match — try the new char alone
                                # (user probably mistyped the first
                                # letter and is starting over).
                                jump_buf = lo
                                matches = [
                                    i for i, k0 in enumerate(keys)
                                    if k0.startswith(jump_buf)
                                ]
                                if matches:
                                    menu_selected = matches[0]
                                else:
                                    jump_buf = ""
                            _draw_menu(menu_items or [], menu_selected,
                                       menu_scroll, menu_now, jump_buf,
                                       menu_status)

                # Jump-buffer timeout: e.g. user typed 'w' (which is an
                # exact key, but also a prefix of 'wr'/'we'). We wait
                # 800 ms for a refinement char; if none comes, commit
                # to the exact match.
                if (menu_items and jump_buf
                        and time.ticks_diff(now, jump_ts) > 800):
                    keys = [c.get("key", "") for c in menu_items]
                    if jump_buf in keys:
                        idx = keys.index(jump_buf)
                        picked = menu_items[idx]
                        label = picked.get("label", "")
                        ok = _menu_fire(picked, kb=kb)
                        menu_items = None
                        menu_status = (
                            "played: " if ok else "cancelled: "
                        ) + label[:18]
                    jump_buf = ""
                    if state == "menu":
                        _draw_menu(menu_items or [], menu_selected,
                                   menu_scroll, menu_now, jump_buf,
                                   menu_status)

            elif state == "showing":
                if _is_new_chat(k):
                    cleared = _post_reset()
                    state = "idle"
                    wifi_ok = _ensure_wifi()
                    _draw_idle(
                        wifi_ok,
                        status_msg="memory cleared" if cleared else "reset failed",
                    )
                    time.sleep_ms(900)
                    _draw_idle(wifi_ok)
                    time.sleep_ms(40)
                    continue
                intent = _scroll_intent(k)
                if intent is not None:
                    # Recompute layout each scroll keypress — it's cheap
                    # (just text-width measurements) and lets us keep
                    # exactly one source of truth (_result_layout) for
                    # scroll bounds.
                    _, r_lines, _, max_visible = _result_layout(
                        last_transcript, last_response,
                    )
                    max_scroll = max(0, len(r_lines) - max_visible)
                    if intent == "up":
                        new_scroll = max(0, scroll - 1)
                    else:
                        new_scroll = min(max_scroll, scroll + 1)
                    if new_scroll != scroll:
                        scroll = new_scroll
                        _draw_result(last_transcript, last_response, scroll)
                elif _is_space(k):
                    state = "idle"
                    wifi_ok = _ensure_wifi()
                    _draw_idle(wifi_ok)
                elif _is_text_trigger(k):
                    state = "typing"
                    text_buf = ""
                    cursor_on = True
                    last_blink_ms = time.ticks_ms()
                    _draw_typing(text_buf, cursor_on)

            elif state == "error":
                if _is_space(k):
                    state = "idle"
                    wifi_ok = _ensure_wifi()
                    _draw_idle(wifi_ok)
                elif _is_text_trigger(k):
                    state = "typing"
                    text_buf = ""
                    cursor_on = True
                    last_blink_ms = time.ticks_ms()
                    _draw_typing(text_buf, cursor_on)
                elif _is_new_chat(k):
                    cleared = _post_reset()
                    state = "idle"
                    wifi_ok = _ensure_wifi()
                    _draw_idle(
                        wifi_ok,
                        status_msg="memory cleared" if cleared else "reset failed",
                    )
                    time.sleep_ms(900)
                    _draw_idle(wifi_ok)

            time.sleep_ms(40)
    finally:
        try:
            M5.Mic.end()
        except Exception:
            pass
        try:
            _LCD.fillScreen(_BLACK)
        except Exception:
            pass
        time.sleep_ms(200)
        machine.reset()


run()
