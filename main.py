import cv2
import numpy as np
from collections import deque

STREAM_URL = "http://10.94.64.101:81/stream"

# ---------------- tuning ----------------
DARK_PERCENTILE = 8      # pupil pixels are within the darkest N% of the eye
MIN_R = 8                # min pupil radius (px)
MAX_R = 120              # max pupil radius (px)
TRAIL_LEN = 60
DILATION_HISTORY = 200   # samples in the live dilation graph
GLINT_W = 0.35           # how strongly the glint prior pulls candidate scoring

FONT = cv2.FONT_HERSHEY_SIMPLEX


# ---------------- Kalman: [x, y, vx, vy] tracking the pupil center ----------
def make_kalman():
    kf = cv2.KalmanFilter(4, 2)
    kf.transitionMatrix = np.array([[1, 0, 1, 0],
                                    [0, 1, 0, 1],
                                    [0, 0, 1, 0],
                                    [0, 0, 0, 1]], np.float32)
    kf.measurementMatrix = np.array([[1, 0, 0, 0],
                                    [0, 1, 0, 0]], np.float32)
    kf.processNoiseCov = np.eye(4, dtype=np.float32) * 0.03
    kf.measurementNoiseCov = np.eye(2, dtype=np.float32) * 0.5
    return kf


def find_glint(gray, mask=None):
    """Brightest small specular spot -> location prior for the pupil."""
    g = cv2.GaussianBlur(gray, (5, 5), 0)
    if mask is not None:
        g = cv2.bitwise_and(g, g, mask=mask)
    _, mx, _, loc = cv2.minMaxLoc(g)
    if mx < 200:                     # no strong specular highlight
        return None
    return loc


def score_candidate(c, gray, glint):
    """Score a contour as 'pupil-ness': dark, round, sharp edge, near glint."""
    area = cv2.contourArea(c)
    if area < np.pi * MIN_R ** 2 or area > np.pi * MAX_R ** 2:
        return None
    peri = cv2.arcLength(c, True)
    if peri == 0:
        return None
    circ = 4 * np.pi * area / (peri * peri)      # 1.0 = perfect circle
    if circ < 0.55:                              # reject eyelid/lash shadows
        return None

    (x, y), r = cv2.minEnclosingCircle(c)
    x, y, r = int(x), int(y), int(r)

    # mean darkness inside the blob (darker = better)
    blob = np.zeros(gray.shape, np.uint8)
    cv2.drawContours(blob, [c], -1, 255, -1)
    inside = cv2.mean(gray, mask=blob)[0]

    # edge contrast: iris ring just outside should be much brighter than inside
    ring = np.zeros(gray.shape, np.uint8)
    cv2.circle(ring, (x, y), int(r * 1.6), 255, -1)
    cv2.circle(ring, (x, y), int(r * 1.1), 0, -1)
    outside = cv2.mean(gray, mask=ring)[0]
    contrast = max(0.0, outside - inside)        # bigger = crisper pupil edge

    # glint proximity prior
    if glint is not None:
        d = np.hypot(x - glint[0], y - glint[1])
        prox = np.exp(-d / (r + 1e-3))           # ~1 when glint is inside pupil
    else:
        prox = 0.0

    darkness = (255 - inside) / 255.0
    score = darkness * circ * (contrast / 255.0 + 0.15) * (1 + GLINT_W * prox)
    return score, c


def detect_pupil(gray, glint):
    """Return fitted ellipse ((cx,cy),(MA,ma),ang) for the best pupil, or None."""
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    eq = clahe.apply(gray)
    blur = cv2.GaussianBlur(eq, (7, 7), 0)

    thr = np.percentile(blur, DARK_PERCENTILE)   # adaptive dark cutoff
    _, mask = cv2.threshold(blur, int(thr), 255, cv2.THRESH_BINARY_INV)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, k)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, k, iterations=2)

    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    best = None
    for c in cnts:
        s = score_candidate(c, eq, glint)
        if s is None:
            continue
        if best is None or s[0] > best[0]:
            best = s
    if best is None or len(best[1]) < 5:
        return None
    return cv2.fitEllipse(best[1])


# ---------------- HUD helpers ----------------
def draw_dilation_graph(frame, hist, x, y, w, h):
    cv2.rectangle(frame, (x, y), (x + w, y + h), (40, 40, 40), -1)
    cv2.rectangle(frame, (x, y), (x + w, y + h), (90, 90, 90), 1)
    if len(hist) < 2:
        return
    lo, hi = min(hist), max(hist)
    rng = max(hi - lo, 1e-3)
    pts = []
    for i, v in enumerate(hist):
        px = x + int(i / (len(hist) - 1) * w)
        py = y + h - int((v - lo) / rng * (h - 6)) - 3
        pts.append((px, py))
    cv2.polylines(frame, [np.array(pts, np.int32)], False, (0, 255, 180), 1)
    cv2.putText(frame, "DILATION", (x + 4, y - 6), FONT, 0.4, (0, 255, 180), 1)


