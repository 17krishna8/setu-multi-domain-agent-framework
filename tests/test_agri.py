#!/usr/bin/env python3
"""Tests for the Agriculture pack — the confidence gate above all."""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agri  # noqa: E402

THRESHOLD = 0.6


def _classifier(label: str, confidence: float):
    return lambda path, crop: {"label": label, "confidence": confidence}


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def test_low_confidence_returns_no_remedies():
    res = agri.treat(agri.classify("x.jpg", "tomato",
                                   classifier=_classifier("Early blight", 0.42)))
    assert res["status"] == "uncertain"
    assert res["remedies"] == []
    assert "consult an expert" in res["headline"].lower()


def test_threshold_boundary_is_inclusive_at_0_6():
    below = agri.classify("x.jpg", "tomato", classifier=_classifier("powdery mildew", 0.599))
    at = agri.classify("x.jpg", "tomato", classifier=_classifier("powdery mildew", 0.6))
    assert below["treatment_allowed"] is False
    assert at["treatment_allowed"] is True
    assert agri.treat(below)["remedies"] == []
    assert len(agri.treat(at)["remedies"]) > 0


def test_high_confidence_produces_ordered_remedies():
    res = agri.treat(agri.classify("x.jpg", "potato",
                                  classifier=_classifier("Late blight", 0.88)))
    assert res["status"] == "ok"
    assert len(res["remedies"]) >= 3
    assert res["urgency"] == "immediate"
    assert "confirm_before_spraying" in res


def test_uncertain_path_still_gives_next_steps():
    res = agri.treat(agri.classify("x.jpg", "tomato",
                                  classifier=_classifier("blight", 0.3)))
    assert res["status"] == "uncertain"
    assert len(res["next_steps"]) >= 2
    assert any("spray" in s.lower() for s in res["next_steps"])


# --------------------------------------------------------------------------- #
# classification honesty
# --------------------------------------------------------------------------- #
def test_no_classifier_never_invents_a_disease():
    res = agri.classify(str(Path(__file__).resolve().parent / "test_agri.py"), "tomato")
    assert res["label"] == "unidentified"
    assert res["confidence"] == 0.0
    assert res["treatment_allowed"] is False


def test_classifier_exception_is_an_error_not_a_diagnosis():
    def boom(path, crop):
        raise RuntimeError("weights missing")
    res = agri.classify("x.jpg", "tomato", classifier=boom)
    assert res["status"] == "error"
    assert res["treatment_allowed"] is False


def test_confident_label_outside_kb_defers_instead_of_improvising():
    res = agri.treat(agri.classify("x.jpg", "mango",
                                  classifier=_classifier("some unknown blight xyz", 0.95)))
    assert res["status"] == "no-guidance"
    assert res["remedies"] == []


def test_abiotic_entries_say_no_pesticide_will_help():
    res = agri.treat(agri.classify("x.jpg", "tomato",
                                  classifier=_classifier("nitrogen deficiency", 0.9)))
    assert res["status"] == "ok"
    assert any("not a disease" in r.lower() for r in res["remedies"])


def test_confidence_is_clamped_to_a_probability():
    res = agri.classify("x.jpg", "tomato", classifier=_classifier("Early blight", 7.5))
    assert res["confidence"] == 1.0


# --------------------------------------------------------------------------- #
# localisation
# --------------------------------------------------------------------------- #
def test_translator_is_used_when_provided():
    out = agri.localize("Do the thing", "hi", translator=lambda t, l: f"[{l}]{t}")
    assert out["translated"] is True
    assert out["text"].startswith("[hi]")


def test_offline_never_claims_a_translation():
    old = os.environ.get("SETU_OFFLINE")
    os.environ["SETU_OFFLINE"] = "1"
    try:
        out = agri.localize("Do the thing", "te")
        assert out["translated"] is False
        assert out["status"] == "untranslated"
        assert out["text"] == "Do the thing"
    finally:
        if old is None:
            os.environ.pop("SETU_OFFLINE", None)
        else:
            os.environ["SETU_OFFLINE"] = old


def test_unsupported_language_is_rejected_not_faked():
    out = agri.localize("text", "xx")
    assert out["status"] == "unsupported-language"
    assert out["translated"] is False


def test_all_thirteen_codes_present():
    assert len(agri.LANGUAGES) == 13
    for code in ("hi", "te", "ta", "kn", "ml", "mr", "bn", "gu", "pa", "or", "as", "ur", "en"):
        assert code in agri.LANGUAGES
