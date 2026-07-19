"""Eye-tracker bridge blueprint.

The QNX Pi (eye_tracker.py) POSTs annotated JPEG frames with a JSON metrics
header to /eye/ingest. This module just stores the latest of each and keeps a
small history for the final report:
  - /eye/video     MJPEG relay for the webapp (<img> src)
  - /eye/snapshot  latest metrics + connected flag (webapp polls round 1 here)
  - summary()      pupil stats + a few sampled frames, for gemini_report.py
"""
import base64
import json
import threading
import time
from collections import deque

from flask import Blueprint, Response, jsonify, request

eye_bp = Blueprint("eye", __name__)

STALE_AFTER = 3.0        # seconds without an ingest -> "not connected"
SAMPLE_GAP = 5.0         # keep one frame every N seconds for the report
MAX_SAMPLES = 8
MAX_DIAMS = 4000         # ~3 min of history at 20fps

_lock = threading.Lock()
_jpeg = None             # latest annotated frame
_metrics = {}
_recv_at = 0.0
_samples = deque(maxlen=MAX_SAMPLES)   # (ts, jpeg)
_diams = deque(maxlen=MAX_DIAMS)


@eye_bp.route("/eye/ingest", methods=["POST"])
def ingest():
    global _jpeg, _metrics, _recv_at
    jpg = request.get_data()
    try:
        m = json.loads(request.headers.get("X-Metrics", "{}"))
    except json.JSONDecodeError:
        m = {}
    now = time.time()
    with _lock:
        _jpeg = jpg
        _metrics = m
        _recv_at = now
        if m.get("active") and m.get("diam"):
            _diams.append(m["diam"])
        if not _samples or now - _samples[-1][0] >= SAMPLE_GAP:
            _samples.append((now, jpg))
    return "", 204


@eye_bp.route("/eye/snapshot")
def snapshot():
    with _lock:
        m = dict(_metrics)
        age = time.time() - _recv_at if _recv_at else None
    m["connected"] = age is not None and age < STALE_AFTER
    m["age"] = round(age, 2) if age is not None else None
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
    """Everything the report builder needs: pupil stats, round-1 verdict,
    and up to 4 base64 sample frames (they carry the trail/trend overlays)."""
    with _lock:
        diams = list(_diams)
        samples = list(_samples)
        m = dict(_metrics)
    out = {"round1": m.get("round1"), "last_metrics": m}
    if diams:
        s = sorted(diams)
        out["pupil_px"] = {
            "mean": round(sum(diams) / len(diams), 1),
            "median": round(s[len(s) // 2], 1),
            "min": round(s[0], 1),
            "max": round(s[-1], 1),
            "samples": len(diams),
        }
    out["images_b64"] = [base64.b64encode(j).decode()
                         for _, j in samples[-4:]]
    return out
