# RapidGlasses — Technical Map & Architecture Spec

> **Purpose of this document:** this codebase grew as several disconnected prototypes built at different times (eye tracking, head-sway tracking, a browser assessment UI, voice prompts). Nothing here has been unified into one product yet. This document is a complete, literal map of what exists on disk today — every file, every endpoint, every constant that matters — so the pieces can be integrated deliberately instead of by archaeology.

---

## 1. Product concept (why this exists)

**RapidGlasses** is a smart-glasses / tablet neurological rapid-assessment tool for concussion and other acute brain trauma.

- **Problem 1 — the data gap.** Between the moment of a head injury (on a field, at a workplace, in a car) and arrival at a hospital or clinic, there is no clinical data captured. Baseline neurological status at the moment of injury is lost.
- **Problem 2 — protocol friction.** Standard concussion protocols (e.g., SCAT5-style balance/oculomotor/memory batteries) are long, manual, and require a trained examiner. Patients and athletes routinely avoid or rush through them.
- **Problem 3 — compounding injury risk.** Repeated minor trauma (sub-concussive or concussive) has a cumulative neurological effect. Without rapid, repeatable, low-friction testing, minor injuries go unassessed and unlogged, and a person can be re-exposed before recovering.

**The proposed solution:** a wearable/tablet device that runs a short, mostly self-administered battery of standard neuro exam components — vestibular/ocular (VOR), smooth pursuit, static balance, and short-term verbal recall — using onboard cameras (eye + head tracking) and voice (TTS instructions, spoken memory words), producing an objective, timestamped record close to the time of injury.

This repo currently contains **prototype pieces of that device's software**, built independently and not yet wired together end-to-end.

---

## 2. Repo-wide inventory

```
RapidGlasses/
├── index.html            Stage 3: the on-device assessment UI (browser/webview)
├── secondcheck.py          THE Flask server entry point — run this one (ElevenLabs TTS proxy + registers tracking.py's blueprint)
├── tracking.py             Flask Blueprint: head-sway tracking (MediaPipe FaceLandmarker) — imported by secondcheck.py
├── main.py                 NOT a Flask app — standalone OpenCV CLI: PuRe-style pupil/eye tracker (see §6.1, §6.2 — naming trap)
├── calibrate.py            Standalone CLI tool: grab one frame from an ESP32 eye-cam stream, dump pixel stats
├── requirements.txt         Python deps: flask, flask-cors, requests, python-dotenv, opencv-python, mediapipe
├── SETUP.md                 Human-written onboarding doc (source of truth for local run instructions)
├── models/
│   └── face_landmarker.task   MediaPipe FaceLandmarker model blob (~3.7MB), used by tracking.py
├── vitals-server/            EMPTY directory — placeholder, nothing implemented yet
└── venv/                     Local-only Windows virtualenv — gitignored, do not use, see SETUP.md
```

`.git-rewrite/` (a stray `git filter-repo`/history-rewrite scratch directory that had been accidentally committed into the repo, containing a duplicate snapshot of early source files) was confirmed tracked-but-dead and removed — see §6.6.

> **Naming warning, read this first:** despite what the filenames suggest, `main.py` is **not** the Flask app and is **not** used by the running server at all — it's a standalone pupil-tracking CLI tool with its own `main()` function and its own `cv2` window, unrelated to HTTP. The real Flask server that SETUP.md tells you to run is `secondcheck.py`. See §6.2.

There is **no package.json, no build tooling, no test suite, no CI config, no database, no persistence layer** anywhere in this repo. Nothing here writes assessment results anywhere permanent yet (see §7, Gaps).

---

## 3. System architecture (as it exists today)

