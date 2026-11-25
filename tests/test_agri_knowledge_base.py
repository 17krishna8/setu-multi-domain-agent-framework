from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agri


def test_added_disease_labels_resolve_to_cautious_local_guidance():
    cases = [("downy mildew", "cucumber"), ("bacterial wilt", "tomato"),
             ("mosaic virus", "tomato"), ("whiteflies", "cotton"),
             ("spider mites", "beans")]
    for label, crop in cases:
        classification = agri.classify("unused.jpg", crop,
                                       classifier=lambda *_: {"label": label,
                                                               "confidence": 0.95})
        assert classification["kb_key"] in agri.KNOWLEDGE_BASE
        assert classification["treatment_allowed"] is True
        treatment = agri.treat(classification)
        assert treatment["status"] == "ok"
        assert treatment["remedies"]
        assert any("extension" in item.lower() or "confirm" in item.lower()
                   for item in treatment["remedies"])
