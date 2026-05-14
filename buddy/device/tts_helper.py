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


def play_tts(url, text, device_secret):
    """Fetch TTS and play through the speaker. Best-effort; never raises."""
    if not text:
        return False
    _reset_log()
    _log("tts: start text_len=", len(text))
    try:
        attrs = [a for a in dir(M5.Speaker) if not a.startswith("_")]
        _log("tts: speaker attrs=", attrs)
    except Exception as e:
        _log("tts: dir err:", e)
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
        try: M5.Speaker.begin()
        except Exception as e: _log("tts: spk.begin:", e)
        try: M5.Speaker.setVolume(64)
        except Exception as e: _log("tts: spk.vol:", e)
        import time as _t

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
        for name in ("playWavFile", "playWav", "playWAV"):
            fn = getattr(M5.Speaker, name, None)
            if fn is None:
                continue
            try:
                rc = fn(TTS_PATH)
                _log("tts: called", name, "rc=", rc)
                _t.sleep_ms(ms_estimate)
                try:
                    busy = M5.Speaker.isPlaying()
                    _log("tts:", name, "isPlaying after sleep=", busy)
                except Exception: pass
                return True
            except Exception as e:
                _log("tts:", name, "err:", e)

        # SLOW PATH — only attempt if file-based call wasn't available.
        # Free as much heap as we can first.
        gc.collect()
        try: _log("tts: slow-path mem_free=", gc.mem_free())
        except Exception: pass
        try:
            with open(TTS_PATH, "rb") as f:
                f.read(44)
                pcm = f.read()
        except Exception as e:
            _log("tts: read err:", e)
            return False
        sample_count = len(pcm) // 2
        _log("tts: pcm bytes=", len(pcm), "samples=", sample_count)
        attempts = (
            lambda: M5.Speaker.playRaw(pcm, sample_count, 16000, False, 1, 0),
            lambda: M5.Speaker.playRaw(pcm, sample_count, 16000),
            lambda: M5.Speaker.playRaw(pcm, 16000),
            lambda: M5.Speaker.playRaw(pcm),
        )
        for i, fn in enumerate(attempts):
            try:
                fn()
                _log("tts: playRaw OK sig", i)
                _t.sleep_ms(ms_estimate)
                return True
            except Exception as e:
                _log("tts: playRaw sig", i, "err:", e)
        return False
    except Exception as e:
        _log("tts: play err:", e)
        return False
