"""Single local store for report_data.json — one file, one patient run.

Stages land here independently (browser posts sway/recall through report.py's
POST /report/data/<stage>; secondcheck.py merges stage1/vitals directly).
Every access goes through a cross-process file lock, so concurrent posts
from the two Flask processes can't clobber each other.

Once all required stages are present, the final report contract that
report.html renders is built here, rule-based — the refer/clear flag is
never LLM-decided. gemini_report.generate_narration() then replaces the
local fallback narration with Gemini's plain-language summary.
"""
import json
import os
import time

from filelock import FileLock

import gemini_report

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "report_data.json")

_lock = FileLock(DATA_FILE + ".lock")

NA = "N/A"

REQUIRED_STAGES = {"stage1", "balance", "pursuit", "vor", "recall"}
# vitals is optional: merged if it arrives before completion, else N/A.
SWAY_FIELDS = ("mean_deviation", "max_deviation", "samples")
REQUIRED_FIELDS = {
    "stage1": ("round1",),
    "balance": SWAY_FIELDS,
    "pursuit": SWAY_FIELDS,
    "vor": SWAY_FIELDS,
    "recall": ("words_presented", "marked_heard", "score", "max_score"),
    "vitals": ("bpm",),
}

BALANCE_SWAY_THRESHOLD = 0.02  # normalized nose-tip deviation
VOR_SEVERE_THRESHOLD = 0.05    # hard override, ~2.5x the balance threshold
RECALL_REFER_MAX = 3           # score <= this (of 5) triggers refer

HEADLINE_REFER = "Deviation from expected — refer for medical evaluation"
HEADLINE_CLEAR = ("No deviation detected — this does not clear the athlete "
                  "to play")

DEVICES = {
    "glasses": "ESP32-S3 IR stream",
    "laptop": "Presage HR/HRV + webcam sway",
    "fusion_node": "Raspberry Pi 5 (QNX)",
}


