#!/usr/bin/env python3
"""Setu Web Dev — the design critic, as code.

`webdev-design-critic` is required to hunt five specific generic-AI-site
patterns. If that hunting is done by a language model re-reading its own
sibling's output, it becomes a vibe check and rubber-stamps everything. So the
five checks are implemented here as deterministic AST-ish rules over the HTML and
CSS that was actually written, and the critic agent consumes this JSON.

Checks (each maps to one line of the critic's brief)
  1. ``gradient-purple-blue``  — purple/indigo/blue gradients, the default AI look
  2. ``font-generic``          — Inter/system-ui/Roboto with no deliberate pairing
  3. ``template-structure``    — centered hero → exactly 3 cards → footer, unchanged
  4. ``emoji-as-icon``         — emoji standing in for functional icons
  5. ``placeholder-copy``      — lorem ipsum, "Feature one", "Your headline here"

Design notes on the rules themselves, because a linter that cries wolf is worse
than none:

* Hues are computed in HSL from the actual colour values, so a purple gradient
  written as ``#7c3aed`` or ``rgb(124,58,237)`` or ``bg-indigo-500`` all match.
  A *warm* gradient (terracotta→amber) is not flagged, because the complaint is
  not "gradients" — it is "the same cool-purple gradient on every generated site".
* The font rule flags the *absence of a decision*, not the use of a common font.
  Inter paired with a distinctive display face is a decision; Inter alone is a
  default. Same for system-ui.
* The structure rule needs the trinity to fire: a hero, then exactly three
  siblings that look like cards, then a footer. Two cards or a five-card bento
  is a different layout and does not fire.

    python3 design_check.py index.html styles.css --json
    python3 design_check.py site/ --json --out report/design.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from colorsys import rgb_to_hls
from pathlib import Path

GENERIC_FONTS = {"inter", "system-ui", "-apple-system", "roboto", "arial",
                 "helvetica", "helvetica neue", "segoe ui", "sans-serif",
                 "open sans", "lato", "noto sans", "ubuntu"}

# A pairing is "deliberate" when a second, non-generic family is present:
# a serif, a mono, or a named display face.
# NOTE: "serif" would also match "sans-serif" and "sans-serif" is a *default*
# fallback, not a decision — so the generic stack is excluded before this runs.
DISTINCTIVE_HINT = re.compile(
    r"serif|mono|display|slab|grotesk|grotesque|playfair|fraunces|instrument|"
    r"space\s*grotesk|dm\s*serif|libre|sora|outfit|clash|satoshi|general\s*sans|"
    r"newsreader|bricolage|anton|archivo|manrope|geist|chivo|epilogue|farro|"
    r"bittersweet|quicksand|cabinet|bespoke|editorial", re.I)

PLACEHOLDER_PATTERNS = [
    (r"lorem\s+ipsum", "lorem ipsum"),
    (r"\byour\s+(headline|title|tagline|text)\s+here\b", "your headline here"),
    (r"\bfeature\s+(one|two|three|1|2|3)\b", "feature one/two/three"),
    (r"\b(amazing|awesome|revolutionary|cutting[- ]edge|next[- ]gen(eration)?)\s+"
     r"(product|solution|platform|app|tool)\b", "generic superlative copy"),
    (r"\bbest[- ]in[- ]class\b", "best-in-class"),
    (r"\bplaceholder\b", "literal placeholder text"),
    (r"\blorem\b", "lorem"),
    (r"\binsert\s+\w+\s+here\b", "insert X here"),
    (r"\bcompany\s+name\b", "company name"),
]

HEX_RE = re.compile(r"#([0-9a-fA-F]{3,8})\b")
RGB_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
TAILWIND_COOL = re.compile(
    r"\b(?:from|via|to|bg|text|border)-(indigo|purple|violet|blue|sky|cyan)-(\d{2,3})\b")
GRADIENT_RE = re.compile(r"(?:linear|radial|conic)-gradient\(([^;{}]*)", re.I)
EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2190-\u21FF\u2B00-\u2BFF]")


def _hex_to_rgb(h: str) -> tuple[int, int, int] | None:
    h = h.strip()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    elif len(h) == 8:
        h = h[:6]
    elif len(h) != 6:
        return None
    try:
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return None


def _hue_sat(r: int, g: int, b: int) -> tuple[float, float, float]:
    """-> (hue 0-360, saturation 0-1, lightness 0-1).

    Note the order: colorsys returns (h, LIGHTNESS, SATURATION). Unpacking it as
    (h, s, l) silently swaps two numbers and makes every colour test wrong while
    still looking plausible — which is the worst kind of bug in a linter.
    """
    h, lightness, saturation = rgb_to_hls(r / 255, g / 255, b / 255)
    return h * 360, saturation, lightness


def _is_cool_ai_purple(r: int, g: int, b: int) -> bool:
    """Hue 210-300 with real saturation and mid lightness = the default AI look."""
    hue, sat, light = _hue_sat(r, g, b)
    return 210 <= hue <= 300 and sat >= 0.35 and 0.25 <= light <= 0.85


def check_gradients(text: str) -> list[dict]:
    hits = []
    for m in GRADIENT_RE.finditer(text):
        body = m.group(1)
        stops = [c for c in HEX_RE.findall(body) if _hex_to_rgb(c)]
        cool = [c for c in stops if _is_cool_ai_purple(*_hex_to_rgb(c))]  # type: ignore[misc]
        rgb_cool = [t for t in RGB_RE.findall(body)
                    if _is_cool_ai_purple(int(t[0]), int(t[1]), int(t[2]))]
        tw = TAILWIND_COOL.search(body) or TAILWIND_COOL.search(m.group(0))
        if len(cool) + len(rgb_cool) >= 2 or (tw and len(cool) + len(rgb_cool) >= 1):
            hits.append({
                "rule": "gradient-purple-blue",
                "severity": "blocking",
                "evidence": {"gradient": m.group(0)[:120],
                             "cool_stops": cool + [f"rgb{r}" for r in rgb_cool],
                             "tailwind": tw.group(0) if tw else None},
                "why": "Purple/indigo/blue gradient — the default AI-generated palette. "
                       "It says nothing about this project's subject.",
                "suggestion": "Derive the palette from the subject: a bakery is flour-white "
                              "and scorched-crust brown; a security tool is terminal green on "
                              "near-black; a farm co-op is soil ochre and leaf green.",
            })
    return hits


def check_fonts(text: str) -> list[dict]:
    families: list[str] = []
    for m in re.finditer(r"font-family\s*:\s*([^;}\n]+)", text, re.I):
        for fam in m.group(1).split(","):
            fam = fam.strip().strip("'\"").lower()
            if fam:
                families.append(fam)
    for m in re.finditer(r"['\"]family=([^'\":&]+)", text):     # google fonts url
        for fam in m.group(1).split("|"):
            families.append(fam.split(":")[0].strip().replace("+", " ").lower())
    uniq = list(dict.fromkeys(families))
    if not uniq:
        return [{
            "rule": "font-generic", "severity": "blocking",
            "evidence": {"families": []},
            "why": "No font-family declared at all — the page inherits the browser default.",
            "suggestion": "Pick a deliberate pairing: a display face for headings and a "
                          "reading face for body. State both in the output note.",
        }]
    CSS_KEYWORDS = {"sans-serif", "serif", "monospace", "cursive", "fantasy",
                    "inherit", "initial", "unset"}
    non_generic = [f for f in uniq if f not in GENERIC_FONTS and f not in CSS_KEYWORDS]
    generic_only = [f for f in uniq if f in GENERIC_FONTS]
    # Only candidates that are neither generic nor a bare CSS keyword can count
    # as a deliberate pairing choice.
    distinctive = [f for f in uniq
                   if f not in GENERIC_FONTS and f not in CSS_KEYWORDS
                   and DISTINCTIVE_HINT.search(f)]
    if not non_generic and not distinctive:
        return [{
            "rule": "font-generic", "severity": "blocking",
            "evidence": {"families": uniq, "generic": generic_only},
            "why": "Only default UI fonts — no pairing decision was made.",
            "suggestion": "Add a distinctive second family (a serif, a grotesk or a mono) "
                          "and use it for a real role: headings, numerals, labels.",
        }]
    return []


def check_structure(html: str) -> list[dict]:
    body = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    hero = re.search(r"<(?:header|section|div)[^>]*(?:class|id)=\"[^\"]*\bhero\b", body, re.I)
    card_count = len(re.findall(r"<(?:div|article|section)[^>]*(?:class|id)=\"[^\"]*\bcard\b",
                                body, re.I))
    footer = re.search(r"<footer\b", body, re.I)
    centered = bool(re.search(r"text-align\s*:\s*center", body + html, re.I)) or \
        len(re.findall(r"\b(?:text-)?center\b", body, re.I)) >= 2
    if hero and footer and card_count == 3 and centered:
        return [{
            "rule": "template-structure", "severity": "blocking",
            "evidence": {"hero": True, "exactly_three_cards": True, "footer": True,
                         "centered": True},
            "why": "Centered hero + exactly 3 cards + footer — the default generated layout. "
                   "The structure carries no information about this content.",
            "suggestion": "Let the content choose the shape: an asymmetric editorial split, a "
                          "long-scroll narrative with a sticky margin note, a dense data table, "
                          "a map-led layout, a two-column ledger, or a bento of 2/5/4.",
        }]
    return []


def check_emoji_icons(html: str) -> list[dict]:
    hits = []
    needs_icon = re.compile(
        r"<(a|button|nav|li)[^>]*>(?P<inner>.*?)</\1\s*>", re.I | re.S)
    for m in needs_icon.finditer(html):
        inner = re.sub(r"<[^>]+>", "", m.group("inner"))
        found = EMOJI_RE.findall(inner)
        if found:
            hits.append({"rule": "emoji-as-icon", "severity": "blocking",
                         "evidence": {"element": m.group(1),
                                      "text": inner.strip()[:80],
                                      "emoji": found[:5]},
                         "why": "Emoji used as a functional icon. It renders differently on "
                                "every OS, cannot be styled, and reads as unserious.",
                         "suggestion": "Use an inline SVG (18-20px, currentColor, 1.5px "
                                       "stroke) drawn for this project."})
            break
    # Also catch icon-font-ish patterns replaced by emoji in headings/nav
    if not hits:
        for m in re.finditer(r"<h[1-3][^>]*>(?P<t>.*?)</h[1-3]>", html, re.I | re.S):
            if EMOJI_RE.search(re.sub(r"<[^>]+>", "", m.group("t"))):
                hits.append({"rule": "emoji-as-icon", "severity": "advisory",
                             "evidence": {"heading": m.group("t")[:80]},
                             "why": "Emoji in a heading presents as decoration-as-design.",
                             "suggestion": "Replace with a typographic device or an SVG."})
                break
    return hits


def check_copy(text: str) -> list[dict]:
    hits = []
    plain = re.sub(r"<[^>]+>", " ", text)
    for pat, label in PLACEHOLDER_PATTERNS:
        m = re.search(pat, plain, re.I)
        if m:
            hits.append({"rule": "placeholder-copy", "severity": "blocking",
                         "evidence": {"match": m.group(0)[:60], "label": label},
                         "why": "Placeholder or generic superlative copy — it fills space "
                                "without saying anything true about this product.",
                         "suggestion": "Write copy with a specific noun and a specific number "
                                       "from the brief: who it is for, what it does, what it "
                                       "costs, what changes."})
            break
    return hits


def run_checks(paths: list[Path]) -> dict:
    html_blobs, css_blobs = [], []
    for p in paths:
        if p.is_dir():
            for f in p.rglob("*"):
                if f.suffix.lower() in {".html", ".htm"}:
                    html_blobs.append(f.read_text(encoding="utf-8", errors="replace"))
                elif f.suffix.lower() in {".css", ".scss"}:
                    css_blobs.append(f.read_text(encoding="utf-8", errors="replace"))
                elif f.suffix.lower() in {".jsx", ".tsx", ".vue", ".svelte"}:
                    html_blobs.append(f.read_text(encoding="utf-8", errors="replace"))
        elif p.suffix.lower() in {".html", ".htm"}:
            html_blobs.append(p.read_text(encoding="utf-8", errors="replace"))
        elif p.suffix.lower() in {".css", ".scss"}:
            css_blobs.append(p.read_text(encoding="utf-8", errors="replace"))
        elif p.exists():
            html_blobs.append(p.read_text(encoding="utf-8", errors="replace"))

    html = "\n".join(html_blobs)
    css = "\n".join(css_blobs)
    everything = html + "\n" + css

    violations: list[dict] = []
    violations += check_gradients(everything)
    violations += check_fonts(everything)
    violations += check_structure(html)
    violations += check_emoji_icons(html)
    violations += check_copy(html)

    blocking = [v for v in violations if v["severity"] == "blocking"]
    score = max(0, 100 - 25 * len(blocking) - 5 * (len(violations) - len(blocking)))
    return {
        "verdict": "REJECT" if blocking else "APPROVE",
        "score": score,
        "blocking": len(blocking),
        "violations": violations,
        "checked": {"html_chars": len(html), "css_chars": len(css),
                    "files": [str(p) for p in paths]},
        "retry_guidance": ("Send back to webdev-builder with these specific violations. "
                           "Max 2 retry loops; on the third, escalate to the user with the "
                           "violations listed rather than looping forever."
                           if blocking else "Clean: no generic-AI-site patterns found."),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="design_check.py",
                                 description="Deterministic generic-AI-site linter for Setu Web Dev")
    ap.add_argument("paths", nargs="+")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    paths = [Path(p) for p in a.paths]
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print(f"design_check: not found: {missing}", file=sys.stderr)
        return 2
    result = run_checks(paths)
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    if a.json or a.out:
        print(json.dumps(result, indent=2))
    else:
        print(f"{result['verdict']}  score={result['score']}  "
              f"blocking={result['blocking']}")
        for v in result["violations"]:
            print(f"\n[{v['severity']:8s}] {v['rule']}")
            print(f"  why: {v['why']}")
            print(f"  evidence: {json.dumps(v['evidence'])[:200]}")
            print(f"  fix: {v['suggestion']}")
    return 0 if result["verdict"] == "APPROVE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
