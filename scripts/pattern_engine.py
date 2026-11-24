#!/usr/bin/env python3
"""Setu shared pattern engine.

One matcher, two consumers:

  * ``cyber-injection`` / ``cyber-crypto-weakness`` — match attack payloads and
    error signatures against HTTP responses and source code.
  * ``finance-scam-check`` — match a suspicious SMS/WhatsApp/email against
    fraud patterns.

Both read the same ``scripts/rules/patterns.json``. Adding a rule there improves
both packs, and the scoring is identical, so "how confident is Setu about this
signal" means the same thing in every pack. That is the whole point of the
shared layer: one scorer, one vocabulary, multiple domains.

Scoring
-------
Weights are signal strengths in 0-1. They combine with probabilistic OR::

    confidence = 1 - prod(1 - w_i)

so three weak signals (0.5, 0.5, 0.5) reach 0.875 while any single strong
signal (0.85) alone stays honest at 0.85. We never claim certainty from one
regex, because one regex is exactly how false positives are born.

CLI
---
    python3 pattern_engine.py --category sqli --text "$(cat response.txt)"
    python3 pattern_engine.py --check scam --text "Dear customer, your KYC..."
    python3 pattern_engine.py --list
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
# After `install.sh --target cursor`, scripts and rules land in different
# subdirectories, so look in every place a Setu install can put the rules.
_RULE_CANDIDATES = [
    _HERE / "rules" / "patterns.json",
    _HERE.parent / "rules" / "patterns.json",
    _HERE.parent / ".cursor" / "rules" / "patterns.json",
]
RULES_PATH = next((p for p in _RULE_CANDIDATES if p.exists()), _RULE_CANDIDATES[0])

# Confidence bands, shared by every pack so a "high" means the same everywhere.
BANDS = [(0.85, "critical"), (0.70, "high"), (0.45, "medium"), (0.20, "low"), (0.0, "info")]


@dataclass
class Match:
    rule_id: str
    label: str
    category: str
    weight: float
    snippet: str
    span: tuple[int, int]
    confirms_outcome: bool = False   # False = this rule firing may just be our own payload

    def as_dict(self) -> dict:
        return {"rule_id": self.rule_id, "label": self.label, "category": self.category,
                "weight": self.weight, "snippet": self.snippet,
                "span": list(self.span), "confirms_outcome": self.confirms_outcome}


@dataclass
class Verdict:
    category: str
    confidence: float
    band: str
    matches: list[Match] = field(default_factory=list)

    @property
    def outcome_confirmed(self) -> bool:
        """True when at least one rule that proves an *effect* fired.

        A reflected payload is not an exploit. If every match is a
        ``confirms_outcome: false`` rule, the response only echoes what we sent,
        and callers should not rate this above "possible".
        """
        return any(m.confirms_outcome for m in self.matches)

    def as_dict(self) -> dict:
        return {"category": self.category, "confidence": round(self.confidence, 3),
                "band": self.band, "match_count": len(self.matches),
                "outcome_confirmed": self.outcome_confirmed,
                "matches": [m.as_dict() for m in self.matches]}

    def __bool__(self) -> bool:
        return self.confidence >= 0.45


def load_rules(path: str | Path | None = None) -> dict:
    p = Path(path) if path else RULES_PATH
    if not p.exists():
        raise FileNotFoundError(f"pattern rules not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def band_of(confidence: float) -> str:
    for threshold, name in BANDS:
        if confidence >= threshold:
            return name
    return "info"


def _compile(regex: str) -> re.Pattern:
    """Compile with DOTALL but not IGNORECASE — case-insensitive rules inline (?i)."""
    return re.compile(regex, re.DOTALL)


_RULE_CACHE: dict[str, re.Pattern] = {}


def _regex(rule: dict) -> re.Pattern:
    rid = rule["id"]
    if rid not in _RULE_CACHE:
        _RULE_CACHE[rid] = _compile(rule["regex"])
    return _RULE_CACHE[rid]


def _snippet(text: str, span: tuple[int, int], pad: int = 40) -> str:
    a = max(span[0] - pad, 0)
    b = min(span[1] + pad, len(text))
    raw = text[a:b].replace("\n", " ⏎ ")
    return ("…" if a > 0 else "") + raw.strip() + ("…" if b < len(text) else "")


def match_category(text: str, category: str, rules: dict | None = None,
                   max_snippet: int = 160) -> Verdict:
    """Run every rule in `category` against `text` and combine with probabilistic OR."""
    rules = rules or load_rules()
    cat = rules.get("categories", {}).get(category)
    if cat is None:
        raise KeyError(f"unknown pattern category '{category}'; "
                       f"have: {', '.join(rules.get('categories', {}))}")
    matches: list[Match] = []
    order = rules["categories"][category]["rules"]
    for rule in order:
        m = _regex(rule).search(text or "")
        if not m:
            continue
        matches.append(Match(rule_id=rule["id"], label=rule["label"], category=category,
                             weight=float(rule.get("weight", 0.5)),
                             snippet=_snippet(text, m.span())[:max_snippet],
                             span=m.span(),
                             confirms_outcome=bool(rule.get("confirms_outcome", True))))
    # Dedupe by rule: one hit per rule is enough; repeated hits don't compound.
    conf = 1.0
    for m in matches:
        conf *= (1.0 - max(0.0, min(m.weight, 0.999)))
    confidence = 1.0 - conf
    matches.sort(key=lambda m: (m.confirms_outcome, m.weight), reverse=True)
    return Verdict(category=category, confidence=confidence, band=band_of(confidence),
                   matches=matches)


def match_all(text: str, rules: dict | None = None) -> dict[str, Verdict]:
    rules = rules or load_rules()
    return {c: match_category(text, c, rules) for c in rules.get("categories", {})}


def explain(verdict: Verdict) -> str:
    """Human-readable one-paragraph explanation — used in reports and chat output."""
    if not verdict.matches:
        return f"No {verdict.category} signals matched. Confidence 0.0 — which is not proof of absence."
    lines = [f"{verdict.category}: confidence {verdict.confidence:.2f} ({verdict.band}) "
             f"from {len(verdict.matches)} signal(s):"]
    for m in verdict.matches:
        lines.append(f"  - [{m.weight:.2f}] {m.label} ({m.rule_id})")
        lines.append(f"      {m.snippet}")
    return "\n".join(lines)


def _read_text_arg(args) -> str:
    if args.file:
        return Path(args.file).read_text(encoding="utf-8", errors="replace")
    if args.text is not None:
        return args.text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise SystemExit("pass --text, --file, or pipe text on stdin")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pattern_engine.py",
                                 description="shared regex signal engine (cyber + finance)")
    ap.add_argument("--category", default=None, help="pattern category, e.g. sqli/scam/secrets")
    ap.add_argument("--check", default=None, help="alias for --category")
    ap.add_argument("--all", action="store_true", help="run every category, print JSON")
    ap.add_argument("--text", default=None)
    ap.add_argument("--file", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--rules", default=None)
    a = ap.parse_args(argv)

    rules = load_rules(a.rules)

    if a.list:
        for cat, body in rules["categories"].items():
            print(f"{cat}  ({body.get('class')}) — {len(body['rules'])} rules")
            for r in body["rules"]:
                print(f"    {r['id']:24s} w={r.get('weight', 0.5):<4} {r['label']}")
        return 0

    cat = a.check or a.category
    if a.all:
        text = _read_text_arg(a)
        out = {k: v.as_dict() for k, v in match_all(text, rules).items()
               if v.matches}
        print(json.dumps(out, indent=2))
        return 0
    if not cat:
        ap.error("pass --category/--check, --all, or --list")
    text = _read_text_arg(a)
    v = match_category(text, cat, rules)
    if a.json:
        print(json.dumps(v.as_dict(), indent=2))
    else:
        print(explain(v))
    return 0 if v else 1


if __name__ == "__main__":
    raise SystemExit(main())
