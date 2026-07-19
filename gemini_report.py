"""Writes report_data.json — the single file report.py (port 8080) renders.

Two paths, both called from secondcheck.py /api/finalize:
  write_abnormal()  round-1 pupil failure: immediate refer report, everything
                    not collected is "N/A". No network calls.
  write_full()      end of assessment: sends all collected data + sampled
                    eye-tracker frames to Gemini, which returns the report
                    JSON in the schema below. Any failure falls back to a
                    locally-built report so the demo never dies.

Needs GEMINI_API_KEY in .env for the Gemini path.
"""
import json
import os
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "report_data.json")

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
              "{model}:generateContent?key={key}")

NA = "N/A"

DEVICES = {
    "glasses": "ESP32-S3 IR stream",
    "laptop": "Presage HR/HRV + webcam sway",
    "fusion_node": "Raspberry Pi 5 (QNX)",
}

# Schema Gemini must fill. Keys mirror what report.html renders. Numeric-only
# fields (weighted_composite, scores) must be numbers or omitted entirely.
SCHEMA = {
    "session_id": "string",
    "patient_label": "Athlete",
    "assessed_at": "ISO-8601 timestamp",
    "outcome": {
        "flag": "refer | clear",
        "headline": "one-line result",
        "scat5_score": "number or N/A",
        "scat5_max": 132,
        "gate_decision": "normal | ambiguous | abnormal",
        "stage3_triggered": True,
        "cleared_to_play": False,
    },
    "hard_overrides": {
        "anisocoria": {"triggered": False},
        "vor_severe": {"triggered": False},
    },
    "stage1": {
        "pupil": {"left_mean_mm": NA, "right_mean_mm": NA,
                  "left_variance": NA, "right_variance": NA,
                  "combined_mean_mm": "pupil px value",
                  "status": "normal | elevated_variance | deviation"},
        "blink": {"rate_per_minute": NA, "mean_duration_ms": NA,
                  "reference_rate_min": NA, "reference_rate_max": NA,
                  "status": NA},
        "plr": {"stimulus_used": False, "amplitude_percent": NA,
                "reference_min_percent": NA, "status": NA},
        "hrv": {"heart_rate_bpm": NA, "hrv_rmssd_ms": NA,
                "source": "Presage (laptop)", "status": NA},
        "stage1_composite_score": "number 0-100",
        "stage1_max_score": 100,
    },
    "stage3": {
        "balance": {"sway_magnitude_px": NA, "sway_frequency_hz": NA,
                    "duration_seconds": 20, "threshold_magnitude_px": NA,
                    "status": NA},
        "smooth_pursuit": {"horizontal_correlation": NA,
                           "vertical_correlation": NA,
                           "threshold_min": NA, "status": NA},
        "vor": {"static_gaze_variance": NA, "motion_gaze_variance": NA,
                "variance_ratio": NA, "threshold_ratio": NA, "status": NA},
        "recall": {"words_presented": [], "words_recalled": [],
                   "score": 0, "max_score": 5,
                   "scoring_method": "self_marked"},
        "stage3_composite_score": "number 0-100",
        "stage3_max_score": 100,
    },
    "fusion": {
        "stage1_weight": 0.55,
        "stage3_weight": 0.45,
        "weighted_composite": "number",
        "decision_basis": "one sentence on how the decision was made",
    },
    "narration": {
        "summary": "plain-language paragraph for a non-clinician",
        "recommendations": ["list of short recommendations"],
    },
    "metadata": {"devices": DEVICES, "report_version": "1.0.0"},
}

