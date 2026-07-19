# Running RapidGlasses on a new machine

Covers the Flask server (ElevenLabs proxy + head-sway tracking) and the
assessment UI. Takes about 5 minutes.

## 1. Prerequisites

- **Python 3.9–3.12.** MediaPipe has no wheels outside that range — 3.13 will
  fail at `pip install`. Check with `python3 --version`.
- A working webcam.
- Git.

## 2. Clone and create a fresh virtualenv

**Do not use the `venv/` folder that comes with the clone.** It is a Windows
venv that was committed to the repo by mistake; it will not work on macOS or
Linux, and on a second Windows machine it will still have stale paths. Always
build your own:

```bash
git clone https://github.com/VynavinV/RapidGlasses.git
cd RapidGlasses

python3 -m venv .venv            # note the dot — keeps it out of the way
source .venv/bin/activate        # macOS/Linux
# .\.venv\Scripts\Activate.ps1   # Windows PowerShell

pip install -r requirements.txt
```

`mediapipe` pulls in a large dependency tree (opencv, numpy, matplotlib) — the
first install takes a few minutes.

## 3. Provide the ElevenLabs key

`.env` is gitignored, so it does not come with the clone. Create it in the repo
root:

```
ELEVENLABS_API_KEY=sk_your_key_here
```

Without it the spoken prompts silently do nothing — the UI still runs, and head
tracking is unaffected.

## 4. Check the face model is present

```bash
ls -l models/face_landmarker.task     # expect ~3.7 MB
```

It is committed, so a clone should already have it. If it is missing or
zero-length:

```bash
mkdir -p models
curl -L -o models/face_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task
```

## 5. Grant camera permission (macOS only)

The **terminal app** owns the camera, not the browser — so macOS prompts the
terminal, not Chrome. On first run you should get a permission dialog; approve
it. If you never saw one and the camera fails, enable it manually:

System Settings → Privacy & Security → Camera → enable your terminal
(Terminal, iTerm, or VS Code). You must fully quit and reopen the terminal
afterwards for it to take effect.

## 5b. Heart rate via Presage (optional, one-time)

Presage SmartSpectra has no Python SDK, so heart rate runs in a small Node
process (`vitals-server/`). `secondcheck.py` starts and stops it automatically;
you only need to set it up once:

1. Install Node.js 18+ (`brew install node` on macOS; check with `node --version`).
2. `cd vitals-server && npm install` — downloads a few hundred MB of native
   runtimes; this is expected.
3. Get an API key from <https://physiology.presagetech.com> (create an account,
   generate a key) and add it to the project `.env`:

   ```
   SMARTSPECTRA_API_KEY=your-key-here
   ```

Without the key or `npm install`, everything else still works — the heart rate
badge just shows `--`, and `http://localhost:3002/vitals` reports why.

## 6. Run

```bash
python secondcheck.py
```

Serves on `http://localhost:3001` (and the vitals bridge on `:3002`). Leave it
running. The webpage now starts the camera on load and shows an annotated
monitor view bottom-right (same drawing as the debug window) plus a live heart
rate badge top-right; the camera stays on the whole time the page is open.

## 7. Open the UI

The Flask app serves only `/api/*` and `/tracking/*` — it does **not** serve the
HTML. Open the file directly:

```bash
open index.html          # macOS
start index.html         # Windows
```

`file://` works because `CORS(app)` allows any origin.

Then click: **Simulate signal → Begin →** (words play) **→ Next**. A calibration
screen appears for roughly 10 seconds while the camera and model warm up, then
the Balance, Pursuit, and VOR tests run.

## Live debug view (optional)

Shows the camera feed with the tracked point, the running-mean baseline, a
magnified sway vector, live fps, and a sway trace. Off by default because it
costs frames and QNX never needs it.

```bash
TRACKING_DEBUG=1 python secondcheck.py          # macOS/Linux
```
```powershell
$env:TRACKING_DEBUG='1'; python secondcheck.py  # Windows PowerShell
```

The window opens when tracking starts and closes when it stops.

## Endpoints (what QNX talks to)

| Method | Path                 | Purpose                                        |
|--------|----------------------|------------------------------------------------|
| POST   | `/tracking/start`    | Open camera, reset the running-mean baseline    |
| POST   | `/tracking/stop`     | Stop the loop, release the camera               |
| GET    | `/tracking/snapshot` | Latest reading — no frame work, just a read     |

Snapshot returns:

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

`deviation` is the sway value: Euclidean distance of the current nose position
from its running mean, in normalized frame units. Poll at ~30 Hz. If `fps`
reports lower than that, back the poll rate off to match — otherwise you are
oversampling stale data. `face: false` means the reading is stale (the point and
`timestamp` are from the last good frame). `error` non-null means the tracker
died and needs a restart.

Watch it live:

```bash
while true; do curl -s localhost:3001/tracking/snapshot; echo; sleep 0.3; done
```

## Troubleshooting

**`pip install` fails on mediapipe** — Python version is outside 3.9–3.12.