```
┌─────────────────────────────┐         ┌──────────────────────────────────────┐
│   index.html (Stage 3 UI)   │         │        Flask server (localhost:3001)    │
│   opened directly via        │  HTTP   │        entry point: secondcheck.py      │
│   file:// in a browser/      │────────▶│        (app object lives here too)      │
│   webview on the device      │         │                                          │
│                               │         │  ┌────────────────────────────────┐  │
│  - runs the test battery     │         │  │  secondcheck.py routes           │  │
│  - polls tracking snapshot   │         │  │   POST /api/speak (ElevenLabs)   │  │
│  - plays TTS audio           │         │  │   POST /api/recall-audio         │  │
│  - records in-memory only    │         │  └────────────────────────────────┘  │
└─────────────────────────────┘         │  ┌────────────────────────────────┐  │
                                          │  │  tracking.py blueprint          │  │
                                          │  │  (imported + registered by       │  │
                                          │  │   secondcheck.py)                 │  │
                                          │  │   POST /tracking/start           │  │
                                          │  │   POST /tracking/stop            │  │
                                          │  │   GET  /tracking/snapshot        │  │
                                          │  └──────────────┬─────────────────┘  │
                                          └─────────────────┼────────────────────┘
                                                             │ opens
                                                             ▼
                                          ┌──────────────────────────────────────┐
                                          │  Laptop webcam (cv2.VideoCapture)        │
                                          │  + MediaPipe FaceLandmarker              │
                                          │  (models/face_landmarker.task)           │
                                          │  background thread, nose-tip sway calc   │
                                          └──────────────────────────────────────┘

                                          ┌──────────────────────────────────────┐
                                          │  External: ElevenLabs TTS API           │
                                          │  api.elevenlabs.io/v1/text-to-speech/…  │
                                          │  auth via ELEVENLABS_API_KEY (.env)     │
                                          └──────────────────────────────────────┘

  ── Separate, unconnected prototype (not run by secondcheck.py at all) ──

┌──────────────────────────────┐         ┌──────────────────────────────────────┐
│   main.py — standalone CLI     │         │   Smart glasses IR eye camera             │
│   window, the PuRe-style       │  HTTP   │   MJPEG stream:                            │
│   pupil/eye tracker            │────────▶│   http://10.94.64.101:81/stream          │
│   opens a cv2 window, no       │         └──────────────────────────────────────┘
│   HTTP server, no Flask at all │
└──────────────────────────────┘
     ▲ same stream URL used by
     └── calibrate.py (one-shot calibration/debug CLI tool, tunes main.py's thresholds)
```

**Key architectural fact:** there are **two completely separate, non-communicating tracking subsystems** in this repo. They target different cameras, run in different processes, and neither imports the other (see §6.1). `main.py` — despite the name — holds the pupil/eye tracker, a standalone `cv2` desktop app with no Flask involvement; `tracking.py` holds the head-sway Blueprint that's actually wired into the running server via `secondcheck.py`.

---

## 4. Module-by-module spec

### 4.1 `index.html` — Stage 3 Assessment UI

**Role:** The only user-facing frontend in the repo. A single self-contained HTML file (inline CSS + inline JS, no build step, no framework, no bundler) implementing a linear state-machine assessment flow. Titled "Stage 3 Assessment" — implying an upstream Stage 1 and Stage 2 exist conceptually (injury detection / device wake?) but are **not implemented anywhere in this repo**; the UI currently starts from a manual "Simulate signal" button.

**How it's run:** opened directly as a `file://` URL (not served by Flask — Flask deliberately does not serve static HTML, see SETUP.md §7). Talks to the Flask server on `http://localhost:3001` via `fetch()`. CORS is wide open (`CORS(app)` with no origin restriction) specifically so `file://` origin requests work.

**State machine (screens, in order):**

