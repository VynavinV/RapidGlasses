# RapidGlasses — Technical Map & Architecture Spec

> **Purpose of this document:** this codebase grew as several disconnected prototypes built at different times (eye tracking, head-sway tracking, a browser assessment UI, voice prompts). Nothing here has been unified into one product yet. This document is a complete, literal map of what exists on disk today — every file, every endpoint, every constant that matters — so the pieces can be integrated deliberately instead of by archaeology.

---

## 1. Product concept (why this exists)

**RapidGlasses** is a smart-glasses / tablet neurological rapid-assessment tool for concussion and other acute brain trauma.

- **Problem 1 — the data gap.** Between the moment of a head injury (on a field, at a workplace, in a car) and arrival at a hospital or clinic, there is no clinical data captured. Baseline neurological status at the moment of injury is lost.
- **Problem 2 — protocol friction.** Standard concussion protocols (e.g., SCAT5-style balance/oculomotor/memory batteries) are long, manual, and require a trained examiner. Patients and athletes routinely avoid or rush through them.
- **Problem 3 — compounding injury risk.** Repeated minor trauma (sub-concussive or concussive) has a cumulative neurological effect. Without rapid, repeatable, low-friction testing, minor injuries go unassessed and unlogged, and a person can be re-exposed before recovering.

**The proposed solution:** a wearable/tablet device that runs a short, mostly self-administered battery of standard neuro exam components — vestibular/ocular (VOR), smooth pursuit, static balance, and short-term verbal recall — using onboard cameras (eye + head tracking) and voice (TTS instructions, spoken memory words), producing an objective, timestamped record close to the time of injury.

**The staged pipeline, as `report_data.json` (§4.10) reveals it:**

`report_data.json` — the sample payload for the new report viewer — is the clearest single artifact in the repo for understanding the *intended* end-to-end architecture, because it's the first place a Stage 1/Stage 3 data contract and a fusion/gating decision actually get written down anywhere. From it:

1. **Stage 1 — passive/quick primary screen.** Pupil diameter + variance (glasses IR eye camera), blink rate/duration, an optional pupillary light response (PLR) test, and heart rate / HRV from a device called **"Presage"** (referenced as `hrv.source: "Presage (laptop)"` — not implemented anywhere in this repo; treat as an external hardware/software integration to track down, not something to build from scratch). Produces a `stage1_composite_score`.
2. **Gate decision.** Stage 1's composite score (plus two **hard override** checks — anisocoria and severe VOR, see §4.10) decides whether the assessment can stop at Stage 1 ("clear") or must escalate to Stage 3 ("ambiguous"/ refer). This is the `outcome.gate_decision` field and is the actual reason `index.html` is titled "Stage 3 Assessment" — it's the escalation path, not the whole product.
3. **Stage 3 — secondary battery.** This is the one currently implemented as `index.html` + `tracking.py`: Balance, Smooth Pursuit, VOR, and delayed word Recall. Produces a `stage3_composite_score`.
4. **Fusion.** A weighted combination of Stage 1 and Stage 3 composites (`fusion.stage1_weight` / `stage3_weight` / `weighted_composite`) plus the hard overrides produces a final `outcome.flag` (`"refer"` or presumably `"clear"`) and a **SCAT5-style severity score** (`outcome.scat5_score` / `scat5_max`, out of 132 — this is the standard clinical symptom-severity scale for concussion, so the product is explicitly trying to map its own composite onto a recognized clinical instrument).
5. **Report.** The fusion node's final output is rendered as a read-only HTML report (`report.py` / `report.html` / `style.css`, §4.10) — this is meant to be the artifact a hospital or clinician actually looks at.

**This means "Stage 1", "Stage 2" (fusion/gating — no dedicated files yet, likely meant to live in a "fusion node," possibly the empty `vitals-server/`), and "Stage 3" are not vague — they now have a concrete data shape via `report_data.json`.** Nothing in the repo currently *produces* a `report_data.json`-shaped payload from a live assessment; it's currently a hand-authored sample fixture the report server reads. Wiring `index.html`'s Stage 3 results (and a real Stage 1 capture pipeline) into that shape is the central unification task — see §6.8 and §8.

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
├── report.py                A THIRD, separate Flask app (its own process, port 8080) — serves the read-only clinician-facing report
├── report.html              Jinja2 template rendered by report.py — the actual report layout
├── report_data.json          Hand-authored SAMPLE payload report.py reads — no real pipeline writes this file yet (see §4.10)
├── style.css                 Stylesheet for report.html, served as a static file by report.py
├── requirements.txt         Python deps: flask, flask-cors, requests, python-dotenv, opencv-python, mediapipe (jinja2 comes in transitively via flask)
├── SETUP.md                 Human-written onboarding doc (source of truth for local run instructions) — NOT yet updated for report.py, see §6.9
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

  ── Third, also-unconnected process: the clinician report viewer ──

