"""TTS download + playback helper. Loaded lazily from push_to_claude
so the parent module stays small at launcher import time.

Flow: POST {text} JSON -> server streams back WAV -> we write it to
/flash/.tts.wav in chunks -> M5.Speaker.playWav() plays it.

The HTTP response is read with the same low-RAM raw-socket pattern as
the audio upload path (no Python bytes object holds the WAV body).
"""

import gc
import os
import time

import M5


TTS_PATH = "/flash/.tts.wav"
TTS_LOG = "/flash/.tts.log"
_MAX_BYTES = 600_000


def _log(*parts):
    """Print to serial AND append to /flash/.tts.log so we can
    recover diagnostics over USB after BLE has locked CDC."""
    msg = " ".join(str(p) for p in parts)
    try:
        print(msg)
    except Exception:
        pass
    try:
        with open(TTS_LOG, "a") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _reset_log():
    try:
        with open(TTS_LOG, "w") as f:
            f.write("")
    except Exception:
        pass


def _post_recv_file(url, json_body, file_path, headers,
                    chunk_size=2048, timeout_s=45):
    """POST a small JSON body, stream response into file_path.

    Assumes Content-Length response (Flask dev server default). Returns
    (status, bytes_written) on success."""
    import socket
    import ssl as _ssl
    import json as _json

    if not url.startswith("https://"):
        raise RuntimeError("https only")
    rest = url[len("https://"):]
    slash = rest.find("/")
    host_port, http_path = (rest, "/") if slash == -1 else (rest[:slash], rest[slash:])
    if ":" in host_port:
        host, port = host_port.split(":", 1); port = int(port)
    else:
        host, port = host_port, 443

    body = _json.dumps(json_body).encode()
    gc.collect()

    addr = socket.getaddrinfo(host, port)[0][-1]
    s = socket.socket()
    try: s.settimeout(timeout_s)
    except Exception: pass
    s.connect(addr)
    ss = _ssl.wrap_socket(s, server_hostname=host)

    received = 0
    status = 0
    try:
        head = (
            "POST {} HTTP/1.1\r\nHost: {}\r\n"
            "User-Agent: m5\r\nContent-Length: {}\r\nConnection: close\r\n"
        ).format(http_path, host, len(body))
        for k, v in headers.items():
            head += "{}: {}\r\n".format(k, v)
        head += "\r\n"
        ss.write(head.encode())
        ss.write(body)

        head_buf = bytearray()
        rb = bytearray(chunk_size)
        sep = -1
        while True:
            try: got = ss.readinto(rb)
            except OSError: break
            if not got: break
            head_buf += rb[:got]
            sep = head_buf.find(b"\r\n\r\n")
            if sep != -1: break
            if len(head_buf) > 4096:
                raise RuntimeError("header too big")
        if sep == -1:
            raise RuntimeError("bad response")

        first = bytes(head_buf[:sep]).split(b"\r\n", 1)[0].decode("utf-8", "replace")
        parts = first.split(" ", 2)
        status = int(parts[1]) if len(parts) >= 2 else 0

        body_start = bytes(head_buf[sep + 4:])
        if status != 200:
            return status, body_start[:200]

        with open(file_path, "wb") as f:
            if body_start:
                f.write(body_start); received = len(body_start)
            while received < _MAX_BYTES:
                try: got = ss.readinto(rb)
                except OSError: break
                if not got: break
                if got < chunk_size:
                    f.write(bytes(rb[:got]))
                else:
                    f.write(bytes(rb))
                received += got
        return status, received
    finally:
        try: ss.close()
        except Exception: pass
        try: s.close()
        except Exception: pass


