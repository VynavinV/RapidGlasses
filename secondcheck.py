import os

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

load_dotenv()

VOICE_ID = "21m00Tcm4TlvDq8ikWAM"  # Rachel — swap as needed
MODEL_ID = "eleven_turbo_v2_5"
ELEVENLABS_URL = f"https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}"

app = Flask(__name__)
CORS(app)


@app.route("/api/recall-audio", methods=["POST"])
def recall_audio():
    words = (request.get_json(silent=True) or {}).get("words", [])
    if not words:
        return jsonify(error="No words provided"), 400

    phrase = ", ".join(words)
    resp = requests.post(
        ELEVENLABS_URL,
        headers={"xi-api-key": os.environ.get("ELEVENLABS_API_KEY", "")},
        json={"text": phrase, "model_id": MODEL_ID},
        stream=True,
    )
    if resp.status_code != 200:
        return jsonify(error="ElevenLabs request failed"), 500

    return Response(resp.iter_content(chunk_size=4096), mimetype="audio/mpeg")


if __name__ == "__main__":
    app.run(port=3001)
