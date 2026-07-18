"""Calibration tool: grab an eye frame from the stream, save it, and dump
pixel-level stats so the pupil detector can be tuned against real data.

Usage:
    python3 calibrate.py            # grab 1 frame, save + analyze
    python3 calibrate.py 20         # skip 20 frames first (let exposure settle)
"""
import sys
import cv2
import numpy as np

STREAM_URL = "http://10.94.64.101:81/stream"
OUT = "calib_frame.png"


def grab(skip):
    cap = cv2.VideoCapture(STREAM_URL)
    if not cap.isOpened():
        print("Failed to open stream")
        sys.exit(1)
    frame = None
    for _ in range(skip + 1):
        ret, f = cap.read()
        if ret:
            frame = f
    cap.release()
    if frame is None:
        print("No frame")
        sys.exit(1)
    return frame


def analyze(frame):
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    print(f"frame: {w}x{h}")
    print(f"gray min/max/mean: {gray.min()}/{gray.max()}/{gray.mean():.1f}")
    for p in (2, 5, 8, 12, 20):
        print(f"  darkest {p:>2}% cutoff = {np.percentile(gray, p):.0f}")

    # brightest spot (candidate glint)
    g = cv2.GaussianBlur(gray, (5, 5), 0)
    _, mx, _, loc = cv2.minMaxLoc(g)
    print(f"brightest spot: val={mx} at {loc}")

    # darkest region centroid (candidate pupil) at a few thresholds
    for cut in (np.percentile(gray, 5), np.percentile(gray, 10)):
        _, mask = cv2.threshold(gray, int(cut), 255, cv2.THRESH_BINARY_INV)
        m = cv2.moments(mask, binaryImage=True)
        if m["m00"] > 0:
            cx, cy = m["m10"] / m["m00"], m["m01"] / m["m00"]
            area = m["m00"] / 255
            print(f"  dark<{cut:.0f}: centroid=({cx:.0f},{cy:.0f}) "
                  f"area={area:.0f}px")

    # save a contact sheet: original | gray | dark-mask
    cut = np.percentile(gray, 8)
    _, mask = cv2.threshold(gray, int(cut), 255, cv2.THRESH_BINARY_INV)
    sheet = np.hstack([
        gray,
        mask,
    ])
    cv2.imwrite("calib_masks.png", sheet)
    cv2.imwrite(OUT, frame)
    print(f"saved {OUT} and calib_masks.png (gray | dark-mask)")


if __name__ == "__main__":
    skip = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    analyze(grab(skip))