PROMPT = """You are the report generator for RapidGlasses, a sideline concussion
screening device. Using the collected data below (and the attached annotated
IR eye-tracker frames showing the pupil ellipse, gaze trail, and dilation
trend), produce the screening report.

Return ONLY a JSON object with exactly the schema shown — same keys, no
extras. Rules:
- Fill values from the collected data. Use "N/A" for anything not measured.
  NEVER invent a measurement.
- Pupil values are in pixels from a single IR eye camera (no mm calibration,
  no left/right split — put the value in combined_mean_mm and "N/A" in
  left/right fields).
- Sway values are normalized nose-tip deviation from the webcam
  (mean_deviation ~0.005 is steady, >0.02 is notable sway). Put the balance
  test's mean deviation (converted to px is not possible — report the raw
  value) in sway_magnitude_px.
- outcome.flag must be "refer" if anything meaningfully deviates
  (pupil out of the given px range, high sway, recall score <= 3/5,
  round1 abnormal), else "clear". cleared_to_play true only when flag is
  "clear".
- Numeric fields (weighted_composite, composite scores, scat5_score) must be
  JSON numbers, or "N/A" only where the schema says N/A is allowed —
  weighted_composite must be a number.
- narration.summary: plain language, no diagnosis claims, always note this
  is a screening not a diagnosis.

SCHEMA:
{schema}

COLLECTED DATA:
{data}
"""