def _read_locked():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_locked(data):
    with open(DATA_FILE, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def read():
    with _lock:
        return _read_locked()


def reset():
    """Clear the store for the next patient run."""
    with _lock:
        if os.path.exists(DATA_FILE):
            os.remove(DATA_FILE)
    print("report store reset")


def merge_stage(stage, payload):
    """Merge one stage's payload under stages[<stage>] only. Raises
    ValueError (caller maps to 400) on unknown stage or missing fields.
    Returns True once all required stages are present; the call that
    completes the set also builds the final report."""
    if stage not in REQUIRED_FIELDS:
        raise ValueError(f"unknown stage '{stage}' — expected one of "
                         + ", ".join(sorted(REQUIRED_FIELDS)))
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    missing = [f for f in REQUIRED_FIELDS[stage] if payload.get(f) is None]
    if missing:
        raise ValueError(f"{stage} missing required fields: "
                         + ", ".join(missing))
    if stage in ("balance", "pursuit", "vor"):
        bad = [f for f in SWAY_FIELDS
               if not isinstance(payload[f], (int, float))]
        if bad:
            raise ValueError(f"{stage} fields must be numeric: "
                             + ", ".join(bad))

    with _lock:
        data = _read_locked()
        stages = data.setdefault("stages", {})
        stages[stage] = dict(payload,
                             received_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        complete = REQUIRED_STAGES <= set(stages)
        claim = complete and not data.get("finalized")
        if claim:
            data["finalized"] = True   # so only one process runs finalize
        _write_locked(data)
    print(f"stage merged: {stage} (complete={complete})")
    if claim:
        _finalize(stages)
    return complete


def _finalize(stages):
    """All required stages present: resolve the flag rule-based, then let
    Gemini rewrite the narration (fallback text survives any failure)."""
    report = _build_report(stages)
    # Gemini gets the report minus the base64 frames — images are token-heavy
    # and the narration prompt is text-only.
    slim = dict(report, stage1={k: v for k, v in report["stage1"].items()
                                if k != "eye_path_images"})
    narration = gemini_report.generate_narration(slim)
    if narration:
        report["narration"] = narration
    with _lock:
        data = _read_locked()
        data.update(report)
        _write_locked(data)
    print("report finalized")


def _build_report(stages):
    stage1 = stages.get("stage1") or {}
    round1 = stage1.get("round1") or {}
    pupil_px = stage1.get("pupil_px") or {}
    balance = stages.get("balance") or {}
    vor = stages.get("vor") or {}
    recall = stages.get("recall") or {}
    vitals = stages.get("vitals") or {}

    # Hard override first: severe VOR head instability outranks the
    # composite rules (anisocoria stays stubbed — single eye camera).
    vor_dev = vor.get("mean_deviation")
    vor_severe = (isinstance(vor_dev, (int, float))
                  and vor_dev > VOR_SEVERE_THRESHOLD)

    deviations = []
    if vor_severe:
        deviations.append(f"severe VOR head instability ({vor_dev:.3f})")
    if round1.get("abnormal"):
        deviations.append(
            f"round-1 pupil {round1.get('median_px', NA)} px out of range")
    score = recall.get("score")
    if isinstance(score, int) and score <= RECALL_REFER_MAX:
        deviations.append(f"recall {score}/{recall.get('max_score', 5)}")
    bal_dev = balance.get("mean_deviation")
    if isinstance(bal_dev, (int, float)) and bal_dev > BALANCE_SWAY_THRESHOLD:
        deviations.append(f"elevated sway ({bal_dev:.3f})")
    flag = "refer" if deviations else "clear"

    if vor_severe:
        basis = (f"Hard override: VOR mean deviation {vor_dev:.3f} exceeds "
                 f"{VOR_SEVERE_THRESHOLD}. ")
    else:
        basis = "Rule-based fusion. "
    basis += ("Deviations: " + ", ".join(deviations) + "."
              if deviations else "No deviations found.")

    return {
        "session_id": "RG-" + time.strftime("%Y%m%d-%H%M%S"),
        "assessed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "outcome": {
            "flag": flag,
            "headline": HEADLINE_REFER if flag == "refer" else HEADLINE_CLEAR,
            "scat5_score": NA, "scat5_max": 132,
            "gate_decision": "ambiguous" if flag == "refer" else "normal",
            "stage3_triggered": True,
            "cleared_to_play": False,   # a screening never clears to play
        },
        "hard_overrides": {
            "anisocoria": {"triggered": False},
            "vor_severe": {
                "triggered": vor_severe,
                "gaze_variance_ratio":
                    round(vor_dev / VOR_SEVERE_THRESHOLD, 2)
                    if isinstance(vor_dev, (int, float)) else NA,
                "threshold_ratio": 1.0,
            },
        },
        "stage1": {
            "pupil": {"left_mean_mm": NA, "right_mean_mm": NA,
                      "left_variance": NA, "right_variance": NA,
                      "combined_mean_mm": pupil_px.get("mean", NA),
                      "status": ("deviation" if round1.get("abnormal")
                                 else "normal")},
            "blink": {"rate_per_minute": NA, "mean_duration_ms": NA,
                      "reference_rate_min": NA, "reference_rate_max": NA,
                      "status": NA},
            "plr": {"stimulus_used": False, "amplitude_percent": NA,
                    "reference_min_percent": NA, "status": NA},
            "hrv": {"heart_rate_bpm": vitals.get("bpm", NA),
                    "breathing_rate_min": vitals.get("breathing", NA),
                    "hrv_rmssd_ms": NA, "source": "Presage (laptop)",
                    "status": NA},
            "eye_path_images": stage1.get("images_b64") or [],
        },
        "stage3": {
            "balance": {"sway_magnitude_px": balance.get("mean_deviation", NA),
                        "sway_frequency_hz": NA, "duration_seconds": 10,
                        "threshold_magnitude_px": BALANCE_SWAY_THRESHOLD,
                        "status": ("deviation" if any(
                            "sway" in d for d in deviations) else "normal")},
            "smooth_pursuit": {"horizontal_correlation": NA,
                               "vertical_correlation": NA,
                               "threshold_min": NA, "status": NA},
            "vor": {"static_gaze_variance": NA, "motion_gaze_variance": NA,
                    "variance_ratio": NA,
                    "threshold_ratio": VOR_SEVERE_THRESHOLD,
                    "status": "deviation" if vor_severe else "normal"},
            "recall": {"words_presented": recall.get("words_presented", []),
                       "words_recalled": recall.get("marked_heard", []),
                       "score": recall.get("score", NA),
                       "max_score": recall.get("max_score", 5),
                       "scoring_method": "self_marked"},
        },
        "fusion": {"stage1_weight": 0.55, "stage3_weight": 0.45,
                   "decision_basis": basis},
        "narration": _fallback_narration(flag, deviations),
        "metadata": {"devices": DEVICES, "report_version": "1.0.0"},
    }


def _fallback_narration(flag, deviations):
    """Local narration used until (or instead of) Gemini's."""
    return {
        "summary": (("Deviations noted: " + ", ".join(deviations) + ". "
                     if deviations else
                     "No metrics deviated from expected ranges. ")
                    + "This is a screening result, not a diagnosis."),
        "recommendations": [
            "Refer to a licensed medical professional for evaluation."
            if flag == "refer" else
            "No referral triggered by this screening.",
            "Monitor for worsening symptoms.",
        ],
    }


def write_abnormal(reason, eye, vitals=None):
    """Round-1 pupil failure: immediate refer report, no Gemini, remaining
    tests never run."""
    eye = eye or {}
    vitals = vitals or {}
    round1 = eye.get("round1") or {}
    med = round1.get("median_px", NA)
    report = {
        "session_id": "RG-" + time.strftime("%Y%m%d-%H%M%S"),
        "assessed_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "outcome": {
            "flag": "refer",
            "headline": "Abnormal pupil response — refer for immediate "
                        "medical evaluation",
            "scat5_score": NA, "scat5_max": 132,
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
            "hrv": {"heart_rate_bpm": vitals.get("bpm", NA),
                    "breathing_rate_min": vitals.get("breathing", NA),
                    "hrv_rmssd_ms": NA,
                    "source": "Presage (laptop)", "status": NA},
            "eye_path_images": eye.get("images_b64") or [],
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
        "metadata": {"devices": DEVICES, "report_version": "1.0.0"},
    }
    with _lock:
        data = _read_locked()
        data["finalized"] = True
        data.update(report)
        _write_locked(data)
    print("abnormal report written")
