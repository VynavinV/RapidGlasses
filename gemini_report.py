"""Gemini narration for the screening report.

The refer/clear flag and every metric in report_data.json are rule-based in
report_store.py — Gemini only writes the plain-language narration from the
already-assembled structured report (no raw sensor streams, no images).
Any failure returns None and the caller keeps its local fallback narration,
so the report never blocks on the API.

Needs GEMINI_API_KEY in .env. gemini-3.5-flash is the free-tier flash model
(checked on Google's Gemini API pricing page 2026-07-19); override with
GEMINI_MODEL in .env if free-tier availability shifts.
"""
import json
import os

import requests

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent?key={key}")

PROMPT = """You write the plain-language narration for a RapidGlasses
sideline concussion screening report. Below is the final structured report
JSON — every measurement and the rule-based outcome are already decided.
Do not change, second-guess, or recompute any of them.

Return ONLY a JSON object of this exact shape:
{"summary": "one paragraph", "recommendations": ["short item", "..."]}

Rules:
- summary: one plain-language paragraph for a non-clinician describing what
  was measured and what (if anything) deviated. Only describe values present
  in the data; treat "N/A" as not measured and NEVER invent a measurement.
- NEVER state or imply a diagnosis. Always note this is a screening, not a
  diagnosis.
- The only allowed outcome phrasings are exactly:
  "Deviation from expected — refer for medical evaluation"
  "No deviation detected — this does not clear the athlete to play"
  Echo the one matching outcome.flag. Never say the athlete is cleared to
  play.
- recommendations: 2-4 short items consistent with the outcome.

REPORT DATA:
"""


def generate_narration(report):
    """Return {"summary": str, "recommendations": [str]} or None."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        print("gemini narration skipped: no GEMINI_API_KEY")
        return None

    body = {
        "contents": [{"role": "user",
                      "parts": [{"text": PROMPT + json.dumps(report,
                                                             indent=1)}]}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    try:
        resp = requests.post(GEMINI_URL.format(model=GEMINI_MODEL, key=key),
                             json=body, timeout=20)
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        summary = data.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("no summary in Gemini response")
        recs = [r for r in data.get("recommendations", [])
                if isinstance(r, str) and r.strip()]
        return {"summary": summary.strip(), "recommendations": recs}
    except Exception as exc:  # fallback narration survives any failure
        print(f"gemini narration failed: {exc}")
        return None
