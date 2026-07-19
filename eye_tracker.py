"""QNX-side eye tracking server (runs on the Raspberry Pi 5).

Zero-config apart from the ESP32 stream URL: run `python3 eye_tracker.py`
and everything is served from this box. The laptop (eye.py in secondcheck)
finds it automatically via the UDP beacon and pulls what it needs:

    GET /eye/video      annotated MJPEG (pupil ellipse, trail, HUD panel)
    GET /eye/snapshot   latest metrics JSON incl. round-1 verdict

Nothing is stored on this box: frames and metrics are relayed and the
laptop (eye.py) accumulates the pupil history and report samples locally.

Beacon: broadcasts {"name": "rapidglasses-eye", "port": 8130} on UDP 8131
every 2s so no IPs need configuring on the laptop.

Round 1 (pupil check): once a pupil is locked, diameters are collected for
round1_seconds; the median is compared against [pupil_min_px, pupil_max_px]
and the verdict rides in every snapshot as `round1`.

Config: eye_config.json next to this file. Env vars with the same uppercase
names override it, e.g. EYE_STREAM_URL.

Headless. Dependencies: opencv (core/imgproc/imgcodecs — no GUI, no video IO
backend) and numpy only; all networking is Python stdlib.
"""
import json
import os
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

from main import Tracker

HTTP_PORT = 8130
BEACON_PORT = 8131
JPEG_QUALITY = 70
STALE_AFTER = 3.0        # no ESP32 frame for this long -> connected: false

_HERE = os.path.dirname(os.path.abspath(__file__))
_cfg = {}
_cfg_path = os.path.join(_HERE, "eye_config.json")
if os.path.exists(_cfg_path):
    with open(_cfg_path, encoding="utf-8") as fh:
        _cfg = json.load(fh)
    print(f"config loaded: {_cfg_path}")


def cfg(key, default):
    """env EYE_STREAM_URL beats json "eye_stream_url" beats the default."""
    return os.environ.get(key.upper(), _cfg.get(key, default))


STREAM_URL = cfg("eye_stream_url", "http://10.94.64.101:81/stream")
ROUND1_SECONDS = float(cfg("round1_seconds", 5.0))
PUPIL_MIN_PX = float(cfg("pupil_min_px", 14))   # constricted below this
PUPIL_MAX_PX = float(cfg("pupil_max_px", 80))   # blown above this

# ---- shared state: written by the tracker loop, read by HTTP handlers ----
_lock = threading.Lock()
_jpeg = None
_metrics = {}
_frame_ts = 0.0


def mjpeg_frames(url):
    """Yield BGR frames from an MJPEG-over-HTTP stream.

    urllib + cv2.imdecode: works on QNX opencv builds that have no video IO
    backend and no GUI. JPEGs are cut at their SOI/EOI markers (ffd8/ffd9).
    Raises OSError when the stream drops.
    """
    resp = urllib.request.urlopen(url, timeout=10)
    buf = b""
    while True:
        chunk = resp.read(8192)
        if not chunk:
            return                     # stream ended cleanly
        buf += chunk
        while True:
            soi = buf.find(b"\xff\xd8")
            eoi = buf.find(b"\xff\xd9", soi + 2)
            if soi < 0 or eoi < 0:
                break
            jpg, buf = buf[soi:eoi + 2], buf[eoi + 2:]
            frame = cv2.imdecode(np.frombuffer(jpg, np.uint8),
                                 cv2.IMREAD_COLOR)
            if frame is not None:
                yield frame
        if len(buf) > 1_000_000:       # corrupt stream guard: drop and resync
            buf = b""


def run_session():
    """One ESP32 connection: track, annotate, publish to shared state.
    Returns/raises when the stream drops; round 1 restarts with the stream."""
    global _jpeg, _metrics, _frame_ts
    tracker = Tracker()
    r1_start = None
    r1_diams = []
    round1 = {"done": False}

    for frame in mjpeg_frames(STREAM_URL):
        display, m = tracker.process(frame)
        now = time.time()

        if not round1["done"] and m["active"] and m["diam"] > 0:
            if r1_start is None:
                r1_start = now
            r1_diams.append(m["diam"])
            if now - r1_start >= ROUND1_SECONDS:
                med = sorted(r1_diams)[len(r1_diams) // 2]
                round1 = {
                    "done": True,
                    "median_px": round(med, 1),
                    "min_px": PUPIL_MIN_PX,
                    "max_px": PUPIL_MAX_PX,
                    "abnormal": med < PUPIL_MIN_PX or med > PUPIL_MAX_PX,
                }
                print(f"round1: median {med:.1f}px "
                      f"{'ABNORMAL' if round1['abnormal'] else 'normal'}")

        m["round1"] = round1
        m["ts"] = now

        ok, jpg = cv2.imencode(".jpg", display,
                               [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            continue
        with _lock:
            _jpeg = jpg.tobytes()
            _metrics = m
            _frame_ts = now


def tracker_loop():
    while True:
        try:
            print(f"connecting to {STREAM_URL}")
            run_session()
            print("stream ended")
        except OSError as exc:
            print(f"stream error: {exc}")
        time.sleep(2)


def beacon_loop():
    """Announce this tracker on the LAN so the laptop needs no config.
    Also sent to localhost so same-machine testing works."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    msg = json.dumps({"name": "rapidglasses-eye", "port": HTTP_PORT}).encode()
    while True:
        for dest in ("255.255.255.255", "127.0.0.1"):
            try:
                s.sendto(msg, (dest, BEACON_PORT))
            except OSError:
                pass
        time.sleep(2)


def _snapshot():
    with _lock:
        m = dict(_metrics)
        age = time.time() - _frame_ts if _frame_ts else None
    m["connected"] = age is not None and age < STALE_AFTER
    m["age"] = round(age, 2) if age is not None else None
    return m


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/eye/snapshot"):
            self._json(_snapshot())
        elif self.path.startswith("/eye/video"):
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    with _lock:
                        jpg = _jpeg
                    if jpg is not None:
                        self.wfile.write(
                            b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                            + jpg + b"\r\n")
                    time.sleep(0.05)
            except OSError:
                pass               # viewer disconnected
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass                       # keep the log to tracker events only


def main():
    threading.Thread(target=tracker_loop, daemon=True).start()
    threading.Thread(target=beacon_loop, daemon=True).start()
    print(f"serving on 0.0.0.0:{HTTP_PORT} "
          f"(beacon on udp {BEACON_PORT})")
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
