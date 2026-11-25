#!/usr/bin/env python3
"""Optional Hugging Face MobileNetV2 classifier for PlantVillage-style leaves.

This adapter downloads model weights only when ``load_classifier`` is explicitly
called. PlantVillage is a controlled-background dataset; field performance and
confidence calibration are not established by this project. Keep the agri
confidence/crop/knowledge-base gates and confirm diagnoses locally.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

MODEL_ID = "Kathir56/plant-disease-tamilnadu"
MODEL_REVISION = "0de5ea2d44fd24d6e3d12332f06557ecd9551001"


def _normalise_output(output: Any) -> dict:
    """Convert a Transformers image-classification output into Setu's contract."""
    if isinstance(output, dict):
        rows = [output]
    elif isinstance(output, list):
        rows = output
    else:
        raise ValueError("image classifier returned an unsupported output shape")
    valid = [row for row in rows if isinstance(row, dict) and row.get("label") is not None]
    if not valid:
        raise ValueError("image classifier returned no labels")
    valid.sort(key=lambda row: float(row.get("score", 0.0)), reverse=True)
    top = valid[0]
    return {
        "label": str(top["label"]),
        "confidence": float(top.get("score", 0.0)),
        "alternatives": [[str(row["label"]), float(row.get("score", 0.0))]
                         for row in valid[1:4]],
    }


def load_classifier(*, model_id: str = MODEL_ID, revision: str = MODEL_REVISION,
                    pipeline_factory: Callable | None = None) -> Callable:
    """Load and return ``(image_path, crop) -> Setu classification mapping``.

    Requires the optional dependencies in ``requirements-agri-model.txt``. The
    repository revision is pinned by default so a remote model update cannot
    silently change results. Tests may provide a lightweight factory.
    """
    if pipeline_factory is None:
        try:
            from transformers import pipeline
        except ImportError as exc:
            raise RuntimeError(
                "pretrained Agri classification requires optional dependencies; "
                "install requirements-agri-model.txt") from exc
        pipeline_factory = pipeline

    infer = pipeline_factory("image-classification", model=model_id,
                             revision=revision, device=-1)

    def classify_image(image_path: str, crop: str | None = None) -> dict:
        # The crop is intentionally not used to alter a model prediction. Include
        # the model's crop label so downstream crop-compatibility checks can
        # compare the actual predicted crop against the user's supplied crop.
        del crop
        result = _normalise_output(infer(image_path, top_k=3))
        model_label = result["label"]
        result["label"] = model_label.rsplit("___", 1)[-1].replace("_", " ")
        if "___" in model_label:
            result["crop"] = model_label.split("___", 1)[0].replace("_", " ").lower()
        result["model_label"] = model_label
        return result

    return classify_image
