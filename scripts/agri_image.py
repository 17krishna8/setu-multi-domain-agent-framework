#!/usr/bin/env python3
"""Opt-in image decoding and plant-content validation for Agri.

The validator uses Kindwise's pinned Apache-2.0 router.tiny model. It is a
plant-content gate, not a disease diagnosis; only a disease classifier may
produce the classification that proceeds to Setu's existing treatment gates.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

MODEL_ID = "kindwise/router.tiny"
MODEL_REVISION = "2fd1892a3aa0f52e61036c0b0bd3ae83e47d3f05"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_SIDE = 12_000
PLANT_LABELS = {"plant", "unhealthy_plant", "crop"}
PLANT_SCORE_THRESHOLD = 0.55


def _decode_image(image_path: str):
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise RuntimeError("image validation requires Pillow; install requirements-agri-image.txt") from exc

    path = Path(image_path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"image is unavailable: {exc}") from exc
    if size <= 0 or size > MAX_IMAGE_BYTES:
        raise ValueError(f"image size must be between 1 byte and {MAX_IMAGE_BYTES} bytes")
    try:
        with Image.open(path) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("unsupported image format; use JPEG, PNG, or WebP")
            if image.width <= 0 or image.height <= 0 or max(image.size) > MAX_IMAGE_SIDE:
                raise ValueError("image dimensions are invalid or exceed the 12,000 px side limit")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("image exceeds the 40 megapixel safety limit")
            image.verify()
        with Image.open(path) as image:
            image.load()
            return image.convert("RGB")
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError(f"image cannot be decoded safely: {exc}") from exc


def load_plant_validator(*, model_id: str = MODEL_ID, revision: str = MODEL_REVISION,
                         model_loader: Callable | None = None,
                         classes_loader: Callable | None = None) -> Callable:
    """Load the pinned CPU TorchScript router and return an image validator.

    No weights are downloaded until this function is explicitly called. The
    model's multi-label outputs are used only to accept/reject plant content.
    """
    try:
        from huggingface_hub import hf_hub_download
        import numpy as np
        import torch
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError(
            "pretrained plant-image validation requires optional dependencies; "
            "install requirements-agri-image.txt") from exc

    model_path = None
    if model_loader is None:
        model_path = hf_hub_download(model_id, "model.traced.pt", revision=revision)
        model_loader = torch.jit.load
    if classes_loader is None:
        class_path = hf_hub_download(model_id, "classes.txt", revision=revision)
        classes_loader = lambda: Path(class_path).read_text(encoding="utf-8").splitlines()

    model = model_loader(model_path) if model_path is not None else model_loader()
    model = model.eval().to("cpu")
    classes = [str(label).strip() for label in classes_loader()]
    required_labels = {"plant", "unhealthy_plant", "crop", "mushroom", "insect", "human"}
    if not required_labels.issubset(classes):
        raise RuntimeError("plant router class labels are incomplete or unsupported")

    def validate(image_path: str) -> dict:
        try:
            image = _decode_image(image_path)
        except ValueError as exc:
            return {"status": "rejected", "reason": str(exc), "plant_image": False}
        width, height = image.size
        side = min(width, height)
        left, top = (width - side) // 2, (height - side) // 2
        image = image.crop((left, top, left + side, top + side)).resize(
            (480, 480), Image.Resampling.BOX)
        tensor = torch.from_numpy(np.asarray(image, dtype=np.float32) / 255.0)
        tensor = tensor.permute(2, 0, 1).unsqueeze(0)
        try:
            with torch.no_grad():
                raw = model(tensor)
            if hasattr(raw, "logits"):
                raw = raw.logits
            if isinstance(raw, (tuple, list)):
                raw = raw[0]
            if hasattr(raw, "detach"):
                raw = raw.detach().cpu().numpy()
            scores = np.asarray(raw).squeeze().tolist()
            if not isinstance(scores, list) or len(scores) != len(classes):
                raise ValueError("plant router returned an unexpected score shape")
            scores = [float(score) for score in scores]
            if any(not 0.0 <= score <= 1.0 for score in scores):
                raise ValueError("plant router scores are outside the expected [0, 1] range")
        except Exception as exc:  # noqa: BLE001 — model failure must fail closed
            return {"status": "error", "reason": f"plant image model failed: {exc}",
                    "plant_image": False}
        ranked = sorted(zip(classes, scores), key=lambda item: item[1], reverse=True)
        target_score = max((score for label, score in ranked if label in PLANT_LABELS),
                           default=0.0)
        non_target_score = max((score for label, score in ranked if label not in PLANT_LABELS),
                               default=0.0)
        plant_image = (target_score >= PLANT_SCORE_THRESHOLD and
                       target_score >= non_target_score)
        return {
            "status": "validated" if plant_image else "rejected",
            "plant_image": plant_image,
            "top_label": ranked[0][0],
            "top_score": round(ranked[0][1], 4),
            "plant_score": round(target_score, 4),
            "threshold": PLANT_SCORE_THRESHOLD,
            "model": model_id,
            "revision": revision,
            "note": "Plant-content gate only; not a disease diagnosis or treatment recommendation.",
        }

    return validate


def validate_image(image_path: str, *, validator: Callable | None = None) -> dict:
    """Validate content with a supplied model; never silently accept by extension."""
    if validator is None:
        return {"status": "unavailable", "plant_image": False,
                "reason": "No plant-content model supplied; use load_plant_validator() explicitly."}
    try:
        result = validator(image_path)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "plant_image": False, "reason": str(exc)}
    if not isinstance(result, dict) or result.get("plant_image") is not True:
        return {"status": "rejected", "plant_image": False,
                "reason": result.get("reason", "image was not validated as plant content")
                if isinstance(result, dict) else "validator returned an invalid result"}
    return result