def _write(data):
    data.setdefault("session_id", "RG-" + time.strftime("%Y%m%d-%H%M%S"))
    data.setdefault("assessed_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    data.setdefault("metadata", {"devices": DEVICES, "report_version": "1.0.0"})
    # report.html applies numeric formatting to these — drop non-numbers
    fusion = data.get("fusion")
    if isinstance(fusion, dict) and not isinstance(
            fusion.get("weighted_composite"), (int, float)):
        fusion.pop("weighted_composite", None)
    if not isinstance(data.get("assessment_duration_seconds"), (int, float)):
        data.pop("assessment_duration_seconds", None)
    with open(DATA_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    print(f"report written: {DATA_FILE}")


def write_abnormal(reason, eye):
    """Round-1 pupil failure: immediate refer report, no Gemini."""
    round1 = (eye or {}).get("round1") or {}
    med = round1.get("median_px", NA)
    data = {
        "outcome": {
            "flag": "refer",
            "headline": "Abnormal pupil response — refer for immediate "
                        "medical evaluation",
            "scat5_score": NA,
            "scat5_max": 132,
            "gate_decision": "abnormal",
            "stage3_triggered": False,
            "cleared_to_play": False,
        },
        "hard_overrides": {
            "anisocoria": {"triggered": False},
            "vor_severe": {"triggered": False},
        },
        "stage1": {
            "pupil": {"left_mean_mm": NA, "right_mean_mm": NA,
                      "left_variance": NA, "right_variance": NA,
                      "combined_mean_mm": med,
                      "status": "deviation"},
            "blink": {"rate_per_minute": NA, "mean_duration_ms": NA,
                      "reference_rate_min": NA, "reference_rate_max": NA,
                      "status": NA},
            "plr": {"stimulus_used": False, "amplitude_percent": NA,
                    "reference_min_percent": NA, "status": NA},
            "hrv": {"heart_rate_bpm": NA, "hrv_rmssd_ms": NA,
                    "source": "Presage (laptop)", "status": NA},
        },
        "stage3": {},
        "fusion": {"decision_basis": f"Hard stop at round 1: {reason}. "
                                     "Remaining tests skipped."},
        "narration": {
            "summary": (f"During the initial pupil check the median pupil "
                        f"diameter was {med} px, outside the expected "
                        f"{round1.get('min_px', NA)}-{round1.get('max_px', NA)}"
                        " px range. The assessment was stopped immediately and "
                        "no further metrics were collected (shown as N/A). "
                        "This is a screening result, not a diagnosis."),
            "recommendations": [
                "Refer to a licensed medical professional immediately.",
                "Do not return to play.",
                "Seek emergency care if symptoms worsen.",
            ],
        },
    }
    _write(data)


def _fallback_report(collected, why):
    """Local rule-based report when Gemini is unavailable."""
    recall = collected.get("recall") or {}
    sway = collected.get("sway") or {}
    balance = sway.get("balance") or {}
    score = recall.get("score")
    deviations = []
    if isinstance(score, int) and score <= 3:
        deviations.append(f"recall {score}/{recall.get('max_score', 5)}")
    mean_dev = balance.get("mean_deviation")
    if isinstance(mean_dev, (int, float)) and mean_dev > 0.02:
        deviations.append(f"elevated sway ({mean_dev:.3f})")
    flag = "refer" if deviations else "clear"

    pupil = collected.get("pupil_px") or {}
    vitals = collected.get("vitals") or {}
    data = {
        "outcome": {
            "flag": flag,
            "headline": ("Deviation from expected — refer for medical "
                         "evaluation" if flag == "refer"
                         else "No deviation detected in this screening"),
            "scat5_score": NA, "scat5_max": 132,
            "gate_decision": "ambiguous" if flag == "refer" else "normal",
            "stage3_triggered": True,
            "cleared_to_play": flag == "clear",
        },
        "hard_overrides": {"anisocoria": {"triggered": False},
                           "vor_severe": {"triggered": False}},
        "stage1": {
            "pupil": {"left_mean_mm": NA, "right_mean_mm": NA,
                      "left_variance": NA, "right_variance": NA,
                      "combined_mean_mm": pupil.get("mean", NA),
                      "status": "normal"},
            "blink": {"rate_per_minute": NA, "mean_duration_ms": NA,
                      "reference_rate_min": NA, "reference_rate_max": NA,
                      "status": NA},
            "plr": {"stimulus_used": False, "amplitude_percent": NA,
                    "reference_min_percent": NA, "status": NA},
            "hrv": {"heart_rate_bpm": vitals.get("bpm", NA),
                    "hrv_rmssd_ms": NA, "source": "Presage (laptop)",
                    "status": NA},
        },
        "stage3": {
            "balance": {"sway_magnitude_px": balance.get("mean_deviation", NA),
                        "sway_frequency_hz": NA, "duration_seconds": 20,
                        "threshold_magnitude_px": NA,
                        "status": "deviation" if any(
                            "sway" in d for d in deviations) else "normal"},
            "smooth_pursuit": {
                "horizontal_correlation": NA, "vertical_correlation": NA,
                "threshold_min": NA,
                "status": NA},
            "vor": {"static_gaze_variance": NA, "motion_gaze_variance": NA,
                    "variance_ratio": NA, "threshold_ratio": NA,
                    "status": NA},
            "recall": {"words_presented": recall.get("words_presented", []),
                       "words_recalled": recall.get("marked_heard", []),
                       "score": recall.get("score", NA),
                       "max_score": recall.get("max_score", 5),
                       "scoring_method": "self_marked"},
        },
        "fusion": {"decision_basis":
                   f"Rule-based fallback (Gemini unavailable: {why}). "
                   + ("Deviations: " + ", ".join(deviations)
                      if deviations else "No deviations found.")},
        "narration": {
            "summary": ("Automated fallback summary. "
                        + ("Deviations noted: " + ", ".join(deviations) + ". "
                           if deviations else
                           "No metrics deviated from expected ranges. ")
                        + "This is a screening result, not a diagnosis."),
            "recommendations": [
                "Refer to a licensed medical professional for evaluation."
                if flag == "refer" else
                "No referral triggered by this screening.",
                "Monitor for worsening symptoms.",
            ],
        },
    }
    return data


def write_full(payload, eye, vitals):
    """Assessment finished normally: let Gemini write the report."""
    eye = eye or {}
    collected = {
        "round1_pupil_check": eye.get("round1"),
        "pupil_px": eye.get("pupil_px"),
        "pupil_normal_range_px": {"min": eye.get("round1", {}).get("min_px")
                                  if eye.get("round1") else None,
                                  "max": eye.get("round1", {}).get("max_px")
                                  if eye.get("round1") else None},
        "sway": payload.get("sway"),
        "recall": payload.get("recall"),
        "vitals": vitals,
    }

    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        _write(_fallback_report(collected, "no GEMINI_API_KEY"))
        return

    prompt = PROMPT.format(schema=json.dumps(SCHEMA, indent=1),
                           data=json.dumps(collected, indent=1))
    parts = [{"text": prompt}]
    for b64 in eye.get("images_b64", []):
        parts.append({"inline_data": {"mime_type": "image/jpeg",
                                      "data": b64}})
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    try:
        resp = requests.post(
            GEMINI_URL.format(model=GEMINI_MODEL, key=key),
            json=body, timeout=90)
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Gemini returned non-object JSON")
    except Exception as exc:  # any failure -> local report, never a 500
        print(f"gemini failed: {exc}")
        _write(_fallback_report(collected, str(exc)))
        return
    _write(data)
