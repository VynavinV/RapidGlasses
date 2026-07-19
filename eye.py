"""Laptop-side eye bridge.

The QNX Pi runs eye_tracker.py as a server and announces itself with a UDP
beacon on port 8131. This module listens for that beacon (set EYE_TRACKER_URL
in .env to pin an address instead, e.g. if broadcast is blocked), pulls the
Pi's annotated MJPEG + metrics in background threads, and re-serves them to
the webapp on the same routes as always:

    /eye/video      MJPEG relay for the <img> tags
    /eye/snapshot   latest metrics (browser polls round 1 here)
    summary()       proxies the Pi's /eye/summary for the report builder

The browser never talks to the Pi directly; only this Flask app does.
"""
import json
import os
import socket
import threading
import time

import requests
from flask import Blueprint, Response, jsonify

eye_bp = Blueprint("eye", __name__)

BEACON_PORT = 8131
DEFAULT_TRACKER_PORT = 8130
STALE_AFTER = 3.0

_lock = threading.Lock()
_tracker_url = os.environ.get("EYE_TRACKER_URL") or None
_jpeg = None
_metrics = {}
_metrics_ts = 0.0
_started = False


def _get_url():
    with _lock:
        return _tracker_url


def _discover_loop():
    """Learn the Pi's address from its UDP beacon. Skipped entirely when
    EYE_TRACKER_URL is pinned in the environment."""
    global _tracker_url
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", BEACON_PORT))
    while True:
        data, addr = s.recvfrom(1024)
        try:
            msg = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            continue
        if msg.get("name") != "rapidglasses-eye":
            continue
        url = f"http://{addr[0]}:{msg.get('port', DEFAULT_TRACKER_PORT)}"
        with _lock:
            if url != _tracker_url:
                print(f"eye tracker found: {url}")
            _tracker_url = url


def _video_loop():
    """Hold one MJPEG connection to the Pi and keep the latest frame."""
    global _jpeg
    while True:
        url = _get_url()
        if not url:
            time.sleep(1)
            continue
        try:
            resp = requests.get(url + "/eye/video", stream=True,
                                timeout=(3.05, 10))
            buf = b""
            for chunk in resp.iter_content(chunk_size=8192):
                buf += chunk
                while True:
                    soi = buf.find(b"\xff\xd8")
                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if soi < 0 or eoi < 0:
                        break
                    jpg, buf = buf[soi:eoi + 2], buf[eoi + 2:]
                    with _lock:
                        _jpeg = jpg
                if len(buf) > 1_000_000:
                    buf = b""
        except requests.RequestException:
            pass
        time.sleep(2)


def _metrics_loop():
    """Poll the Pi's snapshot a few times a second."""
    global _metrics, _metrics_ts
    while True:
        url = _get_url()
        if url:
            try:
                m = requests.get(url + "/eye/snapshot", timeout=1).json()
                with _lock:
                    _metrics = m
                    _metrics_ts = time.time()
            except (requests.RequestException, ValueError):
                pass
        time.sleep(0.25)


@eye_bp.route("/eye/snapshot")
def snapshot():
    with _lock:
        m = dict(_metrics)
        url = _tracker_url
        fresh = _metrics_ts and time.time() - _metrics_ts < STALE_AFTER
    if not fresh:
        m = {"connected": False}       # Pi unreachable (or not found yet)
    m["tracker"] = url                 # Pi's own `connected` = ESP32 fresh
    return jsonify(m)


@eye_bp.route("/eye/video")
def video():
    """MJPEG relay of the annotated eye view from the Pi."""
    def gen():
        while True:
            with _lock:
                jpg = _jpeg
            if jpg is not None:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                       + jpg + b"\r\n")
            time.sleep(0.05)
    return Response(gen(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


def summary():
    """Report-builder data, fetched straight from the Pi."""
    url = _get_url()
    if not url:
        return {}
    try:
        return requests.get(url + "/eye/summary", timeout=10).json()
    except (requests.RequestException, ValueError):
        return {}


def _start():
    global _started
    if _started:
        return
    _started = True
    if not os.environ.get("EYE_TRACKER_URL"):
        threading.Thread(target=_discover_loop, daemon=True).start()
    threading.Thread(target=_video_loop, daemon=True).start()
    threading.Thread(target=_metrics_loop, daemon=True).start()


_start()
