#!/usr/bin/env python3
"""Setu Cyber — CVSS v3.1 severity scorer.

Implements the CVSS v3.1 base-score algorithm *exactly* (including the spec's
round-up function, which is not ``round()`` — the difference matters at every
0.05 boundary). No model call: a severity number that a triager will argue with
must come from arithmetic they can reproduce, not from a language model.

    python3 severity_scorer.py findings.json --out ranked.json
    python3 severity_scorer.py --vector "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"
    python3 severity_scorer.py findings.json --md ranked.md

Ranking combines two independent things and never conflates them:

    cvss_base     — how bad it is IF real         (what triagers care about)
    confidence    — how sure we are it IS real    (what the tester knows)
    priority      — cvss_base x confidence        (what to fix first)

A 9.8 that might not exist ranks below a 7.5 that definitely does, and both
numbers stay visible so a human can disagree with the weighting.

Reference: FIRST CVSS v3.1 specification, section 7.1 (base equations).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

# --- metric weights (CVSS v3.1 spec, section 7.4) -------------------------- #
AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
AC = {"L": 0.77, "H": 0.44}
PR_UNCHANGED = {"N": 0.85, "L": 0.62, "H": 0.27}
PR_CHANGED = {"N": 0.85, "L": 0.68, "H": 0.50}
UI = {"N": 0.85, "R": 0.62}
CIA = {"H": 0.56, "L": 0.22, "N": 0.0}

BANDS = [(9.0, "critical"), (7.0, "high"), (4.0, "medium"), (0.1, "low"), (0.0, "none")]


def roundup(value: float) -> float:
    """CVSS v3.1 Appendix A round-up: smallest 1-decimal number >= input.

    Uses integer arithmetic on the scaled value to dodge the classic
    4.000000000000001 / 4.9999999999 float traps that make other
    implementations disagree by 0.1.
    """
    scaled = int(round(value * 100000))
    if scaled % 10000 == 0:
        return scaled / 100000.0
    return (math.floor(scaled / 10000) + 1) / 10.0


def parse_vector(vector: str) -> dict:
    """'CVSS:3.1/AV:N/AC:L/...' -> {'AV':'N', ...}. Raises on missing metrics."""
    parts = vector.strip().split("/")
    if parts and parts[0].startswith("CVSS:"):
        parts = parts[1:]
    out = {}
    for p in parts:
        if ":" not in p:
            raise ValueError(f"malformed metric '{p}' in vector")
        k, v = p.split(":", 1)
        out[k.strip().upper()] = v.strip().upper()
    for req in ("AV", "AC", "PR", "UI", "S", "C", "I", "A"):
        if req not in out:
            raise ValueError(f"CVSS vector missing required metric {req}")
    if out["S"] not in ("U", "C"):
        raise ValueError(f"S must be U or C, got {out['S']}")
    return out


def base_score(vector: str) -> dict:
    """Compute the CVSS v3.1 base score + its component parts."""
    m = parse_vector(vector)
    changed = m["S"] == "C"
    try:
        iss = 1 - ((1 - CIA[m["C"]]) * (1 - CIA[m["I"]]) * (1 - CIA[m["A"]]))
        if changed:
            impact = 7.52 * (iss - 0.029) - 3.25 * ((iss - 0.02) ** 15)
        else:
            impact = 6.42 * iss
        pr = (PR_CHANGED if changed else PR_UNCHANGED)[m["PR"]]
        exploitability = 8.22 * AV[m["AV"]] * AC[m["AC"]] * pr * UI[m["UI"]]
    except KeyError as e:
        raise ValueError(f"invalid metric value {e} in vector {vector}")
    if impact <= 0:
        score = 0.0
    elif changed:
        score = roundup(min(1.08 * (impact + exploitability), 10))
    else:
        score = roundup(min(impact + exploitability, 10))
    return {
        "vector": "CVSS:3.1/" + "/".join(f"{k}:{m[k]}" for k in
                                        ("AV", "AC", "PR", "UI", "S", "C", "I", "A")),
        "base_score": score,
        "band": band_of(score),
        "impact_subscore": round(impact, 3),
        "exploitability_subscore": round(exploitability, 3),
        "scope_changed": changed,
    }


def band_of(score: float) -> str:
    for threshold, name in BANDS:
        if score >= threshold:
            return name
    return "none"


# --------------------------------------------------------------------------- #
# finding type -> CVSS vector template
# --------------------------------------------------------------------------- #
# Each template is the *default* for that class. The scorer lets a specialist
# override by putting a "cvss" vector in the finding, because only the person
# who actually exploited it knows whether UI:R applies.
VECTOR_TEMPLATES = {
    "sqli-injection":          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    "xss-injection":           "CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N",
    "traversal-injection":     "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N",
    "ssti-injection":          "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
    "secret-exposure":         "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:N",
    "vulnerable-dependency":   "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:L/A:L",
    "exposed-endpoint":        "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N",
    "exposed-internal-service": "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N",
    "misconfiguration":        "CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:L/A:N",
    "information-disclosure":  "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:L/I:N/A:N",
    "cors":                    "CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:C/C:H/I:N/A:N",
    "transport":               "CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:U/C:H/I:L/A:N",
    "email-spoofing":          "CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:C/C:L/I:H/A:N",
    "attack-surface":          "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:N/A:N",
    "recon":                   "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:N/A:N",
    "testing-note":            "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:N/A:N",
    "coverage-note":           "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:N/I:N/A:N",
    "generic":                 "CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:L/I:N/A:N",
}

# Informational classes carry no CVSS — pretending "attack surface" is a 5.3
# inflates every report. They keep severity=info and score 0.
INFORMATIONAL = {"recon", "attack-surface", "testing-note", "coverage-note"}


def score_finding(f: dict) -> dict:
    """Attach a CVSS vector, base score and priority to one finding (non-mutating copy)."""
    out = dict(f)
    ftype = str(out.get("type", "generic"))
    if out.get("cvss"):
        try:
            info = base_score(out["cvss"])
        except ValueError as e:
            info = {"vector": None, "base_score": 0.0, "band": "none",
                    "error": str(e)}
    elif ftype in INFORMATIONAL:
        info = {"vector": None, "base_score": 0.0, "band": "none",
                "note": "informational class carries no CVSS — attack surface, not a break"}
    else:
        vector = VECTOR_TEMPLATES.get(ftype, VECTOR_TEMPLATES["generic"])
        info = base_score(vector)
    conf = max(0.0, min(float(out.get("confidence", 0.0) or 0.0), 1.0))
    out.update({
        "cvss_vector": info.get("vector"),
        "cvss_base": info.get("base_score"),
        "cvss_band": info.get("band"),
        "cvss_note": info.get("note") or info.get("error"),
        "confidence": round(conf, 3),
        "priority_score": round(float(info.get("base_score") or 0.0) * conf, 2),
    })
    # Precedence, and the reason it is in this order:
    #   * CVSS describes how bad the bug is IF it is real, so for a confirmed
    #     finding it is the authority and overrides the specialist's guess.
    #   * For an unconfirmed one (e.g. reflection with no executed-effect
    #     signal) the CVSS is a *potential* score only. Letting it set severity
    #     would turn a mirrored string into a CRITICAL, which is exactly the
    #     inflation that gets reports thrown away. The specialist's lower
    #     severity stands, and the potential score is kept for context.
    confirmed = out.get("outcome_confirmed") is not False
    if info.get("base_score"):
        if confirmed:
            out["severity"] = info["band"] if info["base_score"] >= 0.1 else "info"
        else:
            out["potential_cvss_base"] = info["base_score"]
            out["cvss_note"] = ("potential score if the observed reflection is actually "
                                "exploitable — unconfirmed, so severity is not raised on it")
    return out


def rank(findings: list[dict]) -> list[dict]:
    scored = [score_finding(f) for f in common.normalize_findings(findings)]
    scored.sort(key=lambda f: (-f["priority_score"], -f.get("cvss_base") or 0,
                               -f.get("confidence") or 0))
    for i, f in enumerate(scored):
        f["rank"] = i + 1
    return scored


def to_markdown(ranked: list[dict]) -> str:
    lines = ["| # | Severity | CVSS | Conf | Priority | Finding | Type |",
             "|---|---|---|---|---|---|---|"]
    for f in ranked:
        cvss = f"{f['cvss_base']:.1f}" if f.get("cvss_base") else "—"
        flag = " ⚠needs-review" if f.get("needs_manual_review") else ""
        text = str(f.get("finding", ""))[:90].replace("|", "\\|")
        lines.append(f"| {f['rank']} | {f.get('severity','info')} | {cvss} | "
                     f"{f.get('confidence', 0):.2f} | {f.get('priority_score', 0):.1f} | "
                     f"{text}{flag} | {f.get('type','')} |")
    return "\n".join(lines)


def _selftest() -> int:
    # Reference vectors from the CVSS 3.1 spec / FIRST calculator.
    cases = [
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8),
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1),   # textbook reflected XSS
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:N/A:N", 7.5),
        ("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N", 1.8),
        ("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H", 9.9),
        # hand-verified: impact 2.514 + exploitability 1.620 = 4.134 -> roundup 4.2
        ("CVSS:3.1/AV:N/AC:H/PR:N/UI:R/S:U/C:L/I:L/A:N", 4.2),
        # scope-changed branch: 1.08 * (1.436 + 1.777) = 3.470 -> roundup 3.5
        ("CVSS:3.1/AV:N/AC:H/PR:L/UI:N/S:C/C:L/I:N/A:N", 3.5),
        # zero-impact must score 0.0 regardless of exploitability
        ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N", 0.0),
    ]
    bad = 0
    for vec, expected in cases:
        got = base_score(vec)["base_score"]
        ok = abs(got - expected) < 1e-9
        print(f"{'OK  ' if ok else 'FAIL'} {vec} -> {got} (expected {expected})")
        bad += 0 if ok else 1
    print("roundup checks:", [roundup(x) for x in (4.000000000000001, 4.02, 4.0, 9.999999)])
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="severity_scorer.py",
                                 description="CVSS v3.1 scoring + ranking for Setu findings")
    ap.add_argument("file", nargs="?", help="JSON array of findings")
    ap.add_argument("--vector", help="score a single CVSS vector and exit")
    ap.add_argument("--out", help="write ranked JSON here")
    ap.add_argument("--md", help="also write a markdown table here")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)

    if a.selftest:
        return _selftest()
    if a.vector:
        print(json.dumps(base_score(a.vector), indent=2))
        return 0
    if not a.file:
        ap.error("pass a findings JSON file, --vector, or --selftest")

    data = json.loads(Path(a.file).read_text(encoding="utf-8"))
    findings = data.get("findings", data) if isinstance(data, dict) else data
    ranked = rank(findings)
    if a.out:
        common.write_json(a.out, ranked)
        print(f"wrote {len(ranked)} ranked finding(s) -> {a.out}", file=sys.stderr)
    if a.md:
        Path(a.md).write_text(to_markdown(ranked) + "\n", encoding="utf-8")
        print(f"wrote table -> {a.md}", file=sys.stderr)
    if not a.out:
        print(json.dumps(ranked, indent=2))
    else:
        print(to_markdown(ranked))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
