"""Concussion screening report server.

Serves a read-only HTML report at a fixed URL. Assessment data is read from
report_data.json (written by the fusion node / hardware pipeline).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, render_template, url_for

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "report_data.json"

app = Flask(
    __name__,
    template_folder=str(BASE_DIR),
    static_folder=str(BASE_DIR),
    static_url_path="",
)


def load_report_data() -> dict:
    if not DATA_FILE.exists():
        abort(
            503,
            description=(
                "Report data is not available yet. "
                "The assessment pipeline has not written report_data.json."
            ),
        )

    try:
        with DATA_FILE.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        abort(500, description=f"Report data file is invalid JSON: {exc}")

    if not isinstance(data, dict):
        abort(500, description="Report data must be a JSON object.")

    return data


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
    data = load_report_data()
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
    return load_report_data()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=True)
