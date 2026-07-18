"""Head-sway tracking.

A background thread reads the webcam, locates one reference point on the face
per frame (nose tip via MediaPipe FaceLandmarker), and keeps a running mean of
that point since the last /tracking/start. The per-frame Euclidean distance
from that mean is the sway value. Nothing is streamed — QNX polls
/tracking/snapshot (~30Hz) and reads whatever the loop last stored.

Uses the mediapipe `tasks` API; the legacy `solutions.face_mesh` module is
gone in current mediapipe. That API needs a model file on disk — see
MODEL_PATH, fetched from Google's mediapipe-models bucket.
"""
import math
import os
import threading
import time
from collections import deque

from flask import Blueprint, jsonify

tracking_bp = Blueprint("tracking", __name__)

CAMERA_INDEX = int(os.environ.get("TRACKING_CAMERA_INDEX", "0"))
MODEL_PATH = os.environ.get("TRACKING_MODEL_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "models", "face_landmarker.task")
NOSE_TIP = 1              # FaceLandmarker landmark index for the nose tip
FPS_WINDOW = 30           # frames used to measure the loop rate

# Local debug view (TRACKING_DEBUG=1). Off by default: QNX never needs it, and
# imshow costs frames. The camera is exclusive, so this has to be drawn from
# inside the tracking loop — a separate viewer process can't open it.
DEBUG_WINDOW = os.environ.get("TRACKING_DEBUG", "").lower() in ("1", "true", "yes")
DEBUG_TITLE = "RapidGlasses Head Sway"
SWAY_GAIN = 12            # sway is a few % of frame; magnify it to be visible
DEV_HISTORY = 240

_lock = threading.Lock()
_thread = None
_stop = threading.Event()

# Everything below _lock is read by request threads and written by the loop.
_state = {
    "x": None,
    "y": None,
    "mean_x": None,
    "mean_y": None,
    "deviation": None,
    "timestamp": None,    # when x/y were measured, not when polled
    "fps": 0.0,
    "face": False,
    "running": False,
    "error": None,
}


def _reset_locked():
    _state.update(x=None, y=None, mean_x=None, mean_y=None, deviation=None,
                  timestamp=None, fps=0.0, face=False, error=None)


def _draw_debug(cv2, np, frame, pt, mean, deviation, fps, n, hist):
    """Annotate a frame with the sway reading. Debug only — never sent anywhere."""
    GREEN, CYAN, AMBER, GRAY = (0, 255, 120), (255, 255, 0), (0, 180, 255), (150, 150, 150)
    FONT = cv2.FONT_HERSHEY_SIMPLEX
    h, w = frame.shape[:2]

    if mean is not None:
        mx, my = int(mean[0] * w), int(mean[1] * h)
        # baseline: where the head has averaged since /tracking/start
        cv2.drawMarker(frame, (mx, my), CYAN, cv2.MARKER_CROSS, 22, 1)
        cv2.circle(frame, (mx, my), 3, CYAN, -1)

    if pt is not None:
        px, py = int(pt[0] * w), int(pt[1] * h)
        cv2.circle(frame, (px, py), 6, GREEN, -1)
        if mean is not None:
            cv2.line(frame, (mx, my), (px, py), AMBER, 1)
            # magnified sway vector — raw offset is only a few pixels
            gx = int(mx + (px - mx) * SWAY_GAIN)
            gy = int(my + (py - my) * SWAY_GAIN)
            cv2.arrowedLine(frame, (mx, my), (gx, gy), AMBER, 2, tipLength=0.18)
            cv2.circle(frame, (mx, my), int(deviation * SWAY_GAIN * max(w, h)),
                       AMBER, 1)

    # header
    cv2.rectangle(frame, (0, 0), (w, 62), (24, 24, 24), -1)
    face_txt = "TRACKING" if pt is not None else "NO FACE"
    cv2.putText(frame, face_txt, (12, 24), FONT, 0.6,
                GREEN if pt is not None else (0, 0, 255), 2)
    dev_txt = f"{deviation:.4f}" if deviation is not None else "--"
    cv2.putText(frame, f"sway {dev_txt}", (150, 24), FONT, 0.6, AMBER, 2)
    cv2.putText(frame, f"{fps:5.1f} fps   n={n}   gain x{SWAY_GAIN}",
                (12, 48), FONT, 0.45, GRAY, 1)

    # sway trace along the bottom
    if len(hist) > 1:
        gh, gy0 = 70, h - 78
        cv2.rectangle(frame, (0, gy0), (w, gy0 + gh), (24, 24, 24), -1)
        hi = max(max(hist), 1e-4)
        pts = [(int(i / (len(hist) - 1) * w),
                gy0 + gh - int(v / hi * (gh - 8)) - 4)
               for i, v in enumerate(hist)]
        cv2.polylines(frame, [np.array(pts, np.int32)], False, GREEN, 1)
        cv2.putText(frame, f"peak {hi:.4f}", (12, gy0 + 14), FONT, 0.4, GRAY, 1)
    return frame


def _loop():
    cap = landmarker = None
    n = 0                 # frames folded into the mean since start
    sum_x = sum_y = 0.0
    stamps = deque(maxlen=FPS_WINDOW)

    try:
        # Imported here so the server still boots (minus tracking) without them.
        import cv2
        import mediapipe as mp
        import numpy as np
        from mediapipe.tasks.python.core.base_options import BaseOptions
        from mediapipe.tasks.python.vision import (FaceLandmarker,
                                                   FaceLandmarkerOptions,
                                                   RunningMode)

        if not os.path.exists(MODEL_PATH):
            raise FileNotFoundError(f"face landmark model missing: {MODEL_PATH}")

        cap = cv2.VideoCapture(CAMERA_INDEX)
        if not cap.isOpened():
            raise RuntimeError(f"could not open camera {CAMERA_INDEX}")

        landmarker = FaceLandmarker.create_from_options(FaceLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=MODEL_PATH),
            running_mode=RunningMode.VIDEO,
            num_faces=1,
        ))

        misses = 0
        hist = deque(maxlen=DEV_HISTORY)
        while not _stop.is_set():
            ok, frame = cap.read()
            if not ok:
                # Don't spin hot on a camera that stops delivering.
                misses += 1
                if misses > 100:
                    raise RuntimeError("camera stopped delivering frames")
                _stop.wait(0.01)
                continue
            misses = 0
            now = time.time()
            stamps.append(now)

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            res = landmarker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb),
                int(now * 1000))    # VIDEO mode wants ms, strictly increasing

            fps = 0.0
            if len(stamps) > 1:
                span = stamps[-1] - stamps[0]
                if span > 0:
                    fps = (len(stamps) - 1) / span

            if not res.face_landmarks:
                # Leave the last good point and its timestamp in place so QNX
                # can tell how stale the reading is; just flag the miss.
                with _lock:
                    _state["face"] = False
                    _state["fps"] = fps
                if DEBUG_WINDOW:
                    mean = (sum_x / n, sum_y / n) if n else None
                    cv2.imshow(DEBUG_TITLE, _draw_debug(
                        cv2, np, frame, None, mean, None, fps, n, hist))
                    cv2.waitKey(1)
                continue

            lm = res.face_landmarks[0][NOSE_TIP]
            x, y = float(lm.x), float(lm.y)   # already normalized 0..1

            n += 1
            sum_x += x
            sum_y += y
            mean_x, mean_y = sum_x / n, sum_y / n
            deviation = math.hypot(x - mean_x, y - mean_y)

            with _lock:
                _state.update(x=x, y=y, mean_x=mean_x, mean_y=mean_y,
                              deviation=deviation, timestamp=stamps[-1],
                              fps=fps, face=True)

            if DEBUG_WINDOW:
                hist.append(deviation)
                cv2.imshow(DEBUG_TITLE, _draw_debug(
                    cv2, np, frame, (x, y), (mean_x, mean_y), deviation,
                    fps, n, hist))
                cv2.waitKey(1)   # required to pump the HighGUI event loop
    except BaseException as exc:
        # Never die silently — a dead thread with running=True would leave QNX
        # polling stale values forever.
        with _lock:
            _state["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if DEBUG_WINDOW:
            try:
                import cv2
                cv2.destroyWindow(DEBUG_TITLE)
                cv2.waitKey(1)
            except Exception:
                pass
        if landmarker is not None:
            landmarker.close()
        if cap is not None:
            cap.release()
        with _lock:
            _state["running"] = False


@tracking_bp.route("/tracking/start", methods=["POST"])
def start():
    global _thread
    with _lock:
        already = _thread is not None and _thread.is_alive()
        _reset_locked()       # new test window -> new baseline mean
        _state["running"] = True

    if already:
        return jsonify(status="running", restarted=False)

    _stop.clear()
    _thread = threading.Thread(target=_loop, name="tracking", daemon=True)
    _thread.start()
    # Camera open + model load can take ~10s, so don't block the caller waiting
    # to see if it worked — failures land in snapshot's `error`/`running`.
    return jsonify(status="starting", restarted=True)


@tracking_bp.route("/tracking/stop", methods=["POST"])
def stop():
    global _thread
    _stop.set()
    if _thread is not None:
        _thread.join(timeout=3.0)
        _thread = None
    with _lock:
        _state["running"] = False
    return jsonify(status="stopped")


@tracking_bp.route("/tracking/snapshot", methods=["GET"])
def snapshot():
    """Pure read of the shared state — no frame work happens here."""
    with _lock:
        return jsonify(dict(_state))