**Calibration hangs, then says "Camera unavailable"** — check
`curl -s localhost:3001/tracking/snapshot` for the `error` field. Usually
another app holds the camera (Zoom, Teams, Photo Booth); quit it and retry. On
macOS it is more often the permission in step 5.

**`face` never becomes true** — the loop is running but sees nobody. Improve
lighting, get within about a metre, face the camera.

**Wrong camera picked** (external webcam vs built-in):

```bash
TRACKING_CAMERA_INDEX=1 python secondcheck.py
```

**404 on `http://localhost:3001/index.html`** — expected. Open the file
directly (step 7).

**Port 3001 already in use** — a previous server is still alive:

```bash
lsof -ti:3001 | xargs kill        # macOS/Linux
```
```powershell
Get-NetTCPConnection -LocalPort 3001 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

**First reading takes ~10 seconds** — expected. Camera init plus model load.
The calibration screen absorbs it, and the camera then stays open through VOR,
so it is paid once per assessment, not once per test.

## Repo cleanup worth doing

`venv/` is listed in `.gitignore` but was committed before that rule existed, so
git still tracks it. Every `pip install` shows up as hundreds of modified
binaries in `git status` and causes merge conflicts. To fix, once, on one
machine:

```bash
git rm -r --cached venv
git commit -m "stop tracking venv"
```

The folder stays on disk; git just stops watching it. Everyone else should
delete their local `venv/` after pulling and use `.venv/` per step 2.

## Eye tracker (QNX Raspberry Pi 5)

The Pi runs the ML end-to-end and serves the results; the laptop finds the
Pi on its own and pulls from it. Only the ESP32 address is ever configured.

```
ESP32 IR cam --> eye_tracker.py (Pi/QNX, serves :8130)
                       /eye/video  /eye/snapshot  /eye/summary
                       + UDP beacon on :8131 ("here I am")
                              |  laptop discovers + pulls
                              v
                 secondcheck.py :3001 (eye.py relay)
                              |
       index.html <-- /eye/video (MJPEG) + /eye/snapshot (unchanged routes)
```

Any laptop on the same network that runs `secondcheck.py` will find the
tracker automatically via the beacon. If broadcast is blocked on your
network, pin it in the laptop `.env`: `EYE_TRACKER_URL=http://<pi-ip>:8130`.

On the Pi (needs python3 with opencv and numpy only — all networking is
Python stdlib, no pip packages. The repo `venv/` is macOS-only, don't use it
there. The opencv build needs no GUI and no video IO backend: the MJPEG
stream is parsed over plain HTTP):

1. `git clone` the repo (already done if you're reading this on the Pi).
2. Edit **`eye_config.json`** — this is the only file you should need to touch:

```json
{
  "eye_stream_url": "http://<esp32-ip>:81/stream",
  "round1_seconds": 8.0,
  "pupil_min_px": 14,
  "pupil_max_px": 80
}
```

3. Run `./start_tracker.sh` — it keeps `eye_tracker.py` alive forever
   (restarts 3 s after any crash) and logs to `eye_tracker.log`. Watch it
   with `tail -f eye_tracker.log`.

Env vars with the same names uppercased (`EYE_STREAM_URL`, `PUPIL_MIN_PX`,
...) override the json for one-off runs. Headless — no windows, no GUI deps;
it reconnects to the ESP32 on its own, and the laptop reconnects to it.

### Run at boot (QNX)

QNX runs `/etc/rc.d/rc.local` at startup if it exists and is executable.
As root (`su`), with your actual repo path (`pwd` inside the repo to check —
note QNX images often put home under `/data/home/...`):

```sh
mkdir -p /etc/rc.d
echo '#!/bin/sh' > /etc/rc.d/rc.local
echo '/data/home/qnxuser/RapidGlasses/start_tracker.sh &' >> /etc/rc.d/rc.local
chmod +x /etc/rc.d/rc.local
```

Reboot and check `eye_tracker.log` to confirm. Network coming up late is
fine — the tracker retries the ESP32 and the laptop forever.

## Assessment flow

1. **Round 1** — the waiting screen shows the annotated eye stream full size.
   The Pi measures the median pupil diameter for ~8 s. Abnormal (blown or
   constricted) → the server writes an immediate "refer" report (everything
   uncollected is N/A) and the browser jumps to the report. Normal → the
   assessment starts, and the eye view moves to the bottom-right pip above
   the webcam feed.
2. **Tests** — balance / pursuit / VOR each capture the head-sway mean/max
   from `/tracking/snapshot` when they finish.
3. **Finalize** — after recall, the browser POSTs everything to
   `/api/finalize`. The server adds pupil stats, sampled eye-tracker frames,
   and vitals, then calls Gemini to write `report_data.json`
   (`GEMINI_API_KEY` in `.env`; without it a rule-based fallback report is
   written instead). The browser then redirects to the report.

Run the report server alongside `secondcheck.py`:

```bash
python report.py        # serves the report at http://localhost:8080/report
```
