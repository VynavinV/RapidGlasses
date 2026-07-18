import atexit
import os
import subprocess

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

from tracking import tracking_bp

load_dotenv()

VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel — swap as needed
MODEL_ID = "eleven_turbo_v2_5"
ELEVENLABS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"

app = Flask(__name__)
CORS(app)
app.register_blueprint(tracking_bp)


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
