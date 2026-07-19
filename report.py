"""Concussion screening report server.

Serves the HTML report at a fixed URL and ingests per-stage results into
report_data.json via report_store (file-locked; secondcheck.py writes the
server-side stages through the same store). GET /report reads the file
fresh on every request — no caching.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, url_for
from flask_cors import CORS

load_dotenv()   # GEMINI_API_KEY — finalize may run in this process

import report_store

BASE_DIR = Path(__file__).resolve().parent

app = Flask(
    __name__,
    template_folder=str(BASE_DIR),
    static_folder=str(BASE_DIR),
    static_url_path="",
)
CORS(app)

# Shown while stages are still arriving or Gemini is writing the narration;
# refreshes itself until the finalized report exists.
PENDING_HTML = """<!doctype html>
<html><head><meta http-equiv="refresh" content="2">
<title>RapidGlasses</title></head>
<body style="font-family:sans-serif;background:#0b1020;color:#dbe2ff;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<p>Generating report&hellip;</p></body></html>"""


def format_timestamp(raw: str | None) -> str:
    if not raw:
        return "—"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed.strftime("%B %d, %Y at %I:%M %p %Z").strip()
    except ValueError:
        return raw


def status_class(status: str | None) -> str:
    if not status:
        return "neutral"
    normalized = status.lower()
    if normalized in {"normal", "no_deviation", "within_range"}:
        return "good"
    if normalized in {"borderline", "mild_deviation", "ambiguous"}:
        return "warn"
    if normalized in {"elevated", "elevated_variance", "elevated_hr", "deviation"}:
        return "alert"
    return "neutral"


@app.route("/")
@app.route("/report")
def report_page():
    data = report_store.read()
    if "outcome" not in data:   # stages still arriving / narration running
        return PENDING_HTML
    outcome = data.get("outcome", {})
    flag = outcome.get("flag", "no_deviation")

    context = {
        "data": data,
        "outcome": outcome,
        "hard_overrides": data.get("hard_overrides", {}),
        "stage1": data.get("stage1", {}),
        "stage3": data.get("stage3", {}),
        "fusion": data.get("fusion", {}),
        "narration": data.get("narration", {}),
        "metadata": data.get("metadata", {}),
        "assessed_at_display": format_timestamp(data.get("assessed_at")),
        "outcome_class": "refer" if flag == "refer" else "clear",
        "css_url": url_for("static", filename="style.css"),
    }

    context["status_class"] = status_class
    return render_template("report.html", **context)


@app.route("/api/report")
def report_json():
    return report_store.read()


@app.route("/report/data/<stage>", methods=["POST"])
def ingest_stage(stage):
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify(error="body must be a JSON object"), 400
    try:
        complete = report_store.merge_stage(stage, payload)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(ok=True, complete=complete)


@app.route("/report/reset", methods=["POST"])
def reset():
    report_store.reset()
    return jsonify(ok=True)


if __name__ == "__main__":
    # threaded: the request that completes the stage set blocks on Gemini;
    # the pending page must still be servable meanwhile.
    app.run(host="0.0.0.0", port=8080, debug=True, threaded=True)