| # | Screen id | Purpose | Advances via |
|---|---|---|---|
| 1 | `waiting` | Idle screen, waiting for an external "Stage 1" injury/wake signal | Manual button (`Simulate signal`) — **placeholder for a real signal**, see TODO in source |
| 2 | `intro` | "Quick check-in" — sets expectations | `Begin` button |
| 3 | `recall-priming` | Plays 5 spoken words via TTS for the patient to remember (never shown as text — this is a memory test) | Auto, when audio ends (or fallback timeout) |
| 4 | `calibration` | Starts head tracking, polls `/tracking/snapshot` until a face is found or timeout | Auto |
| 5 | `balance` | Static balance test: fixed dot, patient tries to stay still, 20s, progress ring animates | Auto after fixed duration |
| 6 | `pursuit` | Smooth-pursuit test: dot sweeps horizontally then vertically, patient follows with eyes only, 15s | Auto after fixed duration |
| 7 | `vor` | Vestibulo-ocular reflex test: dot pulses in place, patient turns head side-to-side while fixating, 20s | Auto after fixed duration; stops tracking at the end |
| 8 | `recall-marking` | Recognition-memory check: shows 5 words (3 real + 2 decoys, shuffled), patient marks each "Heard"/"Not heard" | Manual, `Submit` enabled once all 5 are marked |
| 9 | `complete` | "Sending results..." terminal screen | Auto-resets to `waiting` after 4s |

**Tunable constants (top of `<script>`):**

```js
RECALL_FALLBACK_MS = 8000   // enable Next even if TTS audio 'ended' never fires
CALIB_POLL_MS      = 300    // snapshot poll interval during calibration
CALIB_TIMEOUT_MS   = 25000  // give up waiting for a face, run tests anyway
CALIB_SETTLE_MS    = 1200   // let a few stable frames land before Balance
BALANCE_MS         = 20000
PURSUIT_MS         = 15000
VOR_MS             = 20000
COMPLETE_MS        = 4000
```

**Test battery content:**
- Recall word list: `["apple", "dog", "green", "road", "seven"]` (hardcoded, always the same 5 words — not randomized per session, which is a clinical-validity gap, see §7)
- Recall decoys: `["chair", "blue"]`
- Recall-marking screen shows 3 of the 5 real words + both decoys, shuffled — not all 5 real words, so it's a partial recognition check, not full recall.