def main():
    cap = cv2.VideoCapture(STREAM_URL)
    if not cap.isOpened():
        print(f"Failed to open stream: {STREAM_URL}")
        return
    print("Streaming... press ESC or 'q' to quit.")

    WIN = "ESP32 Camera"
    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)   # resizable; upscales, same res
    cv2.resizeWindow(WIN, 1280, 720)

    kf = make_kalman()
    initialized = False
    trail = deque(maxlen=TRAIL_LEN)
    dil_hist = deque(maxlen=DILATION_HISTORY)
    baseline = None          # rolling baseline diameter for %-change readout

    while True:
        ret, frame = cap.read()
        if not ret:
            print("Failed to read frame from stream.")
            break

        h, w = frame.shape[:2]
        fcx, fcy = w // 2, h // 2
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        glint = find_glint(gray)
        ell = detect_pupil(gray, glint)

        # Kalman predict/update
        pred = kf.predict() if initialized else None
        diam = None
        if ell is not None:
            (ecx, ecy), (MA, ma), ang = ell
            diam = (MA + ma) / 2.0
            meas = np.array([[np.float32(ecx)], [np.float32(ecy)]])
            if not initialized:
                kf.statePost = np.array(
                    [[ecx], [ecy], [0], [0]], np.float32)
                initialized = True
            # gate: reject wild jumps vs prediction
            if pred is None or np.hypot(ecx - pred[0, 0],
                                        ecy - pred[1, 0]) < max(MA, 60):
                kf.correct(meas)

        dx = dy = dev = pct = 0.0
        if initialized:
            state = kf.statePost
            cx, cy = float(state[0, 0]), float(state[1, 0])
            icx, icy = int(cx), int(cy)
            trail.append((icx, icy))

            # --- draw pupil ellipse (bold lock on the eye) ---
            if ell is not None:
                cv2.ellipse(frame, ((cx, cy), (MA, ma), ang),
                            (0, 255, 120), 2)
                cv2.circle(frame, (icx, icy), 3, (0, 0, 255), -1)

            # --- glint ---
            if glint is not None:
                cv2.circle(frame, glint, 5, (0, 255, 255), 1)

            # --- gaze / deviation vector from center ---
            dx, dy = cx - fcx, cy - fcy
            dev = float(np.hypot(dx, dy))
            cv2.drawMarker(frame, (fcx, fcy), (180, 180, 180),
                           cv2.MARKER_CROSS, 16, 1)
            cv2.arrowedLine(frame, (fcx, fcy), (icx, icy),
                            (0, 180, 255), 1, tipLength=0.15)

            # --- motion trail (fading) ---
            for i in range(1, len(trail)):
                cv2.line(frame, trail[i - 1], trail[i], (255, 160, 0), 1)

            # --- dilation tracking ---
            if diam is not None:
                dil_hist.append(diam)
                baseline = diam if baseline is None else \
                    0.98 * baseline + 0.02 * diam
                pct = (diam - baseline) / baseline * 100.0

        # ---- HUD sidebar (never covers the eye) ----
        pw = 240
        panel = np.zeros((h, pw, 3), np.uint8)
        panel[:] = (24, 24, 24)
        cv2.putText(panel, "EYE TRACKER", (12, 30), FONT, 0.6,
                    (0, 255, 120), 2)
        if initialized and diam is not None:
            cv2.putText(panel, "PUPIL", (12, 70), FONT, 0.45, (150, 150, 150), 1)
            cv2.putText(panel, f"{diam:.1f}px", (12, 98), FONT, 0.8,
                        (0, 255, 120), 2)
            cv2.putText(panel, "DILATION", (12, 135), FONT, 0.45,
                        (150, 150, 150), 1)
            col = (0, 200, 255) if pct >= 0 else (255, 160, 0)
            cv2.putText(panel, f"{pct:+.1f}%", (12, 163), FONT, 0.8, col, 2)
            cv2.putText(panel, "GAZE DEV", (12, 200), FONT, 0.45,
                        (150, 150, 150), 1)
            cv2.putText(panel, f"{dev:.0f}px", (12, 228), FONT, 0.8,
                        (0, 180, 255), 2)
            cv2.putText(panel, f"dx {dx:+.0f}  dy {dy:+.0f}", (12, 252),
                        FONT, 0.45, (150, 150, 150), 1)
        else:
            cv2.putText(panel, "Searching...", (12, 70), FONT, 0.55,
                        (0, 0, 255), 1)
        draw_dilation_graph(panel, list(dil_hist), 12, h - 80, pw - 24, 60)

        canvas = np.hstack([frame, panel])
        cv2.imshow(WIN, canvas)
        key = cv2.waitKey(1) & 0xFF
        if key == 27 or key == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
