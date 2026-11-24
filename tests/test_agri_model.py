from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from agri_model import MODEL_ID, MODEL_REVISION, load_classifier


def test_model_is_pinned_and_factory_is_loaded_once():
    calls = []
    outputs = [[{"label": "Tomato___Early_blight", "score": 0.91},
                {"label": "Tomato___Late_blight", "score": 0.06},
                {"label": "Tomato___healthy", "score": 0.03}]]

    def factory(task, **kwargs):
        calls.append((task, kwargs))
        return lambda path, **infer_kwargs: outputs[0]

    classify = load_classifier(pipeline_factory=factory)
    result = classify("leaf.jpg", "tomato")

    assert calls == [("image-classification", {"model": MODEL_ID,
                                               "revision": MODEL_REVISION,
                                               "device": -1})]
    assert result == {"label": "Early blight", "crop": "tomato",
                      "model_label": "Tomato___Early_blight", "confidence": 0.91,
                      "alternatives": [["Tomato___Late_blight", 0.06],
                                       ["Tomato___healthy", 0.03]]}


def test_adapter_rejects_empty_or_unknown_output_shapes():
    classify = load_classifier(pipeline_factory=lambda *a, **k: lambda *a, **k: [])
    try:
        classify("leaf.jpg")
    except ValueError as exc:
        assert "no labels" in str(exc)
    else:
        raise AssertionError("expected invalid inference output to be rejected")


def test_agri_classifier_callable_flows_through_crop_and_confidence_gates():
    def factory(*args, **kwargs):
        def infer(*infer_args, **infer_kwargs):
            return [{"label": "Potato___Early_blight", "score": 0.88},
                    {"label": "Potato___healthy", "score": 0.08}]
        return infer

    classify = load_classifier(pipeline_factory=factory)
    import agri

    compatible = agri.classify("potato.jpg", "potato", classifier=classify)
    incompatible = agri.classify("tomato.jpg", "tomato", classifier=classify)
    missing_user_crop = agri.classify("potato.jpg", None, classifier=classify)
    assert compatible["treatment_allowed"] is True
    assert missing_user_crop["crop"] == "potato"
    assert missing_user_crop["predicted_crop"] == "potato"
    assert missing_user_crop["treatment_allowed"] is True
    assert agri.treat(compatible)["status"] == "ok"
    assert incompatible["treatment_allowed"] is False
    assert incompatible["crop_mismatch"] is True
    assert incompatible["crop"] == "potato"
    assert agri.treat(incompatible)["status"] == "crop-mismatch"
    assert agri.treat(incompatible)["remedies"] == []