def play_tts(url, text, device_secret, cancel_kb=None):
    """Fetch TTS and play through the speaker. Best-effort; never raises.

    cancel_kb: 호출자의 MatrixKeyboard 인스턴스. 전달되면 재생 중 ESC/Q
        감지 시 즉시 stop() + return False. 매트릭스 polling 의 owner 가
        호출자라 새 인스턴스 만들면 키 이벤트가 한쪽에만 들어와 cancel
        놓치는 문제를 회피한다. None 이면 cancel 지원 안 함 (정상 재생).
    """
    if not text:
        return False
    _reset_log()
    _log("tts: start text_len=", len(text))
    # 매 호출마다 dir() 수집은 production 에서 노이즈 + GC 압력. 제거.
    try:
        os.remove(TTS_PATH)
    except OSError:
        pass
    try:
        gc.collect()
        status, info = _post_recv_file(
            url, {"text": text}, TTS_PATH,
            {"content-type": "application/json",
             "x-device-secret": device_secret},
        )
    except Exception as e:
        _log("tts: fetch err:", e)
        return False
    if status != 200:
        _log("tts: status", status, info)
        return False
    try:
        # Full I2S re-init. Cardputer mic & speaker share the I2S bus;
        # after recording, a plain begin() doesn't reclaim it cleanly
        # and we get "first playback works, the rest crackle". end()
        # + 60 ms gap + begin() forces the driver to fully reset.
        import time as _t
        try: M5.Speaker.end()
        except Exception: pass
        _t.sleep_ms(60)
        try: M5.Speaker.begin()
        except Exception as e: _log("tts: spk.begin:", e)
        # 255 saturates the speaker amp on Cardputer-Adv (audible
        # crackle); 180 is the practical clipping-free max.
        try: M5.Speaker.setVolume(180)
        except Exception as e: _log("tts: spk.vol:", e)

        # File size + free-heap snapshot for diagnostics.
        try:
            wav_size = os.stat(TTS_PATH)[6]
        except Exception as e:
            _log("tts: stat err:", e)
            return False
        try:
            _log("tts: wav bytes=", wav_size, "mem_free=", gc.mem_free())
        except Exception:
            _log("tts: wav bytes=", wav_size)

        # Estimate playback duration from WAV size: 16kHz mono 16-bit
        # = 32000 B/s. Used to sleep until playback finishes.
        ms_estimate = (wav_size - 44) * 1000 // 32000 + 400

        # FAST PATH — let the binding stream the WAV from disk so we
        # never have to materialize ~150 KB of PCM in MicroPython heap
        # (which OOMs at ~32 KB on this firmware).
        #
        # Speaker hygiene: M5.Speaker leaves PCM in its internal buffer
        # after a playback completes. A naive second call overlays new
        # samples onto that residue and the result reaches the user as
        # noise. Bracket every playWavFile with stop() (clear residue)
        # and poll isPlaying() to land precisely on end-of-clip rather
        # than guessing with a fixed sleep that either cuts the tail
        # (mechanical chirp at clip end) or returns while the buffer
        # is still draining.
        try: M5.Speaker.stop()
        except Exception: pass
        _t.sleep_ms(50)
        # 호출자 kb 가 있으면 그것을 사용 — 새 인스턴스 만들면 매트릭스
        # 큐가 분기되어 키 이벤트 일부가 호출자 쪽으로 가 cancel 놓침.
        _cancel_kb = cancel_kb
        _log("tts: cancel_kb=", _cancel_kb)
        for name in ("playWavFile", "playWav", "playWAV"):
            fn = getattr(M5.Speaker, name, None)
            if fn is None:
                continue
            try:
                rc = fn(TTS_PATH)
                _log("tts: called", name, "rc=", rc)
                # Poll until the firmware reports playback complete,
                # bounded by 1.5 s past the size-based estimate so a
                # broken isPlaying() can't wedge us.
                waited = 0
                budget = ms_estimate + 1500
                cancelled = False
                while waited < budget:
                    _t.sleep_ms(60)
                    waited += 60
                    try:
                        if not M5.Speaker.isPlaying():
                            break
                    except Exception:
                        pass
                    # ESC(0x1B) 또는 'q'/'Q' → 즉시 중단
                    if _cancel_kb is not None:
                        try:
                            _cancel_kb.tick()
                            k = _cancel_kb.get_key()
                        except Exception:
                            k = None
                        if k is not None:
                            if isinstance(k, int):
                                if 0x20 <= k <= 0x7E:
                                    k = chr(k)
                            is_esc = (k == 0x1B)
                            is_q = (isinstance(k, str)
                                    and k and k.lower() == "q")
                            if is_esc or is_q:
                                cancelled = True
                                break
                # Let the final ~150 ms of audio drain out of the DAC
                # before we cut the amp; otherwise the last syllable
                # is truncated mid-sample and the user hears a click.
                if not cancelled:
                    _t.sleep_ms(150)
                try: M5.Speaker.stop()
                except Exception: pass
                _log("tts: done", name, "waited_ms=", waited,
                     "cancelled=", cancelled)
                return not cancelled
            except Exception as e:
                _log("tts:", name, "err:", e)

        # SLOW PATH 비활성화 — 전체 PCM 을 RAM 에 적재하면 ~32KB 한도
        # 넘어 OOM 보장. UIFlow MicroPython 빌드에는 playWavFile 이
        # 항상 있어 이 경로는 도달 안 함. 만약 도달했다면 펌웨어 회귀.
        _log("tts: no playWav* method available, fast-path missing — abort")
        return False
    except Exception as e:
        _log("tts: play err:", e)
        return False
