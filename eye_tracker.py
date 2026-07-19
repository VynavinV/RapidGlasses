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
Needs only: opencv, numpy, requests.
"""
import json
import os
import time

import cv2
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


def run_session(cap, sess):
    """One connected stream session. Returns when the stream drops."""
    tracker = Tracker()
    r1_start = None
    r1_diams = []
    round1 = {"done": False}
    send_fail_logged = False

    while True:
        ok, frame = cap.read()
        if not ok:
            print("stream dropped")
            return

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
        cap = cv2.VideoCapture(STREAM_URL)
        if not cap.isOpened():
            print(f"cannot open stream {STREAM_URL}, retrying in 2s")
            time.sleep(2)
            continue
        print(f"streaming from {STREAM_URL} -> {INGEST_URL}")
        try:
            run_session(cap, sess)
        finally:
            cap.release()
        time.sleep(2)


if __name__ == "__main__":
    main()
