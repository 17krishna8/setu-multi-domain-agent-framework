#!/usr/bin/env python3
"""Setu — cross-pack utilities.

Small on purpose. Three things every pack needs and nothing else:

1. ``finding()`` / ``normalize_findings()`` — the finding shape the Cyber pack
   agrees on, and the rule that *no evidence means no confidence*.
2. ``severity_hint()`` — confidence -> severity, with the CVSS detail in
   ``severity_scorer.py`` (which can override this with real maths).
3. ``read_json`` / ``write_json`` — atomic, tolerant JSON I/O used by every
   script, so a half-written file never breaks a pipeline.

Why the evidence rule matters: a security report is a promise that the finding
is real. A specialist that returns ``{"finding": "possible SQLi"}`` with no
payload, no URL and no response snippet has given the reader nothing they can
verify or reproduce. So we zero its confidence and tag it, rather than quietly
letting it into a ranked list where "confidence 0.9" implies "we checked".
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SEVERITIES = ("critical", "high", "medium", "low", "info")
_SEV_ORDER = {s: i for i, s in enumerate(SEVERITIES)}


def finding(finding: str, severity: str, confidence: float, evidence: Any,
            specialist: str = "unknown", target: str | None = None,
            ftype: str = "generic", cwe: str | None = None, **extra: Any) -> dict:
    """Build a finding in the pack's canonical shape.

    ``evidence`` is required and must be non-empty to earn non-zero confidence.
    """
    sev = severity.lower() if isinstance(severity, str) else "info"
    if sev not in _SEV_ORDER:
        sev = "info"
    try:
        conf = float(confidence)
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(conf, 1.0))
    out = {
        "finding": str(finding).strip(),
        "severity": sev,
        "confidence": round(conf, 3),
        "evidence": evidence if evidence not in (None, "", {}, []) else None,
        "specialist": specialist,
        "type": ftype,
    }
    if target:
        out["target"] = target
    if cwe:
        out["cwe"] = cwe
    out.update(extra)
    if out["evidence"] is None:
        out["confidence"] = 0.0
        out["needs_manual_review"] = True
        out["review_reason"] = "no evidence supplied — claim not verifiable as written"
    return out


def normalize_findings(findings: list[dict]) -> list[dict]:
    """Coerce anything a specialist produced into the canonical shape, sorted."""
    out: list[dict] = []
    for i, f in enumerate(findings or []):
        if not isinstance(f, dict):
            continue
        ev = f.get("evidence")
        if ev is None or ev == {} or ev == [] or ev == "":
            f["confidence"] = 0.0
            f["needs_manual_review"] = True
            f.setdefault("review_reason", "no evidence supplied")
        f.setdefault("severity", "info")
        f.setdefault("confidence", 0.0)
        f.setdefault("id", f"F{i + 1:03d}")
        f.setdefault("type", "generic")
        f.setdefault("specialist", "unknown")
        out.append(f)
    out.sort(key=lambda f: (_SEV_ORDER.get(str(f.get("severity", "info")).lower(), 4),
                            -float(f.get("confidence", 0) or 0)))
    for i, f in enumerate(out):
        f["rank"] = i + 1
    return out


def severity_hint(confidence: float, boost: str | None = None) -> str:
    """Confidence -> provisional severity. ``severity_scorer.py`` refines with CVSS."""
    if confidence >= 0.9:
        sev = "critical"
    elif confidence >= 0.75:
        sev = "high"
    elif confidence >= 0.5:
        sev = "medium"
    elif confidence >= 0.25:
        sev = "low"
    else:
        sev = "info"
    if boost and _SEV_ORDER.get(boost.lower(), 9) < _SEV_ORDER[sev]:
        sev = boost.lower()
    return sev


def configure_console() -> None:
    """Avoid UnicodeEncodeError when diagnostics reach legacy Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):
                pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path).expanduser()
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: str | Path, data: Any) -> Path:
    """Atomic write: temp file in the same directory, then rename."""
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return p


def truncate(text: str, n: int = 200) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


DISCLAIMERS = {
    "finance": ("This is general information, not financial advice. Setu is not a "
                "registered investment adviser; verify rates and terms with your lender."),
    "agri": ("Treatment guidance is general and may not fit your crop variety, soil or "
             "local pesticide regulations. Confirm with your local agriculture extension "
             "officer before applying anything."),
    "admin": ("Draft only — NOT a final or filed document. A human must review, correct "
              "and sign it. Setu does not provide legal advice or representation."),
    "cyber": ("Findings describe observations under authorized testing conditions. "
              "Confirm each item manually before submitting to any bug-bounty program."),
}


def with_disclaimer(text: str, pack: str) -> str:
    d = DISCLAIMERS.get(pack)
    if not d:
        return text
    return f"{text.rstrip()}\n\n---\n_{d}_\n"


def median_adjusted_confidence(values: list[float]) -> float:
    """Median of per-observer confidences — used by dedup to merge duplicates
    without letting one enthusiastic observer drag the group upward."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 0.0
    n = len(vals)
    mid = n // 2
    return round(vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2, 3)
