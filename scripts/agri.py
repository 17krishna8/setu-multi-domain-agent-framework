#!/usr/bin/env python3
"""Setu Agriculture — classify, treat, localize.

The chain has one behaviour that matters more than the model:

    if classification.confidence < 0.6  ->  treat() returns "uncertain, consult an
                                            expert" and NO remedy.

That threshold is enforced in ``treat()`` itself, not in a prompt, because a
confident-sounding pesticide recommendation attached to a wrong diagnosis is how
a farmer loses a season — or sprays something that does not belong on that crop.
An agent that "helpfully" writes a remedy anyway cannot, because the function
returns the refusal.

Model boundaries
----------------
* ``classify`` accepts a **pretrained classifier callable**. Without one it either
  uses a deterministic colour/texture heuristic (clearly labelled as such, with low
  confidence by construction) or returns ``unavailable``. It never invents a
  disease name from a filename.
* ``localize`` accepts a **translator callable**. The default backend routes
  through ``model_wrapper.call_model``, so it works with any configured model;
  offline it returns a clearly-marked untranslated result rather than pretending.

CLI
---
    python3 agri.py classify --image samples/leaf.jpg --crop tomato
    python3 agri.py treat --classification classification.json
    python3 agri.py localize --text "..." --lang hi
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

CONFIDENCE_THRESHOLD = 0.6

LANGUAGES = {
    "hi": "Hindi", "te": "Telugu", "ta": "Tamil", "kn": "Kannada", "ml": "Malayalam",
    "mr": "Marathi", "bn": "Bengali", "gu": "Gujarati", "pa": "Punjabi",
    "or": "Odia", "as": "Assamese", "ur": "Urdu", "en": "English",
}

# A small, deliberately conservative knowledge base. Every entry names the crop,
# the cause type, and remedies ordered from least to most chemical. Nothing here
# is a substitute for a local extension officer, and the text says so.
KNOWLEDGE_BASE = {
    "early-blight": {
        "crop": "tomato/potato",
        "cause": "fungal (Alternaria solani)",
        "signs": "concentric brown rings on lower leaves, yellow halo, spreading upward",
        "remedies": [
            "Remove and destroy affected lower leaves; do not compost them.",
            "Increase spacing and stake plants so foliage dries faster; water at the base, "
            "not the leaves, and water early in the day.",
            "Rotate away from tomato/potato for at least two seasons.",
            "If pressure is high, a copper-based or chlorothalonil fungicide per label rate — "
            "confirm the product is registered for this crop in your state.",
        ],
        "urgency": "act within a few days; spreads fast in humid weather",
    },
    "late-blight": {
        "crop": "tomato/potato",
        "cause": "oomycete (Phytophthora infestans)",
        "signs": "large irregular water-soaked grey-green patches, white mould on the underside "
                 "in damp weather, rapid collapse",
        "remedies": [
            "Act immediately — this can destroy a field within a week in cool wet weather.",
            "Remove and bury or burn infected plants; do not leave debris in the field.",
            "Avoid overhead irrigation entirely; improve airflow.",
            "Contact your local agriculture extension officer the same day: this is a "
            "notifiable, fast-moving disease and control requires a current local protocol.",
        ],
        "urgency": "immediate",
    },
    "powdery-mildew": {
        "crop": "wide host range (cucurbits, tomato, chilli, grapes)",
        "cause": "fungal (many species)",
        "signs": "white powdery coating on upper leaf surfaces, curling leaves",
        "remedies": [
            "Improve airflow: prune, stake, widen spacing.",
            "Remove the worst-affected leaves.",
            "A dilute potassium bicarbonate or wettable sulphur spray is a low-risk first step; "
            "test on a few plants before treating the field.",
            "Avoid excess nitrogen, which produces soft growth that mildews readily.",
        ],
        "urgency": "manageable; act within a week",
    },
    "aphids": {
        "crop": "wide host range",
        "cause": "insect (Aphidoidea)",
        "signs": "clusters of small soft-bodied insects on new growth, sticky honeydew, "
                 "curled shoot tips, ants present",
        "remedies": [
            "A strong water jet removes most colonies on sturdy plants.",
            "Encourage or release ladybirds and lacewings; avoid broad-spectrum sprays that "
            "kill them first.",
            "Yellow sticky traps to monitor and reduce numbers.",
            "Neem oil (per label) for persistent infestations; treat in the evening to spare "
            "pollinators.",
        ],
        "urgency": "monitor; act if new growth is distorted",
    },
    "leaf-spot": {
        "crop": "wide host range",
        "cause": "fungal/bacterial",
        "signs": "discrete dark spots with or without halos, often angular and vein-limited",
        "remedies": [
            "Remove affected leaves; keep foliage dry.",
            "Mulch to stop soil splash onto lower leaves.",
            "Rotate crops; avoid working in the field when leaves are wet.",
            "A copper spray can slow bacterial spread; confirm registration for your crop.",
        ],
        "urgency": "manageable",
    },
    "downy-mildew": {
        "crop": "wide host range (verify host-specific diagnosis)",
        "cause": "oomycete (host-specific species)",
        "signs": "angular yellow patches bounded by veins; grey-purple growth beneath leaves in humid conditions",
        "remedies": [
            "Photograph both leaf surfaces and confirm the crop; nutrient problems and other leaf spots can look similar.",
            "Remove heavily affected leaves when plants are dry and dispose of them away from the field.",
            "Reduce leaf wetness, improve airflow, and avoid moving through wet foliage.",
            "Ask a local extension officer to confirm the host-specific disease and any locally registered control before spraying.",
        ],
        "urgency": "seek confirmation promptly if lesions spread after wet weather",
    },
    "bacterial-wilt": {
        "crop": "wide host range (verify host-specific diagnosis)",
        "cause": "bacterial; several unrelated pathogens can cause wilt",
        "signs": "rapid wilt despite moist soil; vascular browning or bacterial streaming may occur but is not conclusive",
        "remedies": [
            "Do not assume drought: check soil moisture and compare affected and healthy plants.",
            "Avoid moving soil, tools, or irrigation water from affected plants to healthy beds.",
            "Remove severely affected plants with roots only after documenting symptoms; do not compost them.",
            "Request a local diagnostic confirmation; there is no reliable curative spray recommendation from symptoms alone.",
        ],
        "urgency": "prompt confirmation; soil-borne wilt can spread between plants",
    },
    "mosaic-virus": {
        "crop": "wide host range (verify host-specific diagnosis)",
        "cause": "viral; symptoms may also be caused by herbicide injury or nutrient stress",
        "signs": "mottled light/dark green pattern, leaf distortion, or stunting; visual symptoms alone are not diagnostic",
        "remedies": [
            "Do not use seed or cuttings from symptomatic plants until the cause is confirmed.",
            "Clean tools between plants and avoid handling healthy plants after affected plants.",
            "Check for aphids, whiteflies, or other vectors, but do not infer virus from their presence alone.",
            "Seek local confirmation; fungicides do not cure viral disease and vector control must be crop-specific.",
        ],
        "urgency": "confirm before selecting control measures",
    },
    "whiteflies": {
        "crop": "wide host range",
        "cause": "insect (Aleyrodidae); some species transmit plant viruses",
        "signs": "tiny white adults rise from leaf undersides when disturbed; nymphs and honeydew beneath leaves",
        "remedies": [
            "Inspect leaf undersides and use yellow cards to monitor adults rather than diagnose from leaf yellowing alone.",
            "Remove heavily infested leaves where practical and control weeds that host the pest.",
            "Conserve natural enemies; avoid broad-spectrum sprays that can trigger pest resurgence.",
            "For persistent crop damage, consult local extension guidance and use only a product registered for the crop and pest.",
        ],
        "urgency": "monitor; act if populations or plant damage increase",
    },
    "spider-mites": {
        "crop": "wide host range",
        "cause": "mite (Tetranychidae), favoured by hot, dry conditions",
        "signs": "fine pale stippling, bronzing, and possible fine webbing, especially on leaf undersides",
        "remedies": [
            "Confirm mites with a hand lens on the underside of leaves; drought stress can look similar.",
            "Reduce dust and avoid water stress; a gentle water wash can reduce mites on sturdy plants.",
            "Preserve predatory mites and other natural enemies; avoid unnecessary broad-spectrum insecticides.",
            "If damage continues, ask local extension staff about a crop-registered miticide and label precautions.",
        ],
        "urgency": "monitor closely in hot, dry weather",
    },
    "nutrient-deficiency-nitrogen": {
        "crop": "wide host range",
        "cause": "abiotic (not a pathogen)",
        "signs": "uniform pale-yellowing of older lower leaves first, stunted growth",
        "remedies": [
            "This is not a disease and no fungicide or insecticide will help.",
            "Check soil pH and organic matter before adding fertiliser.",
            "Apply nitrogen in split doses rather than all at once.",
            "Confirm with a soil test — over-application wastes money and pollutes water.",
        ],
        "urgency": "no emergency",
    },
    "water-stress": {
        "crop": "wide host range",
        "cause": "abiotic",
        "signs": "wilting that reverses overnight, leaf margin browning, or waterlogged "
                 "yellowing with root smell",
        "remedies": [
            "Check soil moisture at 10cm depth by hand before any treatment.",
            "Wilting that recovers overnight is water stress, not disease.",
            "If waterlogged, improve drainage before anything else.",
            "Not a pest or pathogen: pesticide application would be wasted and harmful.",
        ],
        "urgency": "no emergency",
    },
}

# Deterministic fallback heuristic. It is honest about being weak: it examines the
# image's colour composition and cannot identify a pathogen. Used only when no
# pretrained model is wired in, and it caps its own confidence below the treatment
# threshold on purpose.
def _heuristic_classify(image_path: str, crop: str | None) -> dict:
    p = Path(image_path)
    if not p.exists():
        return {"status": "unavailable", "reason": f"image not found: {image_path}"}
    try:
        size = p.stat().st_size
        head = p.read_bytes()[:64]
    except OSError as e:
        return {"status": "unavailable", "reason": str(e)}
    if not (head.startswith(b"\xff\xd8") or head.startswith(b"\x89PNG")):
        return {"status": "unavailable",
                "reason": "not a JPEG/PNG; cannot analyse"}
    return {
        "status": "heuristic-only",
        "crop": crop or "unknown",
        "label": "unidentified",
        "confidence": 0.0,
        "reason": ("No pretrained classifier is wired in. Setu will not name a disease from "
                   "a filename or a file size — a confident wrong diagnosis is worse than "
                   "no diagnosis. Wire a model with --classifier or consult an expert."),
        "image_bytes": size,
    }


def classify(image_path: str, crop: str | None = None,
             classifier=None, image_validator=None) -> dict:
    """Classify a leaf/fruit image.

    ``classifier`` is any callable ``(image_path, crop) -> dict`` returning::

        {"label": str, "confidence": float, "alternatives": [[label, conf], ...]}

    so a real pretrained model (a torchvision fine-tune, a TFLite plant-disease
    model, a hosted vision API) plugs in without touching this file.
    """
    if image_validator is not None:
        try:
            validation = image_validator(image_path)
        except Exception as e:  # noqa: BLE001 — validator failure must fail closed
            return {"check": "agri-classify", "status": "image-validation-error",
                    "reason": str(e), "confidence": 0.0, "treatment_allowed": False}
        if not isinstance(validation, dict) or validation.get("plant_image") is not True:
            reason = (validation.get("reason", "image was not validated as plant content")
                      if isinstance(validation, dict) else "validator returned an invalid result")
            return {"check": "agri-classify", "status": "invalid-image",
                    "reason": reason, "confidence": 0.0, "treatment_allowed": False,
                    "image_validation": validation}

    if classifier is not None:
        try:
            raw = classifier(image_path, crop)
        except Exception as e:  # noqa: BLE001 — a model failure must not become a diagnosis
            return {"check": "agri-classify", "status": "error",
                    "reason": f"classifier raised: {e}", "confidence": 0.0,
                    "treatment_allowed": False}
        label = str(raw.get("label", "")).strip()
        try:
            conf = float(raw.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(conf, 1.0))
        model_crop = raw.get("crop")
        effective_crop = model_crop or crop
        kb_key = _kb_lookup(label, effective_crop)
        crop_conflict = bool(crop and model_crop and
                             re.sub(r"[^a-z0-9]+", "-", crop.lower()).strip("-") !=
                             re.sub(r"[^a-z0-9]+", "-", str(model_crop).lower()).strip("-"))
        crop_ok = bool(kb_key and not crop_conflict and
                       _crop_compatible(kb_key, effective_crop))
        return {
            "check": "agri-classify",
            "status": "classified",
            "source": "pretrained-classifier",
            "crop": effective_crop or "unknown",
            "predicted_crop": model_crop,
            "crop_mismatch": crop_conflict,
            "label": label,
            "kb_key": kb_key,
            "confidence": round(conf, 3),
            "alternatives": raw.get("alternatives", [])[:3],
            "treatment_allowed": (conf >= CONFIDENCE_THRESHOLD and crop_ok),
            "threshold": CONFIDENCE_THRESHOLD,
            "gate_note": (f"below {CONFIDENCE_THRESHOLD}: no remedy will be offered"
                          if conf < CONFIDENCE_THRESHOLD else
                          "crop missing or incompatible: no remedy will be offered"
                          if not crop_ok else
                          "above threshold and crop compatible: treatment advice permitted"),
        }
    base = _heuristic_classify(image_path, crop)
    # Every classification result must carry the same keys, whatever path it took,
    # so downstream code never has to guess whether "confidence" exists.
    base.update({"check": "agri-classify",
                 "source": "heuristic-no-model",
                 "confidence": float(base.get("confidence", 0.0) or 0.0),
                 "label": base.get("label", "unidentified"),
                 "treatment_allowed": False,
                 "threshold": CONFIDENCE_THRESHOLD,
                 "gate_note": (f"below {CONFIDENCE_THRESHOLD}: no remedy will be offered")})
    return base


def _kb_lookup(label: str, crop: str | None = None) -> str | None:
    """Map a classifier label onto a knowledge-base key, tolerantly."""
    if not label:
        return None
    norm = label.lower().replace("_", "-").replace(" ", "-")
    if norm in KNOWLEDGE_BASE:
        return norm
    # A partial label ("blight") can fit several entries. Picking the first one would
    # issue one disease's treatment for an ambiguous label, so only a unique fit counts.
    candidates = [key for key in KNOWLEDGE_BASE if key in norm or norm in key]
    if len(candidates) == 1:
        return candidates[0]
    aliases = {
        "alternaria": "early-blight", "phytophthora": "late-blight",
        "mildew": "powdery-mildew", "aphid": "aphids", "septoria": "leaf-spot",
        "bacterial-spot": "leaf-spot", "downy-mildew": "downy-mildew",
        "bacterial-wilt": "bacterial-wilt", "mosaic-virus": "mosaic-virus",
        "whitefly": "whiteflies", "spider-mite": "spider-mites",
        "nitrogen-deficiency": "nutrient-deficiency-nitrogen",
        "healthy": None,
    }
    for alias, key in aliases.items():
        if alias in norm:
            return key
    return None


def _crop_compatible(kb_key: str, crop: str | None) -> bool:
    """Check explicit crop restrictions before allowing knowledge-base remedies."""
    crop_scope = KNOWLEDGE_BASE.get(kb_key, {}).get("crop", "")
    allowed = {"tomato/potato": {"tomato", "tomatoes", "potato", "potatoes"}}
    if not crop_scope or crop_scope.lower().startswith("wide host range"):
        return True
    restricted_crops = allowed.get(crop_scope)
    if restricted_crops is None:
        return False
    crop_name = re.sub(r"[^a-z0-9]+", "-", str(crop or "").strip().lower()).strip("-")
    return crop_name in restricted_crops


def treat(classification: dict) -> dict:
    """Return a remedy — or refuse.

    The confidence gate is here, in code. Below ``CONFIDENCE_THRESHOLD`` the
    function returns an "uncertain" answer with an empty remedies list, so no
    downstream agent can quote a remedy that was never justified.
    """
    conf = float(classification.get("confidence", 0.0) or 0.0)
    label = classification.get("label") or "unknown"
    crop = classification.get("crop") or "unknown"

    if classification.get("crop_mismatch"):
        return {
            "check": "agri-treat",
            "status": "crop-mismatch",
            "confidence": conf,
            "label": label,
            "crop": crop,
            "predicted_crop": classification.get("predicted_crop"),
            "remedies": [],
            "headline": "The image model's crop prediction conflicts with the supplied crop.",
            "explanation": (f"The supplied crop '{crop}' does not match the image model's "
                            f"predicted crop '{classification.get('predicted_crop')}'. "
                            "No treatment is recommended."),
            "next_steps": ["Confirm the crop and diagnosis with your local agriculture "
                           "extension officer or KVK."],
            "disclaimer": common.DISCLAIMERS["agri"],
        }

    # Two independent reasons not to advise, and they must not be conflated:
    #   * low confidence   -> we do not know WHAT this is      -> "uncertain"
    #   * unknown label    -> we know what it is, but not what to do -> "no-guidance"
    # Collapsing them produced a contradictory message at high confidence
    # ("confidence 0.95, below the 0.60 threshold"), which is exactly the kind of
    # self-contradiction that destroys a farmer's trust in the whole output.
    explicitly_forbidden = classification.get("treatment_allowed") is False \
        and float(classification.get("confidence", 0.0) or 0.0) >= CONFIDENCE_THRESHOLD
    if conf < CONFIDENCE_THRESHOLD and not explicitly_forbidden:
        return {
            "check": "agri-treat",
            "status": "uncertain",
            "confidence": conf,
            "threshold": CONFIDENCE_THRESHOLD,
            "label": label,
            "crop": crop,
            "remedies": [],
            "headline": "Uncertain — consult an expert.",
            "explanation": (
                f"The classification confidence is {conf:.2f}, below the {CONFIDENCE_THRESHOLD:.2f} "
                f"threshold Setu requires before offering treatment advice. Recommending a "
                f"pesticide or fungicide for a misidentified problem can destroy a crop or "
                f"leave harmful residue, so no remedy is given."),
            "next_steps": [
                "Photograph the affected plant again in daylight, close up and whole-plant, "
                "including the underside of leaves.",
                "Take a sample to your local Krishi Vigyan Kendra (KVK) or agriculture "
                "extension officer for a lab or field diagnosis.",
                "Do not spray anything until the cause is confirmed — an unnecessary "
                "application costs money and can worsen the problem.",
            ],
            "disclaimer": common.DISCLAIMERS["agri"],
        }

    label_kb_key = _kb_lookup(label, crop)
    supplied_kb_key = classification.get("kb_key")
    kb_key = (label_kb_key if supplied_kb_key in (None, label_kb_key) else None)
    entry = KNOWLEDGE_BASE.get(kb_key or "")
    if entry and not _crop_compatible(kb_key, crop):
        return {
            "check": "agri-treat",
            "status": "crop-mismatch",
            "confidence": conf,
            "label": label,
            "crop": crop,
            "kb_key": kb_key,
            "remedies": [],
            "headline": "Crop does not match the treatment guidance.",
            "explanation": (f"The local guidance for '{label}' applies to "
                            f"{entry['crop']}, not the supplied crop '{crop}'. "
                            "No treatment is recommended."),
            "next_steps": [
                "Confirm the crop and diagnosis with your local agriculture extension officer or KVK.",
                "Do not apply a crop-specific pesticide based on this result.",
            ],
            "disclaimer": common.DISCLAIMERS["agri"],
        }

    if not entry:
        return {
            "check": "agri-treat",
            "status": "no-guidance",
            "confidence": conf,
            "label": label,
            "remedies": [],
            "headline": "Diagnosis accepted, but Setu has no guidance for this condition.",
            "explanation": (f"'{label}' was identified with confidence {conf:.2f}, but it is not "
                            f"in Setu's local knowledge base. Rather than improvise, it defers."),
            "next_steps": ["Consult your local agriculture extension officer or KVK."],
            "disclaimer": common.DISCLAIMERS["agri"],
        }

    return {
        "check": "agri-treat",
        "status": "ok",
        "confidence": conf,
        "label": label,
        "crop": crop,
        "kb_key": kb_key,
        "cause": entry["cause"],
        "signs_to_confirm": entry["signs"],
        "urgency": entry["urgency"],
        "remedies": entry["remedies"],
        "ordering_note": "Ordered least-to-most chemical. Try the first two before any spray.",
        "confirm_before_spraying": [
            "That the product is registered for this crop in your state.",
            "The label rate — more is not better and can burn the crop.",
            "That you are not spraying flowering plants during pollinator hours (early morning).",
        ],
        "disclaimer": common.DISCLAIMERS["agri"],
    }


def localize(text: str, target_lang: str = "hi", translator=None,
             model: str | None = None) -> dict:
    """Translate the final output to a regional language.

    ``translator`` is any callable ``(text, lang_code) -> str``. Default backend is
    ``model_wrapper.call_model``; offline, it returns the original text clearly
    marked untranslated rather than silently shipping English to a Hindi-speaking
    farmer.
    """
    lang = (target_lang or "hi").lower()
    if lang not in LANGUAGES:
        return {"check": "agri-localize", "status": "unsupported-language",
                "requested": lang, "supported": LANGUAGES,
                "translated": False, "text": text}
    if lang == "en":
        return {"check": "agri-localize", "status": "no-op", "language": "English",
                "translated": True, "text": text}

    if translator is not None:
        try:
            out = translator(text, lang)
            return {"check": "agri-localize", "status": "translated",
                    "language": LANGUAGES[lang], "code": lang,
                    "source": "provided-translator", "translated": True, "text": out}
        except Exception as e:  # noqa: BLE001
            return {"check": "agri-localize", "status": "translator-error",
                    "reason": str(e), "translated": False, "text": text}

    from model_wrapper import call_model
    import os
    if os.environ.get("SETU_OFFLINE") in {"1", "true", "TRUE", "yes"}:
        return {"check": "agri-localize", "status": "untranslated",
                "language": LANGUAGES[lang], "code": lang, "translated": False,
                "text": text,
                "reason": "SETU_OFFLINE is set; returning the source text rather than "
                          "pretending to translate."}
    prompt = (
        f"Translate the following agricultural advisory into {LANGUAGES[lang]} ({lang}). "
        "Keep pesticide and product names in Latin script. Keep all numbers exactly as "
        "given. Do not add advice that is not in the source. Output only the translation.\n\n"
        f"{text}")
    try:
        out = call_model(prompt, model=model,
                         system="You translate. You do not add, remove or reinterpret advice.")
    except Exception as e:  # noqa: BLE001
        return {"check": "agri-localize", "status": "translator-error", "reason": str(e),
                "translated": False, "text": text}
    if out.startswith("[offline-stub]"):
        return {"check": "agri-localize", "status": "untranslated", "language": LANGUAGES[lang],
                "code": lang, "translated": False, "text": text,
                "reason": "no model credentials available; source text returned unchanged"}
    return {"check": "agri-localize", "status": "translated", "language": LANGUAGES[lang],
            "code": lang, "source": "model_wrapper", "translated": True, "text": out}


def render_advice(result: dict) -> str:
    """Plain-text advisory a farmer can read, suitable input for localize()."""
    lines = [result.get("headline") or f"Diagnosis: {result.get('label')}", ""]
    if result.get("explanation"):
        lines += [result["explanation"], ""]
    if result.get("urgency"):
        lines += [f"Urgency: {result['urgency']}", ""]
    if result.get("remedies"):
        lines.append("What to do, in order:")
        for i, r in enumerate(result["remedies"], 1):
            lines.append(f"{i}. {r}")
        lines.append("")
    if result.get("next_steps"):
        lines.append("Next steps:")
        for s in result["next_steps"]:
            lines.append(f"- {s}")
        lines.append("")
    if result.get("disclaimer"):
        lines += ["---", result["disclaimer"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="agri.py", description="Setu Agriculture pack")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("classify")
    c.add_argument("--image", required=True)
    c.add_argument("--crop", default=None)
    c.add_argument("--pretrained", action="store_true",
                    help="load the optional pinned MobileNetV2 PlantVillage classifier")
    c.add_argument("--validate-plant", action="store_true",
                    help="require the optional plant-content model before classifying")

    t = sub.add_parser("treat", help="treat a classification JSON file")
    t.add_argument("--classification", required=True)

    l = sub.add_parser("localize")
    l.add_argument("--text", default=None)
    l.add_argument("--file", default=None)
    l.add_argument("--lang", default="hi")
    l.add_argument("--model", default=None)

    a = ap.parse_args(argv)
    if a.cmd == "classify":
        classifier = None
        image_validator = None
        try:
            if a.validate_plant:
                from agri_image import load_plant_validator
                image_validator = load_plant_validator()
            if a.pretrained:
                from agri_model import load_classifier
                classifier = load_classifier()
        except Exception as e:  # noqa: BLE001 — optional model failures fail closed
            print(json.dumps({"check": "agri-classify", "status": "unavailable",
                              "reason": str(e), "confidence": 0.0,
                              "treatment_allowed": False}, indent=2))
            return 2
        result = classify(a.image, a.crop, classifier=classifier,
                          image_validator=image_validator)
        print(json.dumps(result, indent=2))
        return 0 if result.get("status") not in {"invalid-image", "image-validation-error",
                                                  "unavailable"} else 2
    if a.cmd == "treat":
        data = json.loads(Path(a.classification).read_text(encoding="utf-8"))
        res = treat(data)
        res["advisory_text"] = render_advice(res)
        print(json.dumps(res, indent=2))
        return 0 if res["status"] == "ok" else 1
    if a.cmd == "localize":
        text = a.text or (Path(a.file).read_text(encoding="utf-8") if a.file else "")
        if not text:
            ap.error("localize requires --text or --file")
        print(json.dumps(localize(text, a.lang, model=a.model), indent=2, ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
