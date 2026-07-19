"""QNX-side eye tracking service (runs on the Raspberry Pi 5).

Headless: no windows, no GUI deps. Reads the ESP32-S3 IR stream, runs the
Tracker from main.py (detection + Kalman + overlays), and pushes each
annotated frame plus a JSON metrics header to the Flask server on the laptop:

    POST {eye_server_url}/eye/ingest   body = JPEG, X-Metrics = json

Round 1 (pupil check): once a pupil is locked, diameters are collected for
round1_seconds; the median is compared against [pupil_min_px, pupil_max_px].
The verdict rides along in every metrics packet as `round1` — the webapp
polls it via /eye/snapshot and decides whether to abort to the report.

Config: eye_config.json next to this file (see repo copy). Env vars with the
same uppercase names override it, e.g. EYE_STREAM_URL, PUPIL_MIN_PX.

Dependencies: opencv (core/imgproc/imgcodecs — no GUI, no video IO backend)
and numpy only. All networking is Python stdlib (urllib/http.client), so no
pip packages are needed on QNX.
"""
import json
import os
import time
import urllib.request
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlparse

import cv2
import numpy as np

from main import Tracker

NetworkError = (OSError, HTTPException)   # covers URLError, timeouts, resets

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
SERVER_URL = cfg("eye_server_url", "http://localhost:3001")
INGEST_URL = SERVER_URL + "/eye/ingest"

JPEG_QUALITY = 70
ROUND1_SECONDS = float(cfg("round1_seconds", 8.0))
PUPIL_MIN_PX = float(cfg("pupil_min_px", 14))   # constricted below this
PUPIL_MAX_PX = float(cfg("pupil_max_px", 80))   # blown above this


def mjpeg_frames(url):
    """Yield BGR frames from an MJPEG-over-HTTP stream.

    urllib + cv2.imdecode: works on QNX opencv builds that have no video IO
    backend (ffmpeg/gstreamer) and no GUI. JPEGs are cut out of the byte
    stream at their SOI/EOI markers (ffd8/ffd9), which is all the ESP32
    MJPEG framing needs. Raises OSError/URLError when the stream drops.
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


class IngestSender:
    """POSTs frames over one persistent http.client connection, reconnecting
    on failure. stdlib replacement for a requests.Session."""

    def __init__(self, url):
        u = urlparse(url)
        self.host = u.hostname
        self.port = u.port or 80
        self.path = u.path
        self.conn = None

    def send(self, jpg_bytes, metrics):
        if self.conn is None:
            self.conn = HTTPConnection(self.host, self.port, timeout=1.0)
        try:
            self.conn.request("POST", self.path, body=jpg_bytes,
                              headers={"Content-Type": "image/jpeg",
                                       "X-Metrics": json.dumps(metrics)})
            self.conn.getresponse().read()
        except NetworkError:
            self.conn.close()
            self.conn = None
            raise


def run_session(frames, sender):
    """One connected stream session. Returns/raises when the stream drops."""
    tracker = Tracker()
    r1_start = None
    r1_diams = []
    round1 = {"done": False}
    send_fail_logged = False

    for frame in frames:
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
        try:
            sender.send(jpg.tobytes(), m)
            send_fail_logged = False
        except NetworkError:
            if not send_fail_logged:
                print(f"cannot reach server at {INGEST_URL} (retrying quietly)")
                send_fail_logged = True


def main():
    sender = IngestSender(INGEST_URL)
    while True:
        try:
            print(f"connecting to {STREAM_URL} -> {INGEST_URL}")
            run_session(mjpeg_frames(STREAM_URL), sender)
            print("stream ended")
        except NetworkError as exc:
            print(f"stream error: {exc}")
        time.sleep(2)


if __name__ == "__main__":
    main()