┌──────────────────────────────┐         ┌──────────────────────────────────────┐
│   Clinician / hospital        │  HTTP   │   report.py — Flask app #2               │
│   browser                     │────────▶│   its OWN process, port 8080             │
│                                │         │   GET  /  and  /report → renders          │
│                                │         │           report.html (Jinja2)             │
│                                │         │   GET  /api/report → raw JSON               │
└──────────────────────────────┘         └──────────────┬───────────────────────┘
                                                            │ reads (static file, not DB)
                                                            ▼
                                          ┌──────────────────────────────────────┐
                                          │  report_data.json (hand-authored        │
                                          │  SAMPLE fixture — no real assessment    │
                                          │  pipeline writes this file yet)         │
                                          └──────────────────────────────────────┘
```

**Key architectural fact #1:** there are **two completely separate, non-communicating tracking subsystems** in this repo. They target different cameras, run in different processes, and neither imports the other (see §6.1). `main.py` — despite the name — holds the pupil/eye tracker, a standalone `cv2` desktop app with no Flask involvement; `tracking.py` holds the head-sway Blueprint that's actually wired into the running server via `secondcheck.py`.

**Key architectural fact #2:** there are now **three separate Flask apps / processes** in this repo, none of which talk to each other over HTTP or share any in-memory state: `secondcheck.py` (port 3001, the assessment-runtime server), `report.py` (port 8080, the read-only report viewer), and conceptually a not-yet-built "fusion node" that would sit between them (see §1). The only thing connecting Stage 3 (`index.html`/`secondcheck.py`) to the report (`report.py`) today is that they'd need to agree on the `report_data.json` schema — nothing currently writes that file from a live run.

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

### 4.10 `report.py` / `report.html` / `style.css` / `report_data.json` — the clinician-facing report viewer

**Role:** A **separate, standalone Flask app** — its own process, its own port (`8080`, vs. `secondcheck.py`'s `3001`) — that renders a read-only, print-friendly HTML report summarizing a completed assessment. This is the newest addition to the repo and is the best current evidence of what the *finished* data pipeline is meant to produce (see the staged-pipeline breakdown in §1). It does not import or share any code with `secondcheck.py`, `tracking.py`, or `main.py`.

**`report.py` — the Flask app:**
- `app = Flask(__name__, template_folder=BASE_DIR, static_folder=BASE_DIR, static_url_path="")` — templates and static files (`style.css`) both served from repo root, not a conventional `templates/`/`static/` subfolder split. Keep this in mind if the repo is ever restructured into subdirectories — moving `report.html` or `style.css` into a subfolder will break this config unless `template_folder`/`static_folder` are updated too.
- `load_report_data()`: reads `report_data.json` from disk on every request (no caching, no DB). Returns HTTP `503` if the file doesn't exist yet ("assessment pipeline has not written report_data.json" — this is the exact error a fresh clone or an un-run pipeline will hit), `500` on invalid JSON or a non-object JSON root.
- `format_timestamp(raw)`: parses an ISO 8601 string (handling a trailing `Z` by rewriting to `+00:00`) into a human-readable `"July 18, 2026 at 03:04 PM"`-style string for display; falls back to the raw string on parse failure.
- `status_class(status)`: maps a free-text status string (e.g. `"elevated_variance"`, `"borderline"`, `"normal"`) to one of four CSS-facing buckets — `good`, `warn`, `alert`, `neutral` — via fixed string-membership sets. **This mapping is the closest thing in the repo to defined clinical thresholds for what counts as normal/borderline/concerning** — anyone building a real scoring/fusion engine should look here first for the vocabulary already in use, and should keep `report_data.json`'s status strings within these known sets (an unrecognized status string silently falls through to `"neutral"`, no error).

**Routes:**

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` and `/report` | Renders `report.html` with the loaded JSON data + computed template context (formatted timestamp, outcome CSS class, `status_class` passed in as a callable so the template can call it inline) |
| `GET` | `/api/report` | Returns the raw `report_data.json` contents as JSON — a machine-readable mirror of the same data |

**Run:** `python report.py` — starts Flask's dev server with `debug=True` on `0.0.0.0:8080` (note: `debug=True` and `host="0.0.0.0"` together is a dev-only combination — Flask's debugger allows arbitrary code execution if reachable from the network, so this must not ship as-is to anything beyond a local/trusted demo environment; see §6.10).

**`report_data.json` — the data contract:**

This is presently a **hand-authored sample/fixture**, not real pipeline output — a session for `"Athlete A"` with a `"refer"` outcome, used to develop and demo the report layout. Its shape is nonetheless the most authoritative schema reference in the repo for what a finished assessment record should look like. Top-level keys:

- `session_id`, `patient_label`, `assessed_at` (ISO 8601), `assessment_duration_seconds`
- `outcome`: `flag` (`"refer"` | presumably `"clear"`), `headline` (display text), `scat5_score`/`scat5_max` (SCAT5 clinical severity scale, out of 132), `gate_decision` (e.g. `"ambiguous"`), `stage3_triggered` (bool), `cleared_to_play` (bool)
- `hard_overrides`: `anisocoria` (pupil-size asymmetry — `left_pupil_mm`, `right_pupil_mm`, `asymmetry_mm`, `threshold_mm`, `triggered`) and `vor_severe` (`gaze_variance_ratio`, `threshold_ratio`, `triggered`) — **these are meant to bypass the normal scoring/fusion path entirely**: per `report.html`'s copy, a triggered override forces "urgent referral regardless of other scores." This is a real clinical safety mechanism (anisocoria in particular is a classic acute-neurological red flag, e.g. possible intracranial pressure/herniation) and should be treated as a hard gate, not a weighted input, in any real fusion implementation.
- `stage1`: `pupil` (`left_mean_mm`, `right_mean_mm`, `left_variance`, `right_variance`, `combined_mean_mm`, `status`), `blink` (`rate_per_minute`, `mean_duration_ms`, reference range, `status`), `plr` (`stimulus_used` bool, `amplitude_percent`, `reference_min_percent`, `status`), `hrv` (`heart_rate_bpm`, `hrv_rmssd_ms`, `source` — sample says `"Presage (laptop)"`, `status`), plus `stage1_composite_score`/`stage1_max_score`
- `stage3`: `balance` (`sway_magnitude_px`, `sway_frequency_hz`, `duration_seconds`, `threshold_magnitude_px`, `status`), `smooth_pursuit` (`horizontal_correlation`, `vertical_correlation`, `threshold_min`, `status`), `vor` (`static_gaze_variance`, `motion_gaze_variance`, `variance_ratio`, `threshold_ratio`, `status`), `recall` (`words_presented`, `words_recalled`, `score`, `max_score`, `scoring_method`), plus `stage3_composite_score`/`stage3_max_score`
- `fusion`: `stage1_weight`, `stage3_weight`, `weighted_composite`, `decision_basis` (free text)
- `narration`: `summary` (free-text paragraph), `recommendations` (list of strings) — currently hand-written in the sample; a real pipeline would need to either template this from the structured fields or have an LLM/rules engine generate it
- `metadata`: `devices` (a dict of role → device description — sample values: `glasses: "ESP32-S3 IR stream"`, `laptop: "Presage HR/HRV"`, `fusion_node: "Raspberry Pi (QNX)"`, `tablet: "MacBook secondary battery"` — **this is the first place in the repo that names the intended physical hardware topology**, confirming a Raspberry Pi running QNX as the fusion/orchestration node referenced obliquely in `SETUP.md`'s "what QNX talks to" section), `report_version`

**Important cross-reference:** `metadata.devices.fusion_node` confirms the "QNX" references scattered through `SETUP.md` and `tracking.py`'s docstring (`"QNX polls /tracking/snapshot"`) refer to a **Raspberry Pi running QNX** acting as the orchestration/fusion layer between the glasses, the laptop, and the tablet — this is the actual system architecture and gives a name to the thing that's supposed to eventually call `secondcheck.py`'s and `main.py`'s tracking endpoints, run the fusion logic, and write `report_data.json`. No code for this QNX/fusion node exists anywhere in this repo.

**Template details (`report.html`, Jinja2):**
- Uses `{{ x | default("—") }}` extensively so missing fields render an em-dash instead of erroring — the template is defensive against partially-populated data, which is worth preserving as a pattern once a real pipeline starts producing partial results (e.g. Stage 1 complete but Stage 3 not yet run).
- `{% if outcome.stage3_triggered %}` gates whether the entire Stage 3 section renders — so a "cleared at Stage 1" report simply omits Balance/Pursuit/VOR/Recall.
- The hard-override panel only renders if `anisocoria.triggered` or `vor_severe.triggered` is truthy.
- `@media print` rules in `style.css` strip box-shadows for print output — the report is explicitly designed to be printed or exported as a hospital chart insert, not just viewed on-screen.

**Not yet connected to anything:** nothing in `index.html`/`secondcheck.py`/`tracking.py`/`main.py` writes to `report_data.json`. The report viewer and the assessment runtime are two ends of a pipe with no pipe built yet — see §6.8.

---

## 5. Full endpoint reference (consolidated)

**Process 1 — the assessment runtime, `localhost:3001`**, launched via `python secondcheck.py` (see §6.2 for why `main.py` is easy to mistake for the server entry point when it isn't):

| Method | Path | Module | Auth | Purpose |
|---|---|---|---|---|
| `POST` | `/api/speak` | secondcheck.py | none (local only) | TTS-render arbitrary instruction text via ElevenLabs, streamed back as `audio/mpeg` |
| `POST` | `/api/recall-audio` | secondcheck.py | none | TTS-render the recall word list as one clip |
| `POST` | `/tracking/start` | tracking.py (Blueprint) | none | Start/reset head-sway tracking thread + webcam |
| `POST` | `/tracking/stop` | tracking.py (Blueprint) | none | Stop tracking thread, release webcam |
| `GET` | `/tracking/snapshot` | tracking.py (Blueprint) | none | Poll current sway/face-detection state |

**Process 2 — the report viewer, `localhost:8080`**, launched via `python report.py` (§4.10), entirely separate from Process 1:

| Method | Path | Module | Auth | Purpose |
|---|---|---|---|---|
| `GET` | `/` | report.py | none | Renders the HTML report from `report_data.json` |
| `GET` | `/report` | report.py | none | Same as `/` — alias |
| `GET` | `/api/report` | report.py | none | Raw JSON mirror of `report_data.json` |

No authentication, no HTTPS, no rate limiting anywhere on either process — acceptable for a `localhost`-only prototype talking to a `file://` UI on the same device, **not acceptable if this ever becomes a networked/multi-device product**, and especially not acceptable for `report.py` specifically, which is explicitly meant to display real patient data to a clinician (flag for the security review once real patient data is involved — HIPAA-relevant if deployed clinically in the US; see also §6.10 on `report.py`'s `debug=True` + `host="0.0.0.0"` combination).

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

### 6.4 No Stage 1 / Stage 2 implementation in this repo (though the shape is now known)
`index.html`'s title ("Stage 3 Assessment") and its "Waiting for Stage 1 signal" screen refer to a real staged pipeline — see §1's breakdown from `report_data.json`. Stage 1 (pupil/blink/PLR/HRV via glasses + a "Presage" device) and the gate/fusion decision that follows it are **fully described in the report data schema but have zero implementing code** anywhere in this repo. Whoever picks this up needs to: (a) find or build whatever "Presage" is, (b) build a Stage 1 capture flow analogous to `index.html`'s Stage 3 flow, and (c) implement the fusion/gating logic that decides `stage3_triggered` — none of which currently exists even as a stub. The "Simulate signal" button in `index.html` is a placeholder for the gate decision's escalation-to-Stage-3 signal.

### 6.5 Hardcoded LAN IP for the glasses' IR eye camera
`STREAM_URL = "http://10.94.64.101:81/stream"` appears in both `calibrate.py` and `main.py`'s pupil tracker — a hardcoded local network address for the smart glasses' onboard IR eye camera (ESP32-CAM-class module). Not configurable via env var (unlike `tracking.py`'s `TRACKING_CAMERA_INDEX`/`TRACKING_MODEL_PATH` pattern for the webcam) — worth normalizing before this goes to a second physical glasses unit, since every unit will get a different DHCP-assigned IP.

### 6.6 Fixed, non-randomized recall word list
The 5 recall words are hardcoded and identical on every run (`["apple", "dog", "green", "road", "seven"]`). For a repeatable clinical memory test, a fixed word list is a validity problem (patients could memorize expected words after repeat administration) — likely needs a word bank with per-session randomization.

### 6.7 Dead tooling artifact removed: `.git-rewrite/`
Confirmed tracked-but-useless and deleted from the repo (see §4.9) — a `git filter-repo`/history-rewrite scratch directory that had no relationship to the running application, just an old snapshot of source files from mid-rewrite. `git rm -r --cached` plus a filesystem delete; no source files were touched.

### 6.8 `report.py` is an island — nothing feeds it real data
`report.py` (§4.10) is a complete, working report renderer, but it reads a single static, hand-authored `report_data.json` fixture. No code anywhere — not `index.html`, not `secondcheck.py`, not `tracking.py`, not `main.py` — writes a file in that shape. To make the report real: (1) Stage 3's results in `index.html` need to actually be collected (see §6.3) and shaped into the `stage3` block of the schema instead of being discarded, (2) a Stage 1 pipeline needs to exist at all (§6.4), (3) something needs to implement the fusion/gating/hard-override logic and write the final `outcome`/`fusion`/`narration` blocks, and (4) that something needs to write to `report_data.json` (or `report.py` needs to be pointed at wherever the real pipeline lands its output — e.g. a per-session file or a database row instead of one hardcoded filename). This is the single highest-value integration task now that all the pieces (capture, scoring vocabulary via `status_class`, and rendering) individually exist.

### 6.9 SETUP.md does not mention `report.py` at all
`SETUP.md` documents how to run `secondcheck.py` and open `index.html`, but has no section for installing/running `report.py`, no mention of port `8080`, and no note that `report_data.json` must exist first (or that a fresh clone already ships the sample fixture, so `report.py` will actually work out of the box unlike the rest of the untested-integration pieces). Worth a "Report viewer" section addition next time SETUP.md is touched.

### 6.10 `report.py` runs with `debug=True` on `host="0.0.0.0"`
`app.run(host="0.0.0.0", port=8080, debug=True)` — this binds to all network interfaces (not just localhost) *and* enables Flask's interactive debugger, which is a known remote-code-execution vector if the debugger endpoint is reachable by anyone other than the developer (Werkzeug's debugger has no auth by default in many configurations). Fine for a demo on a trusted local network; must be changed (`debug=False`, and probably bind back to `127.0.0.1` unless cross-device access on the LAN is actually intended) before this is exposed anywhere less trusted, and definitely before any real patient data flows through it.

---

## 7. Summary: build status by product component

| Component | Status | File(s) |
|---|---|---|
| Assessment UI / state machine (Stage 3) | Built, functional | `index.html` |
| Spoken instructions (TTS) | Built, functional (needs API key) | `secondcheck.py` |
| Recall memory test (verbal) | Built, but fixed word list + results discarded | `index.html` |
| Head-sway tracking (balance/pursuit/VOR signal) | Built, functional as a service; **not consumed by the UI during tests** | `tracking.py`, `secondcheck.py` |
| Pupil/gaze tracking (dilation, gaze deviation) | Built as a standalone desktop tool; **not networked, not integrated with anything** | `main.py` |
| Calibration/tuning tooling | Built (manual CLI) | `calibrate.py` |
| Report viewer (clinician-facing HTML report) | Built, functional, renders real layouts — **but only from a hand-authored sample fixture** | `report.py`, `report.html`, `style.css` |
| Report data schema | Defined (de facto, via the sample fixture) — see §4.10 | `report_data.json` |
| Results storage / persistence | **Not started** — no DB, no per-session files, `report_data.json` is a single static fixture | — |
| Stage 1 capture pipeline (pupil/blink/PLR/HRV via glasses + "Presage") | **Not started** — schema defined, zero implementing code, "Presage" device/integration unidentified | — |
| Stage 2 / gate decision / fusion / hard-override logic | **Not started** — schema and thresholds partially implied by `report.py`'s `status_class()` and the sample `hard_overrides`/`fusion` blocks, but no scoring engine exists | — |
| Pipe from Stage 3 results → `report_data.json` | **Not started** — the single highest-value integration task, see §6.8 | — |
| Vitals ingestion | **Not started** (empty placeholder) | `vitals-server/` |
| Baseline comparison / longitudinal tracking across injuries | **Not started** | — |
| Auth / patient identity / HIPAA-relevant controls | **Not started** | — |
| QNX/Raspberry Pi fusion node (physical orchestration hardware) | **Not started** — named in `report_data.json.metadata.devices.fusion_node` and `SETUP.md`'s QNX references, no code anywhere | — |

---

## 8. Orientation for whoever (human or AI agent) works on this next

This section exists so a new contributor — especially an AI coding agent starting a fresh session with no memory of this repo — doesn't have to re-derive the facts above by reading every file from scratch. Read this section first; it links back into the detailed sections above by number.

**The one-paragraph mental model:** three independent processes exist (`secondcheck.py`:3001 running the Stage 3 assessment + head-sway tracking, `main.py` a desktop-only pupil tracker with no server, `report.py`:8080 rendering a static sample report) plus a fully-specified-but-unbuilt data pipeline (Stage 1 capture → gate/fusion decision → `report_data.json`) that would connect them. Nothing currently writes real data between any of these pieces. If you're asked to "unify" or "connect" this codebase, the work is almost certainly: (a) get Stage 3 to actually record its results instead of discarding them (§6.3), (b) shape those results into the `report_data.json` schema (§4.10, §6.8), (c) figure out what Stage 1 needs to be and build a capture flow for it (§6.4), and (d) write the fusion/gating logic that ties it all together and produces the final report payload.

**Fast facts an agent should not have to rediscover:**

- **Run the server with `python secondcheck.py`, never `main.py`.** `main.py` is a standalone desktop pupil-tracker CLI with zero Flask code in it, despite the filename suggesting otherwise. This is the single most common wrong turn in this repo — see §6.2.
- **Two cameras, two subsystems, never confuse them:** the **laptop/tablet webcam** feeds `tracking.py` (head-sway, nose-tip landmark, served over HTTP on `/tracking/*`). The **smart glasses' onboard IR eye camera** (MJPEG stream, hardcoded at `http://10.94.64.101:81/stream`) feeds `main.py` (pupil detection/dilation, desktop-only, no HTTP). See §6.1.
- **Three Flask processes, three ports, no shared state:** `secondcheck.py` (3001), `report.py` (8080), and no third process yet for the not-built fusion node. None of them import from each other or share a database — moving data between them today means writing new integration code, not flipping a flag.
- **`report_data.json` is the closest thing this repo has to an API contract.** If you need to know what fields a "finished assessment" is supposed to have, what the clinical thresholds/status vocabulary looks like (`normal`/`borderline`/`elevated`/etc. via `report.py`'s `status_class()`), or what hardware the system assumes exists (`metadata.devices`), read that file and §4.10 before inventing your own shape.
- **"Stage 1", "Stage 2", "Stage 3" are real, specific pipeline stages**, not vague labels — see §1's full breakdown. Stage 3 (Balance/Pursuit/VOR/Recall) is the only one with implementing code. Stage 1 (pupil/blink/PLR/HRV + a device called "Presage") and Stage 2 (the gate/fusion decision) exist only as fields in `report_data.json`.
- **"QNX" and "Presage" are real external references, not placeholders to ignore.** `metadata.devices.fusion_node` in `report_data.json` says `"Raspberry Pi (QNX)"`, matching `SETUP.md`'s "what QNX talks to" endpoint table and `tracking.py`'s docstring ("QNX polls /tracking/snapshot"). `hrv.source` says `"Presage (laptop)"`. Neither has any code in this repo — if you're asked to integrate with either, that's an external system/hardware integration task, not something to stub out casually, and it's worth explicitly asking the user what these already are (existing hardware? a vendor SDK?) before guessing.
- **No persistence layer exists anywhere.** No database, no per-session files being written, no patient/session identity system. `report_data.json` is a single static file, not a per-session record. If a task implies "save the results" or "look up a prior baseline," that storage layer needs to be designed, not found.
- **No tests, no CI, no build tooling, no linter config** anywhere in the repo, for either the Python or the HTML/JS side. Don't assume `npm test` or `pytest` do anything meaningful — check for their absence before relying on them, and don't invent a test framework unasked.
- **Recall word list is hardcoded and identical every run** (`["apple", "dog", "green", "road", "seven"]` in `index.html`) — if you're asked to make the memory test more clinically rigorous, this is the spot, and per-session randomization is the obvious first fix (§6.6).
- **Every network call in `index.html` is fire-and-forget with a swallowed catch.** TTS failures, tracking-start failures, etc. never surface an error to the user — the UI is deliberately built to never block or fail visibly on a backend hiccup. Preserve this resilience pattern (it's intentional, documented in `tracking.py`'s comments) rather than "fixing" it into throwing errors, unless the task specifically calls for surfacing failures.
- **`report.py` has a real security issue as shipped** (`debug=True` + `host="0.0.0.0"`, §6.10) — don't copy this pattern into new server code, and flag it if asked to productionize anything.
- **Before adding a new top-level Python file, check whether its name will collide in meaning with an existing one** — this repo already has one confirmed case (`main.py` vs `secondcheck.py`, §6.2) where filenames actively lie about their role. `server.py` / `report.py` / `tracking.py` / `main.py` (pupil tool) is the current, not-entirely-self-explanatory set; consider proposing clearer names rather than adding a fifth confusingly-named file.
- **This README is the map, not the code.** If anything above turns out to be stale (a file moved, a route changed), trust the current source over this document and update the relevant section rather than propagating the discrepancy — see the note in each section about where the underlying facts were verified (git history, `git ls-tree`, direct file reads).

# Report Wrapper Documentation

The report system is a clinician-facing tool that renders assessment data into a professional, printable HTML report. It consists of four interconnected components:

- **report.py** – Flask backend that loads data and renders templates
- **report.html** – Jinja2 template that defines report layout and structure
- **report_data.json** – Assessment data in standardized JSON format
- **style.css** – Visual styling, colors, and responsive layout

The system reads a JSON file containing all assessment metrics and transforms it into a styled, interactive HTML document suitable for hospital charts and clinical review.

---

## report.py – Backend Server

### Purpose
Runs a Flask HTTP server on port 8080 that loads assessment data from disk and renders it into a templated HTML report. This is a standalone application separate from the main assessment UI.

### Key Responsibilities
- Loads and validates `report_data.json` on each request
- Provides error handling with appropriate HTTP status codes (503 if data missing, 500 if invalid JSON)
- Formats timestamps from ISO 8601 to human-readable text
- Maps metric status values to semantic CSS classes for visual styling
- Passes data and helper functions to the template engine

### Core Functions

**Data Loading**
Reads `report_data.json` from the same directory as the script. Validates that the file exists and contains valid JSON structured as a single object. Returns appropriate error responses if data is missing or malformed.

**Timestamp Formatting**
Converts ISO 8601 datetime strings (with timezone information) into readable format suitable for clinical display. Falls back gracefully to the original string if parsing fails.

**Status Classification**
Maps free-text status strings (e.g., "elevated_variance", "borderline", "normal") into four semantic CSS classes:
- `good` (green) – normal findings
- `warn` (orange) – borderline findings
- `alert` (red) – elevated/abnormal findings
- `neutral` (gray) – unclassified

This mapping serves as a reference for clinical thresholds and vocabularies used throughout the assessment.

### HTTP Routes

**`GET /` or `GET /report`**
- Returns the fully rendered HTML report
- Loads data from disk
- Builds template context with all variables needed for rendering
- Applies formatting and status classification

**`GET /api/report`**
- Returns raw JSON from `report_data.json`
- Provides machine-readable access to the same data

### Running the Server
Execute `python report.py` to start the Flask development server. The server listens on `http://localhost:8080` with debug mode enabled. Note: debug mode and `host="0.0.0.0"` together are development-only; production deployment requires a proper WSGI server (Gunicorn, etc.).

### Template Context Variables
The Flask route builds a context dictionary containing:
- Complete assessment data and all nested objects
- Extracted outcome, stage1, stage3, fusion, narration, metadata
- Formatted timestamp (human-readable)
- CSS class for outcome banner color
- Reference to status_class function for inline template use

---

## report.html – Template Structure

### Purpose
Jinja2 template that renders assessment data into a structured, accessible HTML report. The template is defensive against partial or missing data, using fallbacks throughout.

### Core Characteristics
- Responsive mobile-first layout that scales from phone to desktop
- Conditional section rendering (sections hidden if data absent or flags false)
- Status-aware styling (metric cards color-coded by clinical status)
- Semantic HTML with accessibility attributes
- Print-friendly design with optimized print styles

### Document Sections

**Header**
Contains session identification, assessment timestamp, and participant information. Uses full-width gradient background with white text. Displays session ID, formatted timestamp, and optional participant name in a styled metadata card.

**Disclaimer**
Always-visible legal notice stating the device does not provide medical diagnosis and results are for sideline triage only. Blue-accented styling draws attention without alarming.

**Outcome Banner**
Primary result display showing the screening verdict. Includes headline text, SCAT5 severity score, and return-to-play clearance status. Colors dynamically based on verdict: red for "refer", green for "cleared". Two-column layout with outcome text on left and SCAT5 score card on right.

**Summary Grid**
Three-column display of gate decision, assessment duration, and fusion composite score. Each metric shown in a card with label and prominent numeric value. Responsive: stacks to single column on narrow screens.

**Hard Override Flags** (Conditional)
Red warning panel that appears only if medical emergency flags are triggered. Lists specific flag details (anisocoria asymmetry measurement, severe VOR deviation ratio). Indicates urgent referral required regardless of other scores.

**Narration** (Conditional)
Plain-language summary of findings written by the assessment system. Optionally includes bulleted list of clinical recommendations or next steps. Only renders if narration data is provided.

**Stage 1 Metrics**
Four metric cards covering primary screening: pupil diameter, blink dynamics, pupillary light response, heart rate/HRV. Each card displays relevant measurements with reference ranges. Color-coded by clinical status. Includes composite score for Stage 1.

**Stage 3 Metrics** (Conditional)
Four metric cards covering secondary battery (if triggered): balance, smooth pursuit, VOR, delayed recall. Layout and styling identical to Stage 1 cards. Only displays if `outcome.stage3_triggered` is true. Includes composite score for Stage 3.

**Device Footer** (Conditional)
Lists capture devices used in assessment and report schema version. Only displays if metadata is provided. Tracks which hardware/software components generated the data.

### Template Features

**Data Handling Patterns**
- Uses fallback values (em-dash) for missing fields
- Conditional blocks for optional sections
- Call to status_class function to determine card colors
- String filters for formatting (capitalization, underscores, decimal places)

**Conditional Rendering**
- Entire Stage 3 section hidden if not triggered
- Hard override panel hidden if no flags triggered
- Narration section hidden if summary absent
- Device list hidden if metadata absent

---

## report_data.json – Data Schema

### Purpose
Single source of truth for all assessment metrics. Currently a hand-authored sample, but defines the data contract that a real pipeline must produce. Structured as a JSON object with nested fields for each stage, scores, and metadata.

### Top-Level Structure

**Session Information**
- Session ID: unique identifier for the assessment
- Patient label: optional participant name or identifier
- Assessed at: ISO 8601 timestamp with timezone
- Assessment duration: total seconds from start to finish

**Outcome**
Contains the final verdict and severity scores. Includes flag ("refer" or "clear"), headline text, SCAT5 score, gate decision status, and return-to-play clearance boolean. Tracks whether Stage 3 was triggered.

**Hard Overrides**
Medical emergency flags that trigger urgent referral:
- Anisocoria: pupil size asymmetry measurements with threshold
- Severe VOR: vestibulo-ocular reflex deviation ratio with threshold
Each has a triggered boolean and detailed measurements.

**Stage 1 Metrics**
Primary screening measurements including pupil characteristics, blink dynamics, pupillary light response, and heart rate/HRV data. Each metric has a status field and a composite score for the stage.

**Stage 3 Metrics** (Optional)
Secondary battery if escalated. Contains balance sway measurements, smooth pursuit correlation values, VOR variance data, and delayed recall word lists. Only present if Stage 3 triggered. Includes composite score.

**Fusion**
Composite scoring information. Defines weighting between Stage 1 and Stage 3. Contains final weighted composite score and text explaining the decision basis.

**Narration** (Optional)
Plain-language clinical summary. May include human-readable explanation of findings and bulleted recommendations for follow-up.

**Metadata** (Optional)
Device identification and schema versioning. Lists hardware/software components used (eye tracker, heart rate monitor, fusion node platform). Tracks report and assessment schema versions for compatibility tracking.

### Status Field Values

Status fields throughout the schema use consistent string values:
- `"normal"` – within expected range
- `"borderline"` – near threshold but not exceeded
- `"ambiguous"` – unclear, requires further evaluation
- `"mild_deviation"` – slightly abnormal
- `"elevated"` or `"elevated_variance"` or `"elevated_hr"` – moderately abnormal
- `"deviation"` – significantly abnormal

These map to the CSS status classes in report.py.

### Field Types

Timestamps are ISO 8601 format with timezone. Numeric values use appropriate precision (millimeters for distance, hertz for frequency, percentage for ratios, milliseconds for timing). Arrays contain strings for word lists and device names. Nested objects organize related measurements.

---

## style.css – Styling System

### Purpose
Defines visual design, responsive layout, typography, and status-aware color coding. Uses CSS custom properties (variables) for theming and mobile-first responsive design.

### Color Palette

**Brand Colors**
Primary blue for headers and interactive elements. Light blue for soft backgrounds and overlays.

**Verdict Colors**
Red family for "refer" verdict (urgent, requires medical evaluation). Green family for "clear" verdict (passed screening).

**Status Colors**
- Green for normal/good findings
- Orange for borderline/warning findings
- Red for elevated/alert findings
- Gray for neutral/unclassified findings

**Neutral Colors**
- Light background for page
- White for card surfaces
- Dark gray for primary text
- Medium gray for secondary text
- Light gray for borders

**Effects**
Subtle drop shadow on cards (soft, not aggressive). Rounded corners on cards (18px) and smaller components (12px).

### Typography

**Font Family**
System font stack prioritizing Segoe UI, Helvetica Neue, Arial, sans-serif for good rendering across platforms.

**Font Sizes**
- Headers scale responsively with viewport width (using CSS clamp)
- Body text: 0.95–1rem for readability
- Secondary text: 0.78–0.88rem for labels and captions
- Metric values: 1.5rem+ bold for prominence

**Line Height**
- Body: 1.55 for readable spacing
- Headers: 1.15 for compact display

### Layout Components

**Container**
Maximum width of 1100px with 1rem padding on each side. Centers content horizontally.

**Header**
Full-width gradient background with white text. Two-column flex layout: brand on left, session metadata on right. Substantial padding for visual prominence.

**Main Content**
Vertical padding for breathing room between sections.

**Grid Layouts**
- Three-column grid for summary cards (gate decision, duration, composite)
- Two-column grid for metric cards (pupil, blink, PLR, HR/HRV)
- Single-row flex for header components

**Responsive Design**
Mobile-first approach with no hard breakpoints. Uses CSS Grid/Flex defaults that naturally stack on narrow screens. Font sizes scale fluidly with viewport width using `clamp()` function. Padding and margins scale proportionally.

### Component Styling

**Cards**
White background with light gray border. Soft shadow for depth. 18px rounded corners. 1.1–1.3rem padding. Consistent spacing across all card types.

**Metric Cards**
Colored top border (4px) based on status class. Key-value pairs displayed as flex layout (label left, value right). Dashed border separators between rows. Status badge (pill) positioned in header.

**Status Pill**
Small inline badge displaying metric status. Background and text color match status (green/orange/red/gray). Fully rounded appearance. Capitalized, non-wrapping text.

**Outcome Banner**
Two-column layout: outcome copy on left (1.4fr width), SCAT5 score card on right (0.8fr width). Gradient background colors dynamically based on verdict. SCAT5 score displayed large and bold.

**Override Panel**
Red background and borders indicating urgency. Compact bulleted list of triggered flags. Only appears if flags present.

### Theming System

All colors, spacing, and effects use CSS custom properties (variables) defined at root level. This allows:
- Easy global color changes by updating variable values
- Consistency across all components
- Future dark mode support by redefining variables

---

## Data Flow

Assessment pipeline writes `report_data.json` → Flask loads and validates JSON → Template context built with helper functions → Jinja2 renders HTML with conditional sections → Browser fetches stylesheet → Styled report displays in browser.

Each stage transforms the data into a more human-friendly format:
1. Raw JSON (machine format)
2. Python dict (validated)
3. Enhanced context (timestamps formatted, statuses mapped to CSS classes)
4. Templated HTML (semantic structure with conditionals)
5. Styled display (colors, layout, typography applied)

---

## Extension Points

### Adding New Metrics
1. Pipeline writes new field to report_data.json
2. Flask extracts field (transformation if needed)
3. Template adds new card in appropriate section
4. CSS status class styling applied automatically

### Changing Colors
Update CSS custom property values in the style root. Single source of change for brand, verdict, and status colors.

### Adding Sections
Add Jinja conditional block in template for new section. Create corresponding CSS classes for layout and styling.

### Modifying Layout
Adjust CSS Grid columns and gaps. Flex properties for component alignment. No changes to template needed for simple layout adjustments.

---

## Error Scenarios

| Situation | HTTP Response | Display Result |
|-----------|---------------|-----------------|
| report_data.json missing | 503 Service Unavailable | Server indicates pipeline hasn't written data |
| Invalid JSON syntax | 500 Internal Server Error | Server indicates malformed JSON |
| JSON not an object | 500 Internal Server Error | Server indicates wrong JSON structure |
| Missing field in data | 200 OK | Field renders as em-dash (—) |
| Timestamp parse fails | 200 OK | Original ISO string displayed as fallback |

---

## Deployment Considerations

### Development
Running `python report.py` starts Flask's development server with debug mode enabled. Suitable only for local development and trusted environments.

### Production
Requires proper WSGI server (Gunicorn, uWSGI, etc.). Debug mode must be disabled. Host should be restricted or placed behind reverse proxy for security. SSL/TLS encryption recommended for network transmission.

### File Paths
Template folder and static folder both configured to project root. Moving `report.html` or `style.css` into subdirectories requires updating Flask configuration.

### Data Source
Currently reads `report_data.json` from same directory as script on every request. No caching. No database. Suitable for prototype; real deployment would benefit from persistent storage and caching.

---

## Clinical Design Principles

The report system enforces several clinical safety patterns:

**Hard Overrides**
Medical emergency flags (anisocoria, severe VOR) are separate from normal scoring. Triggered overrides force urgent referral regardless of other scores. This reflects real clinical practice where certain findings are red flags.

**Status Vocabulary**
Status strings map to consistent CSS classes. This creates a single source of truth for what counts as normal/borderline/concerning. New metrics should use existing vocabulary.

**Defensive Templating**
Missing data renders as em-dash rather than breaking or showing errors. Sections hide gracefully if not applicable. Incomplete assessments produce valid reports.

**Printability**
Report designed to print or export as PDF for physical chart insertion. Print styles remove shadows for readability on paper.

---

## Future Work

To connect the report system to real data:

1. Assessment pipeline must write `report_data.json` with correct schema after each assessment completes
2. Stage 1 capture pipeline (currently missing from repo) must feed pupil, blink, PLR, HR/HRV data
3. Fusion node (QNX/Raspberry Pi) must orchestrate stages, compute composites, and apply thresholds
4. Narration generation requires either templating engine or LLM-based clinical summary writer
5. Persistence layer needed to store reports and allow retrieval by session ID

Currently all components are present and functional, but disconnected. The data contract (report_data.json schema) is defined; implementation needs to flow data through each stage into that contract.
