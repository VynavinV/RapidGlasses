import atexit
import os
import subprocess

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

load_dotenv()   # before local imports — eye/gemini_report read env at import

import eye
import report_store
from tracking import tracking_bp

VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel — swap as needed
MODEL_ID = "eleven_turbo_v2_5"
ELEVENLABS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"

app = Flask(__name__)
CORS(app)
app.register_blueprint(tracking_bp)
app.register_blueprint(eye.eye_bp)


@app.route("/api/finalize", methods=["POST"])
def finalize():
    """End of assessment. The browser posts its own stages (sway, recall)
    straight to report.py's /report/data/<stage>; this endpoint contributes
    the server-side stages — the eye-tracker summary (stage1) and vitals —
    into the same store. The abnormal hard-stop still writes a complete
    refer report immediately."""
    payload = request.get_json(silent=True) or {}
    eye_summary = eye.summary()
    if payload.get("round1"):      # browser-confirmed verdict wins over stale
        eye_summary["round1"] = payload["round1"]

    try:
        vitals = requests.get("http://localhost:3002/vitals", timeout=1).json()
    except Exception as exc:       # vitals stage is optional
        print(f"vitals unavailable: {exc}")
        vitals = None

    if payload.get("abnormal"):
        report_store.write_abnormal(
            payload.get("reason", "pupil size out of range"), eye_summary,
            vitals)
        return jsonify(ok=True)

    # vitals before stage1: stage1 completes the required set and triggers
    # finalize, so vitals must already be in the store to reach the report
    if vitals:
        try:
            report_store.merge_stage("vitals", vitals)
        except ValueError as exc:
            print(f"vitals not merged: {exc}")
    try:
        report_store.merge_stage("stage1", {
            # placeholder round1 when no tracker was present: the report must
            # still finalize (demo runs the full flow no matter what)
            "round1": eye_summary.get("round1")
                      or {"done": False, "abnormal": False},
            "pupil_px": eye_summary.get("pupil_px"),
            "images_b64": eye_summary.get("images_b64", []),
        })
    except ValueError as exc:
        print(f"stage1 not merged: {exc}")
    return jsonify(ok=True)


def _tts(phrase):
    resp = requests.post(
        ELEVENLABS_URL,
        headers={"xi-api-key": os.environ.get("ELEVENLABS_API_KEY", "")},
        json={"text": phrase, "model_id": MODEL_ID},
        stream=True,
    )
    if resp.status_code != 200:
        return None
    return resp


@app.route("/api/recall-audio", methods=["POST"])
def recall_audio():
    words = (request.get_json(silent=True) or {}).get("words", [])
    if not words:
        return jsonify(error="No words provided"), 400

    resp = _tts(", ".join(words))
    if resp is None:
        return jsonify(error="ElevenLabs request failed"), 500
    return Response(resp.iter_content(chunk_size=4096), mimetype="audio/mpeg")


@app.route("/api/speak", methods=["POST"])
def speak():
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if not text:
        return jsonify(error="No text provided"), 400

    resp = _tts(text)
    if resp is None:
        return jsonify(error="ElevenLabs request failed"), 500
    return Response(resp.iter_content(chunk_size=4096), mimetype="audio/mpeg")


# ---- Presage vitals bridge (node) ----
# Presage has no Python SDK, so heart rate runs in a small node process
# (vitals-server/server.js). tracking.py forwards it camera frames; the
# webpage polls it at :3002/vitals. Needs SMARTSPECTRA_API_KEY in .env.

VITALS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vitals-server")


def _start_vitals_server():
    try:
        proc = subprocess.Popen(["node", "server.js"], cwd=VITALS_DIR)
    except OSError as exc:
        print(f"vitals server not started ({exc}) — heart rate unavailable")
        return
    atexit.register(lambda: proc.poll() is None and proc.terminate())


if __name__ == "__main__":
    _start_vitals_server()
    app.run(port=3001, threaded=True)
