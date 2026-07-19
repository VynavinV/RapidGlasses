"""QNX-side eye tracking service (runs on the Raspberry Pi 5).

Headless: no windows, no GUI deps. Reads the ESP32-S3 IR stream, runs the
Tracker from main.py (detection + Kalman + overlays), and pushes each
annotated frame plus a JSON metrics header to the Flask server on the laptop:

    POST {EYE_SERVER_URL}/eye/ingest   body = JPEG, X-Metrics = json

Round 1 (pupil check): once a pupil is locked, diameters are collected for
ROUND1_SECONDS; the median is compared against [PUPIL_MIN_PX, PUPIL_MAX_PX].
The verdict rides along in every metrics packet as `round1` — the webapp
polls it via /eye/snapshot and decides whether to abort to the report.

Config: eye_config.json next to this file (see repo copy). Env vars with the
same uppercase names override it, e.g. EYE_STREAM_URL, PUPIL_MIN_PX.

Needs only: opencv (core/imgproc/imgcodecs — no GUI, no video IO backend),
numpy, requests. The MJPEG stream is parsed over plain HTTP, so QNX opencv
builds without highgui/ffmpeg work as-is.
"""
import json
import os
import time

import cv2
import numpy as np
import requests

from main import Tracker

_HERE = os.path.dirname(os.path.abspath(__file__))
_cfg = {}
_cfg_path = os.path.join(_HERE, "eye_config.json")
if os.path.exists(_cfg_path):
    with open(_cfg_path, encoding="utf-8") as fh:
        _cfg = json.load(fh)
    print(f"config loaded: {_cfg_path}")


def cfg(key, default):
    """env EYE_STREAM_URL beats json "stream_url" beats the default."""
    return os.environ.get(key.upper(), _cfg.get(key, default))


STREAM_URL = cfg("eye_stream_url", "http://10.94.64.101:81/stream")
SERVER_URL = cfg("eye_server_url", "http://localhost:3001")
INGEST_URL = SERVER_URL + "/eye/ingest"

JPEG_QUALITY = 70
ROUND1_SECONDS = float(cfg("round1_seconds", 8.0))
PUPIL_MIN_PX = float(cfg("pupil_min_px", 14))   # constricted below this
PUPIL_MAX_PX = float(cfg("pupil_max_px", 80))   # blown above this


def mjpeg_frames(sess, url):
    """Yield BGR frames from an MJPEG-over-HTTP stream.

    Pure requests + cv2.imdecode: works on QNX opencv builds that have no
    video IO backend (ffmpeg/gstreamer) and no GUI. JPEGs are cut out of the
    byte stream at their SOI/EOI markers (ffd8/ffd9), which is all the ESP32
    MJPEG framing needs. Raises requests exceptions when the stream drops.
    """
    resp = sess.get(url, stream=True, timeout=(3.05, 10))
    resp.raise_for_status()
    buf = b""
    for chunk in resp.iter_content(chunk_size=8192):
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
        if len(buf) > 1_000_000:   # corrupt stream guard: drop and resync
            buf = b""


def run_session(frames, sess):
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
            sess.post(INGEST_URL, data=jpg.tobytes(),
                      headers={"Content-Type": "image/jpeg",
                               "X-Metrics": json.dumps(m)},
                      timeout=1.0)
            send_fail_logged = False
        except requests.RequestException:
            if not send_fail_logged:
                print(f"cannot reach server at {INGEST_URL} (retrying quietly)")
                send_fail_logged = True


def main():
    sess = requests.Session()
    while True:
        try:
            print(f"connecting to {STREAM_URL} -> {INGEST_URL}")
            run_session(mjpeg_frames(sess, STREAM_URL), sess)
            print("stream ended")
        except requests.RequestException as exc:
            print(f"stream error: {exc}")
        time.sleep(2)


if __name__ == "__main__":
    main()
