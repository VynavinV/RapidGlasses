"""RapidGlasses eye tracker.

PuRe-style pupil detection (the algorithm class used by robust open-source
eye trackers): normalize lighting, remove specular glints, detect edges,
fit ellipses to edge segments, and score each candidate by how pupil-like it
is (round, closed, dark interior, bright surround). A Kalman filter smooths
the pupil center for stable gaze tracking. Overlays show pupil dilation and
gaze deviation.
"""
import cv2
import numpy as np
from collections import deque

STREAM_URL = "http://10.94.64.101:81/stream"

# ---------------- detection tuning ----------------
GLINT_THRESH = 180       # brightness above which a spot is treated as glint
MIN_AREA = 150           # min pupil contour area (px^2)
MAX_AREA_FRAC = 0.4      # reject blobs bigger than this frac of the frame
MIN_ASPECT = 0.45        # min minor/major axis ratio (roundness)
MIN_FIT = 0.55           # min contour<->ellipse area agreement
MIN_CONTRAST = 8         # pupil interior must be this much darker than around

# ---------------- tracking / display ----------------
TRAIL_LEN = 50
DILATION_HISTORY = 180
FONT = cv2.FONT_HERSHEY_SIMPLEX
GREEN, CYAN, AMBER, GRAY = (0, 255, 120), (0, 255, 255), (0, 180, 255), (150, 150, 150)


def make_kalman():
    kf = cv2.KalmanFilter(4, 2)
    kf.transitionMatrix = np.array([[1, 0, 1, 0], [0, 1, 0, 1],
                                    [0, 0, 1, 0], [0, 0, 0, 1]], np.float32)
    kf.measurementMatrix = np.array([[1, 0, 0, 0], [0, 1, 0, 0]], np.float32)
    kf.processNoiseCov = np.eye(4, dtype=np.float32) * 0.03
    kf.measurementNoiseCov = np.eye(2, dtype=np.float32) * 0.4
    return kf


def detect_pupil(gray):
    """PuRe-style pupil detection -> (ellipse, glint) or (None, glint)."""
    h, w = gray.shape
    clahe = cv2.createCLAHE(3.0, (8, 8))
    eq = clahe.apply(gray)
    blur = cv2.medianBlur(eq, 5)

    # locate + remove specular glints so they don't break the pupil edge
    _, glint_mask = cv2.threshold(blur, GLINT_THRESH, 255, cv2.THRESH_BINARY)
    glint_mask = cv2.dilate(glint_mask, np.ones((5, 5), np.uint8))
    filled = cv2.inpaint(blur, glint_mask, 5, cv2.INPAINT_TELEA)

    edges = cv2.Canny(filled, 30, 90)
    edges = cv2.dilate(edges, np.ones((2, 2), np.uint8))

    cnts, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    frame_area = h * w
    best = None
    for c in cnts:
        if len(c) < 5:
            continue
        area = cv2.contourArea(c)
        if area < MIN_AREA or area > MAX_AREA_FRAC * frame_area:
            continue
        try:
            ell = cv2.fitEllipse(c)
        except cv2.error:
            continue
        (ex, ey), (MA, ma), _ = ell
        if MA <= 0 or ma <= 0:
            continue
        aspect = ma / MA
        if aspect < MIN_ASPECT:
            continue
        ell_area = np.pi * (MA / 2) * (ma / 2)
        fit = min(area, ell_area) / max(area, ell_area)
        if fit < MIN_FIT:
            continue

        cx, cy, r = int(ex), int(ey), int((MA + ma) / 4)
        if not (0 <= cx < w and 0 <= cy < h) or r < 6:
            continue
        # reject candidates hugging the frame border (vignette/edge artifacts)
        m = r + 4
        if cx - m < 0 or cy - m < 0 or cx + m >= w or cy + m >= h:
            continue

        inner = np.zeros_like(gray)
        cv2.circle(inner, (cx, cy), max(3, r - 3), 255, -1)
        outer = np.zeros_like(gray)
        cv2.circle(outer, (cx, cy), int(r * 1.8), 255, -1)
        cv2.circle(outer, (cx, cy), int(r * 1.2), 0, -1)
        i_mean = cv2.mean(gray, mask=inner)[0]
        o_mean = cv2.mean(gray, mask=outer)[0]
        contrast = o_mean - i_mean
        if contrast < MIN_CONTRAST:
            continue

        darkness = (255 - i_mean) / 255
        # mild center bias: pupil sits near frame middle, not jammed in a corner
        cdist = np.hypot(cx - w / 2, cy - h / 2) / np.hypot(w / 2, h / 2)
        center = 1.0 - 0.5 * cdist          # 1.0 at center -> 0.5 at corner
        score = fit * aspect * (contrast / 255 + 0.1) * (0.4 + darkness) * center
        if best is None or score > best[0]:
            best = (score, ell)

    # find the glint that lies inside the chosen pupil (for display)
    glint = None
    if best is not None:
        (ex, ey), (MA, ma), _ = best[1]
        r = (MA + ma) / 4
        gc, _ = cv2.findContours(glint_mask, cv2.RETR_EXTERNAL,
                                 cv2.CHAIN_APPROX_SIMPLE)
        for c in gc:
            m = cv2.moments(c)
            if m["m00"] == 0:
                continue
            gx, gy = m["m10"] / m["m00"], m["m01"] / m["m00"]
            if np.hypot(gx - ex, gy - ey) < r * 1.3:
                glint = (int(gx), int(gy))
                break
    return (best[1] if best else None), glint


