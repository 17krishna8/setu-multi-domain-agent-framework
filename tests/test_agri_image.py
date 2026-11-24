from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
import agri
from agri_image import (MODEL_ID, MODEL_REVISION, MAX_IMAGE_BYTES,
                        load_plant_validator, validate_image)


def test_validation_is_closed_when_no_model_is_supplied():
    result = validate_image("leaf.jpg")
    assert result["status"] == "unavailable"
    assert result["plant_image"] is False


def test_validator_rejects_false_and_malformed_results():
    assert validate_image("x", validator=lambda _: {"plant_image": False})["status"] == "rejected"
    assert validate_image("x", validator=lambda _: "yes")["status"] == "rejected"
    assert validate_image("x", validator=lambda _: 1 / 0)["status"] == "error"


def test_upload_validation_decodes_and_enforces_image_size(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from agri_image import _decode_image

    image = tmp_path / "leaf.jpg"
    Image.new("RGB", (32, 24), "green").save(image)
    assert _decode_image(str(image)).size == (32, 24)

    oversized = tmp_path / "oversized.jpg"
    oversized.write_bytes(b"0" * (MAX_IMAGE_BYTES + 1))
    with pytest.raises(ValueError, match="image size"):
        _decode_image(str(oversized))


def test_non_image_file_with_image_suffix_is_rejected(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from agri_image import _decode_image

    fake = tmp_path / "leaf.jpg"
    fake.write_text("this is not a jpeg", encoding="ascii")
    with pytest.raises(ValueError, match="cannot be decoded"):
        _decode_image(str(fake))


def test_router_plant_and_nonplant_decisions(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from agri_image import _decode_image

    image_path = tmp_path / "input.png"
    Image.new("RGB", (16, 16), "green").save(image_path)
    class FakeModel:
        def __init__(self, scores): self.scores = scores
        def eval(self): return self
        def to(self, device): return self
        def __call__(self, tensor): return self.scores

    import numpy as np
    classes = ["crop", "human", "insect", "mushroom", "plant", "unhealthy_plant"]
    plant = load_plant_validator(
        model_loader=lambda: FakeModel(np.array([[.05, .01, .01, .01, .88, .04]])),
        classes_loader=lambda: classes)
    not_plant = load_plant_validator(
        model_loader=lambda: FakeModel(np.array([[.01, .90, .02, .02, .03, .02]])),
        classes_loader=lambda: classes)
    assert plant(str(image_path))["plant_image"] is True
    assert not_plant(str(image_path))["plant_image"] is False
    assert MODEL_ID == "kindwise/router.tiny"
    assert len(MODEL_REVISION) == 40


def test_agri_classifier_never_runs_after_image_gate_rejects():
    called = []
    result = agri.classify("person.jpg", "tomato",
        classifier=lambda *_: called.append(True) or {"label": "early blight", "confidence": 1},
        image_validator=lambda _: {"plant_image": False, "reason": "person image"})

    assert result["status"] == "invalid-image"
    assert result["treatment_allowed"] is False
    assert called == []
    assert agri.treat(result)["remedies"] == []