**Network calls made by this file:**
| Call | Target | When |
|---|---|---|
| `POST /tracking/start` | `http://localhost:3001` | Entering calibration, balance, pursuit, VOR (fire-and-forget, rebaselines mean each time except it's really only meaningfully "opening" the camera once) |
| `POST /tracking/stop` | `http://localhost:3001` | End of VOR test, and safety-net in `resetAll()` |
| `GET /tracking/snapshot` | `http://localhost:3001` | Polled during calibration only, at `CALIB_POLL_MS` |
| `POST /api/speak` | `http://localhost:3001` | Every screen transition that has a spoken instruction |
| `POST /api/recall-audio` | `http://localhost:3001` | Once, in recall-priming, to speak the 5 memory words |

**Known-explicit TODOs in source:**
- Line 138: replace the "Simulate signal" click with a real Stage 1 signal (WebSocket or push) — this is the actual injury-trigger integration point.
- Line 393: recall results and all other assessment data are **not sent anywhere** — `submitRecall()` just transitions to a "Sending results..." screen and resets. No backend call exists. This is the single biggest unimplemented piece for the product's core value proposition (a persisted, timestamped clinical record).
- Balance/pursuit/VOR results (the actual sway/gaze deviation data collected via `/tracking/snapshot`) are **never read or scored by the frontend at all** — the UI polls tracking only during calibration to check `face` status, not during the actual tests. All the sway data produced during Balance/Pursuit/VOR is discarded; nothing aggregates or scores it. **This is the core clinical logic gap** — the objective measurements are being taken by `tracking.py` but never consumed.

---

### 4.2 `secondcheck.py` — Flask app entry (ElevenLabs TTS proxy) — THE server to run

**Role:** Defines the Flask `app` object, mounts CORS, registers the `tracking.py` blueprint, and exposes two ElevenLabs text-to-speech proxy endpoints. This is the file with the actual `Flask(__name__)` app, and it is the one SETUP.md tells you to run (`python secondcheck.py`) — see §6.2 for why the name is misleading (it sounds like a diagnostic script, but it's the production entry point).

**Config:**
```python
VOICE_ID   = "21m00Tcm4TlvDq8ikWAM"   # ElevenLabs "Rachel" voice
MODEL_ID   = "eleven_turbo_v2_5"
ELEVENLABS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"
```
API key read from `ELEVENLABS_API_KEY` env var (via `python-dotenv`, `.env` file, gitignored). If missing, ElevenLabs calls fail (401) and are swallowed client-side (`speak()`'s fire-and-forget `.catch(() => {})` in index.html) — audio just silently doesn't play, no error surfaced to the patient/tester.

**Endpoints:**

| Method | Path | Purpose | Request body | Response |
|---|---|---|---|---|
| `POST` | `/api/recall-audio` | TTS-render the comma-joined recall word list as one audio clip | `{ "words": string[] }` | `audio/mpeg` stream (proxied straight from ElevenLabs), or `400` if no words, `500` if ElevenLabs call failed |
| `POST` | `/api/speak` | TTS-render an arbitrary instruction string | `{ "text": string }` | `audio/mpeg` stream, or `400`/`500` |

Both endpoints call the shared `_tts(phrase)` helper, which does a streaming `requests.post` to ElevenLabs and returns the raw response object (or `None` on non-200) — the Flask route then re-streams `resp.iter_content()` back to the browser as `audio/mpeg`. No caching; every call round-trips to ElevenLabs.

**Blueprint registration:** `app.register_blueprint(tracking_bp)` — this is what merges in all of `tracking.py`'s `/tracking/*` routes onto the same Flask app/port.

---

### 4.3 `tracking.py` — Head-sway tracking Blueprint

**Role:** A Flask Blueprint (mounted into `secondcheck.py`'s app) that runs a background thread reading the **laptop's built-in/USB webcam** (`cv2.VideoCapture(CAMERA_INDEX)`, local device index — not a network stream), detecting the nose-tip landmark via MediaPipe's `FaceLandmarker` task API, and computing head-sway deviation from a running mean. This is the **actual objective-measurement engine** behind the Balance/Pursuit/VOR tests in `index.html` — although as noted above, the frontend does not currently consume its output beyond calibration status.

**Camera source:** this is deliberately the tester/patient-facing **laptop or tablet webcam**, not the smart glasses — head-sway during Balance/Pursuit/VOR is measured by watching the face from the device screen's own camera, the same way the patient is looking at the on-screen dot.

**Why nose tip, why running mean:** the method is deliberately simple — one landmark (`NOSE_TIP = 1`, MediaPipe FaceLandmarker index), normalized 0–1 image coordinates, and the "baseline" is just the cumulative mean of x/y since the last `/tracking/start` call. `deviation` = Euclidean distance of the current point from that running mean. This is a proxy for postural/head sway, not a calibrated clinical measurement — no units conversion to real-world distance, no correction for camera distance/angle.

**Threading model:**
- One background thread (`_loop`), started/stopped via `/tracking/start` / `/tracking/stop`.
- Shared mutable state dict `_state` protected by a `threading.Lock`.
- Request handlers never touch the camera directly — `snapshot()` is a pure read of `_state`.
- `_loop()` imports `cv2`/`mediapipe`/`numpy` inside the function body (not at module top) specifically so **the Flask server still boots even if those heavy deps aren't installed** — tracking would just fail at `/tracking/start` time with an `error` in the snapshot instead of crashing the whole server. This is a deliberate resilience pattern worth preserving if this module is refactored.

**Config (env-overridable):**
```python
CAMERA_INDEX  = env TRACKING_CAMERA_INDEX, default 0
MODEL_PATH    = env TRACKING_MODEL_PATH, default ./models/face_landmarker.task
NOSE_TIP      = 1        # MediaPipe FaceLandmarker landmark index
FPS_WINDOW    = 30        # frames used for rolling fps calc
DEBUG_WINDOW  = env TRACKING_DEBUG in (1/true/yes) — opens a live cv2 debug window
SWAY_GAIN     = 12         # visual-only magnification factor for the debug overlay
DEV_HISTORY   = 240        # debug sway-trace history length
```

**Endpoints** (mounted with no prefix beyond `/tracking`, registered on `secondcheck.py`'s app → served on port 3001 alongside `/api/*`):

| Method | Path | Purpose | Behavior detail |
|---|---|---|---|
| `POST` | `/tracking/start` | Opens camera (if not already running), resets the running-mean baseline, spawns/reuses the background thread | Returns immediately (`status: "starting"` or `"running"`) — camera+model warm-up (~10s) happens async; caller must poll `/snapshot` |
| `POST` | `/tracking/stop` | Signals the thread to stop, joins with 3s timeout, releases camera | Returns `{status: "stopped"}` |
| `GET` | `/tracking/snapshot` | Pure read of current shared state, no camera work on this thread | See response shape below |

**Snapshot response shape:**
```json
{
  "x": 0.53, "y": 0.64,
  "mean_x": 0.54, "mean_y": 0.64,
  "deviation": 0.0258,
  "timestamp": 1784398909.28,
  "fps": 30.2,
  "face": true,
  "running": true,
  "error": null
}
```
- `deviation`: Euclidean distance of current nose position from running mean, normalized frame units (0–1 scale, not real-world units).
- `face: false`: reading is stale — `x`/`y`/`timestamp` are from the last good frame, not this one.
- `error` non-null: the tracking thread died (camera lost, model missing, etc.) and needs a fresh `/tracking/start` to recover. `running` will also flip to `false`.
- Intended poll rate: ~30Hz, matched to `fps` — polling faster than the loop's actual fps re-reads stale data.

**Debug visualization (`TRACKING_DEBUG=1`):** opens a live `cv2.imshow` window (`_draw_debug`) showing the tracked point, the running-mean baseline crosshair, a gain-magnified sway arrow, a live deviation circle, fps/frame-count readout, and a scrolling sway-trace graph. Purely diagnostic — never sent over the network. Must run inside the tracking loop's own thread since only one process can hold the camera.

---

### 4.4 `calibrate.py` — Eye-camera calibration CLI (standalone tool)

**Role:** A one-off diagnostic script, not part of the running product. Connects to a hardcoded MJPEG stream URL (`http://10.94.64.101:81/stream` — the **smart glasses' onboard IR eye camera**, an ESP32-CAM-class module built into the prototype glasses hardware), grabs one frame (optionally skipping N frames first to let exposure settle), and dumps pixel-level statistics to help tune the pupil detector's thresholds in `main.py` (§4.5).

**What it does:**
1. Opens `cv2.VideoCapture(STREAM_URL)`.
2. Reads and discards `skip` frames (CLI arg, default 10), keeps the last one.
3. Prints grayscale min/max/mean, percentile darkness cutoffs (2/5/8/12/20%), brightest-spot location (glint candidate), and dark-region centroids at two thresholds (pupil candidates).
4. Saves `calib_frame.png` (raw frame) and `calib_masks.png` (grayscale | dark-mask side by side) to disk for visual inspection.

**Usage:** `python3 calibrate.py [skip_frames]`

**Output artifacts:** `calib_frame.png`, `calib_masks.png` — both currently present at repo root (untracked/gitignored status not verified, worth checking before committing).

---

### 4.5 `main.py` — the PuRe-style pupil/eye tracker (standalone CLI, NOT the Flask app)

**Role:** A standalone OpenCV desktop application (`main()` opens a `cv2` window, no Flask, no HTTP server of its own) that connects to the **same MJPEG stream** as `calibrate.py` (`http://10.94.64.101:81/stream` — the **smart glasses' IR eye camera**, physically mounted on the glasses and aimed at the wearer's eye, distinct from the head-sway laptop webcam), and does real-time pupil detection, tracking, and dilation analysis, rendered live in a two-pane window (video + HUD panel). Despite its filename, this has nothing to do with the server — do not confuse it with `secondcheck.py` (§4.2), which is the actual Flask entry point.

**Camera source and why IR matters:** the eye camera is infrared-illuminated (typical for pupil tracking — IR passes through the iris and reflects off the retina/produces a strong pupil-vs-iris contrast independent of visible-light conditions, and doesn't dazzle the wearer's vision with a visible light source pointed at their eye). `GLINT_THRESH` in the detection tuning is picking up the IR illuminator's reflection off the cornea (the "glint"), not an ambient light reflection — that distinction matters if this constant is ever retuned for a different camera/illuminator.

**Algorithm — PuRe-style pupil detection** (`detect_pupil(gray, prior=None)`):
1. Compute an absolute darkness reference (3rd percentile of grayscale).
2. CLAHE contrast normalization + median blur.
3. Threshold + dilate to find specular glints (bright spots), then `cv2.inpaint` them out so they don't fracture pupil edges.
4. Canny edge detection + dilation.
5. Find contours, fit ellipses to each candidate (`cv2.fitEllipse`), reject on:
   - area out of `[MIN_AREA, MAX_AREA_FRAC * frame_area]`
   - aspect ratio (roundness) below `MIN_ASPECT`
   - contour↔ellipse area agreement below `MIN_FIT`
   - too close to frame border
   - interior/surround contrast below `MIN_CONTRAST`
   - interior brightness too far above the dark reference (`MAX_INTERIOR_ABOVE_DARK`)
6. Score surviving candidates by a weighted product of fit, aspect, contrast, darkness (cubed — heavily weighted), size, and **locality** — either temporal (distance from prior Kalman-predicted position, exponential decay) or, cold-start, a gentle center-of-frame bias.
7. Highest-scoring candidate wins; also finds the glint contour nearest the chosen pupil center for HUD display.

**Tracking/smoothing:**
- 4-state Kalman filter (`x, y, vx, vy`) smooths the pupil center frame to frame; re-acquires (resets state) after 4 consecutive frames where detection disagrees strongly with the predicted position.
- Ellipse shape (major/minor axis, angle) is separately smoothed via EMA (`SHAPE_EMA = 0.25`), with angle represented as `sin(2θ)/cos(2θ)` to avoid 180°-wrap discontinuities, and axes pulled toward a perfect circle by `ROUNDNESS = 0.6` (pupils are near-round; this kills ellipse-flipping artifacts).

**Derived metrics:**
- **Pupil diameter** (px, average of smoothed major/minor axis) — tracked over a rolling baseline (slow EMA, `0.985/0.015` decay) to compute **dilation % vs. baseline** — this is the actual physiologically-relevant signal (pupillary response is a standard neuro-exam component; asymmetric or sluggish dilation is a concussion/TBI red flag).
- **Gaze deviation** — pixel offset (dx, dy, Euclidean) of the tracked pupil from frame center. Not calibrated to visual angle/degrees.

**Constants:**
```python
GLINT_THRESH = 180
MIN_AREA = 150 ; MAX_AREA_FRAC = 0.4
MIN_ASPECT = 0.45 ; MIN_FIT = 0.55
MIN_CONTRAST = 8 ; MAX_INTERIOR_ABOVE_DARK = 45
SHAPE_EMA = 0.25 ; ROUNDNESS = 0.6
TRAIL_LEN = 50 ; DILATION_HISTORY = 180
```

**Display:** live OpenCV window, video feed (resized to 640px display height) + a right-side HUD panel showing pupil diameter, dilation % (color-coded), gaze deviation with dx/dy, and a scrolling dilation-trend graph. **Entirely local/manual — no HTTP endpoints, no data export, no way for `index.html` or any server to consume this.** This is the biggest integration gap between the two tracking subsystems (see §7).

**Exit:** `ESC` or `q` key.

---

### 4.6 `models/face_landmarker.task`

MediaPipe's pretrained face landmark model (float16, ~3.7MB), sourced from `https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task`. Committed to the repo directly (binary blob) so a fresh clone works without a separate download step. Consumed only by `tracking.py`'s `_loop()` (§4.3) via MediaPipe's `tasks.python.vision.FaceLandmarker`.

### 4.7 `vitals-server/`

**Empty directory.** No files. Presumably a placeholder for a future service — name suggests vital-signs (heart rate? pulse ox? blood pressure?) ingestion, which would fit the broader concussion-assessment product concept (vitals are part of standard trauma triage) but nothing has been built here. Flag this explicitly as **planned-but-unstarted** in any roadmap discussion.

### 4.8 `venv/`

A Windows virtualenv directory that exists on disk locally — **not usable cross-platform**, explicitly called out as a mistake in SETUP.md. It is gitignored and confirmed **not tracked** by git (`git ls-tree` shows no `venv/` entries in HEAD), so it is not a repo-hygiene problem, just a leftover local folder each developer should delete and replace with their own `.venv/` per SETUP.md step 2.

### 4.9 `.git-rewrite/` — REMOVED

This directory (a `git filter-repo`/history-rewrite scratch dir containing a `t/` snapshot of older `calibrate.py`, `index.html`, `main.py`, `requirements.txt`, `secondcheck.py`) was confirmed **tracked in git** (present under `HEAD` via `git ls-tree`) despite being pure tooling scratch state with no relationship to the running application. It has been removed (`git rm -r --cached .git-rewrite` + deleted from disk). If a teammate's local clone still has it, it's safe to delete after pulling this change.

---

## 5. Full endpoint reference (consolidated)

All endpoints below are served from **one Flask process on `localhost:3001`**, launched via `python secondcheck.py` (see §6.2 for why `main.py` is easy to mistake for the server entry point when it isn't).

| Method | Path | Module | Auth | Purpose |
|---|---|---|---|---|
| `POST` | `/api/speak` | secondcheck.py | none (local only) | TTS-render arbitrary instruction text via ElevenLabs, streamed back as `audio/mpeg` |
| `POST` | `/api/recall-audio` | secondcheck.py | none | TTS-render the recall word list as one clip |
| `POST` | `/tracking/start` | tracking.py (Blueprint) | none | Start/reset head-sway tracking thread + webcam |
| `POST` | `/tracking/stop` | tracking.py (Blueprint) | none | Stop tracking thread, release webcam |
| `GET` | `/tracking/snapshot` | tracking.py (Blueprint) | none | Poll current sway/face-detection state |

No authentication, no HTTPS, no rate limiting anywhere — acceptable for a `localhost`-only prototype talking to a `file://` UI on the same device, **not acceptable if this ever becomes a networked/multi-device product** (flag for the security review once real patient data is involved — HIPAA-relevant if deployed clinically in the US).

**External API dependency:**
| Service | Used by | Auth | Notes |
|---|---|---|---|
| ElevenLabs TTS (`api.elevenlabs.io/v1/text-to-speech/{voice_id}`) | `_tts()` in secondcheck.py | `xi-api-key` header, from `ELEVENLABS_API_KEY` env var | Voice: "Rachel" (`21m00Tcm4TlvDq8ikWAM`), model `eleven_turbo_v2_5`. No caching — every instruction round-trips live. No offline fallback; if this API is down or the key is missing, all spoken prompts silently fail (audio just doesn't play) but the UI does **not** block or show an error — every `speak()` call is fire-and-forget. |

---

## 6. Known issues / traps for whoever unifies this

### 6.1 Naming collision: two things called "tracking"
There is a **head/face tracking system** (`tracking.py`, Flask Blueprint, nose-tip sway, feeds the Balance/Pursuit/VOR tests, reads the **laptop/tablet webcam**) and a **separate pupil/eye tracking system** (PuRe-style detector, Kalman-filtered, dilation %, standalone OpenCV window, reads the **smart glasses' onboard IR eye camera** over an MJPEG stream at `10.94.64.101:81`). These are **conceptually complementary** (a full neuro exam wants both head-sway AND pupillary response) but **operationally unconnected** — different processes, different cameras, no shared server, no shared data model. Before unifying, decide: do both run simultaneously during the same assessment (glasses IR camera for pupil/gaze during Pursuit/VOR, device webcam for head-sway throughout)? Does the pupil tracker need to become a Flask blueprint just like head tracking, exposing its own `/pupil/snapshot`-style endpoint?

### 6.2 `main.py` is not the server — the filenames actively mislead
Every instinct says `main.py` should be the entry point and `secondcheck.py` sounds like a throwaway diagnostic script — it's the opposite. `secondcheck.py` is the real Flask app (TTS proxy + registers the tracking blueprint) and is what SETUP.md tells you to run. `main.py` is a standalone pupil-tracking desktop tool with no Flask code in it at all. This is the single highest-risk landmine for onboarding — a strong recommendation is to rename these two files to something that states their role (e.g. `server.py` and `eye_tracker_cli.py`) before adding more code to either.

### 6.3 Assessment data is never persisted or transmitted
- Recall answers: computed in-memory (`marks` array in `index.html`), never sent anywhere (`submitRecall()` TODO).
- Balance/Pursuit/VOR sway data: available live via `/tracking/snapshot` but **never polled by the UI during the actual tests** — only during calibration. The core clinical signal (sway magnitude during each test phase) is being measured by the backend and thrown away by the frontend.
- Pupil dilation / gaze deviation data: only ever rendered to a local `cv2` debug window, never captured to any file or endpoint.
- **There is no database, no results storage, no patient/session identifier, no way to compare a post-injury assessment against a prior baseline** — which is core to the stated product goal (detecting deviation from a person's normal baseline, and tracking compounding injuries over time). This is the largest gap between "what's built" and "what the product needs to do."

### 6.4 No Stage 1 / Stage 2 in this repo
`index.html`'s title ("Stage 3 Assessment") and its "Waiting for Stage 1 signal" screen imply a staged pipeline (injury detection → triage decision → assessment) that isn't present in this codebase at all. Whoever owns Stage 1/2 (impact detection sensor? manual clinician trigger?) needs to define the real signal contract that will replace the "Simulate signal" button.

### 6.5 Hardcoded LAN IP for the glasses' IR eye camera
`STREAM_URL = "http://10.94.64.101:81/stream"` appears in both `calibrate.py` and `main.py`'s pupil tracker — a hardcoded local network address for the smart glasses' onboard IR eye camera (ESP32-CAM-class module). Not configurable via env var (unlike `tracking.py`'s `TRACKING_CAMERA_INDEX`/`TRACKING_MODEL_PATH` pattern for the webcam) — worth normalizing before this goes to a second physical glasses unit, since every unit will get a different DHCP-assigned IP.

### 6.6 Fixed, non-randomized recall word list
The 5 recall words are hardcoded and identical on every run (`["apple", "dog", "green", "road", "seven"]`). For a repeatable clinical memory test, a fixed word list is a validity problem (patients could memorize expected words after repeat administration) — likely needs a word bank with per-session randomization.

### 6.7 Dead tooling artifact removed: `.git-rewrite/`
Confirmed tracked-but-useless and deleted from the repo (see §4.9) — a `git filter-repo`/history-rewrite scratch directory that had no relationship to the running application, just an old snapshot of source files from mid-rewrite. `git rm -r --cached` plus a filesystem delete; no source files were touched.

---

## 7. Summary: build status by product component

| Component | Status | File(s) |
|---|---|---|
| Assessment UI / state machine | Built, functional | `index.html` |
| Spoken instructions (TTS) | Built, functional (needs API key) | `secondcheck.py` |
| Recall memory test (verbal) | Built, but fixed word list + results discarded | `index.html` |
| Head-sway tracking (balance/pursuit/VOR signal) | Built, functional as a service; **not consumed by the UI during tests** | `tracking.py`, `secondcheck.py` |
| Pupil/gaze tracking (dilation, gaze deviation) | Built as a standalone desktop tool; **not networked, not integrated with anything** | `main.py` |
| Calibration/tuning tooling | Built (manual CLI) | `calibrate.py` |
| Results storage / persistence | **Not started** | — |
| Stage 1/2 injury-trigger signal | **Not started** (stubbed with a manual button) | `index.html` |
| Vitals ingestion | **Not started** (empty placeholder) | `vitals-server/` |
| Baseline comparison / longitudinal tracking across injuries | **Not started** | — |
| Auth / patient identity / HIPAA-relevant controls | **Not started** | — |