def draw_graph(canvas, hist, x, y, w, h):
    cv2.rectangle(canvas, (x, y), (x + w, y + h), (40, 40, 40), -1)
    cv2.rectangle(canvas, (x, y), (x + w, y + h), (90, 90, 90), 1)
    if len(hist) < 2:
        return
    lo, hi = min(hist), max(hist)
    rng = max(hi - lo, 1e-3)
    pts = [(x + int(i / (len(hist) - 1) * w),
            y + h - int((v - lo) / rng * (h - 6)) - 3)
           for i, v in enumerate(hist)]
    cv2.polylines(canvas, [np.array(pts, np.int32)], False, GREEN, 1)


def build_panel(h, pw, active, diam, pct, dev, dx, dy, hist):
    # scale HUD layout to the actual panel height so nothing overlaps
    s = h / 240.0
    def y(v):
        return int(v * s)
    fs = 0.42 * s          # small font
    fb = 0.72 * s          # big font
    panel = np.full((h, pw, 3), 24, np.uint8)
    cv2.putText(panel, "EYE TRACKER", (12, y(26)), FONT, 0.5 * s, GREEN, 2)
    cv2.line(panel, (12, y(36)), (pw - 12, y(36)), (60, 60, 60), 1)
    if active:
        rows = [("PUPIL", f"{diam:.1f} px", GREEN),
                ("DILATION vs base", f"{pct:+.1f} %", AMBER if pct < 0 else CYAN),
                (f"GAZE DEV  (dx {dx:+.0f}  dy {dy:+.0f})",
                 f"{dev:.0f} px", AMBER)]
        yy = 56
        for label, val, col in rows:
            cv2.putText(panel, label, (12, y(yy)), FONT, fs, GRAY, 1)
            cv2.putText(panel, val, (12, y(yy + 17)), FONT, fb, col, 2)
            yy += 44
    else:
        cv2.putText(panel, "searching...", (12, y(56)), FONT, 0.5 * s,
                    (0, 0, 255), 1)
    gh = y(44)
    gy = h - gh - y(14)
    cv2.putText(panel, "DILATION TREND", (12, gy - y(8)), FONT, fs, GRAY, 1)
    draw_graph(panel, list(hist), 12, gy, pw - 24, gh)
    return panel


def main():
    cap = cv2.VideoCapture(STREAM_URL)
    if not cap.isOpened():
        print(f"Failed to open stream: {STREAM_URL}")
        return
    print("Streaming... press ESC or 'q' to quit.")

    WIN = "RapidGlasses Eye Tracker"
    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, 1280, 720)

    kf = make_kalman()
    inited = False
    miss = 0
    trail = deque(maxlen=TRAIL_LEN)
    hist = deque(maxlen=DILATION_HISTORY)
    baseline = None

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Failed to read frame from stream.")
            break

        h, w = frame.shape[:2]
        fcx, fcy = w // 2, h // 2
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        ell, glint = detect_pupil(gray)
        pred = kf.predict() if inited else None

        diam = pct = dev = dx = dy = 0.0
        active = False
        if ell is not None:
            (ex, ey), (MA, ma), ang = ell
            diam = (MA + ma) / 2.0
            meas = np.array([[np.float32(ex)], [np.float32(ey)]])
            if not inited:
                kf.statePost = np.array([[ex], [ey], [0], [0]], np.float32)
                inited = True
                miss = 0
            else:
                far = np.hypot(ex - pred[0, 0], ey - pred[1, 0])
                if far < max(diam * 1.5, 45):
                    kf.correct(meas)              # normal update
                    miss = 0
                else:
                    miss += 1                     # detection disagrees w/ lock
                    if miss >= 4:                 # stuck -> re-acquire here
                        kf.statePost = np.array(
                            [[ex], [ey], [0], [0]], np.float32)
                        trail.clear()
                        miss = 0

        if inited:
            active = True
            cx = float(kf.statePost[0, 0])
            cy = float(kf.statePost[1, 0])
            icx, icy = int(cx), int(cy)
            trail.append((icx, icy))

            if ell is not None:
                cv2.ellipse(frame, ((cx, cy), (MA, ma), ang), GREEN, 2)
                cv2.circle(frame, (icx, icy), 3, (0, 0, 255), -1)
                if glint is not None:
                    cv2.circle(frame, glint, 4, CYAN, 1)

            dx, dy = cx - fcx, cy - fcy
            dev = float(np.hypot(dx, dy))
            cv2.drawMarker(frame, (fcx, fcy), GRAY, cv2.MARKER_CROSS, 16, 1)
            cv2.arrowedLine(frame, (fcx, fcy), (icx, icy), AMBER, 1,
                            tipLength=0.18)
            for i in range(1, len(trail)):
                cv2.line(frame, trail[i - 1], trail[i], (255, 160, 0), 1)

            if ell is not None:
                hist.append(diam)
                baseline = diam if baseline is None else \
                    0.985 * baseline + 0.015 * diam
                pct = (diam - baseline) / baseline * 100.0

        # scale the eye view up to a crisp fixed height, build HUD to match
        disp_h = 640
        scale = disp_h / h
        big = cv2.resize(frame, (int(w * scale), disp_h),
                         interpolation=cv2.INTER_NEAREST)
        panel = build_panel(disp_h, 320, active, diam, pct, dev, dx, dy, hist)
        cv2.imshow(WIN, np.hstack([big, panel]))
        key = cv2.waitKey(1) & 0xFF
        if key == 27 or key == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
