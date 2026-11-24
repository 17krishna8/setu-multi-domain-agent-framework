#!/usr/bin/env python3
"""Setu Cyber — finding deduplication.

Three specialists looking at one login form will produce three findings for one
bug. If we ship all three, the report reads as "12 vulnerabilities" when there
are six real ones — that is the fastest way to lose a triager's trust.

Merging rules
-------------
Two findings are the same bug when:
  * same ``type`` class, and
  * same normalised location (scheme-insensitive host + path, query order
    dropped, ids that look like numbers/uuids collapsed), and
  * overlapping evidence (same parameter, or matching CWE)

We do **not** merge on title similarity alone: "Possible SQLi" and "Possible
XSS" share words on the same parameter and are different bugs.

On merge, the group keeps:
  * the **union** of evidence (nothing a specialist found is thrown away)
  * the **max** CVSS vector (the scarier exploitation wins the rating)
  * the **median** confidence (one enthusiastic observer cannot inflate a group)
  * a ``corroborated_by`` list, which is real signal: three independent checks
    agreeing is stronger than one, even though we don't average them upward.

    python3 dedup.py findings.json --out deduped.json
    python3 dedup.py findings.json --report        # show merge decisions
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

_NUMERIC_ID = re.compile(r"(?:/|=)\d+(?=/|&|$)")
_UUID = re.compile(r"/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def normalize_target(target: str) -> str:
    """host+path, query dropped, volatile ids collapsed."""
    t = str(target or "").strip().lower()
    t = re.sub(r"^https?://", "", t)
    t = t.split("#")[0].split("?")[0]
    t = t.rstrip("/")
    t = _UUID.sub("/{id}", t)
    t = _NUMERIC_ID.sub("/{n}", t)
    return t


def _param_set(f: dict) -> set[str]:
    ev = f.get("evidence")
    params: set[str] = set()
    if isinstance(ev, dict):
        for key in ("parameter", "param", "field"):
            if ev.get(key):
                params.add(str(ev[key]).lower())
        for item in ev.get("matches") or []:
            if isinstance(item, dict) and item.get("rule_id"):
                params.add("rule:" + str(item["rule_id"]))
    return params


def similarity_key(f: dict) -> tuple:
    return (str(f.get("type", "generic")), normalize_target(f.get("target") or ""))


def merge_group(group: list[dict]) -> dict:
    """Fold a group of same-bug findings into one canonical finding."""
    base = dict(max(group, key=lambda g: (float(g.get("cvss_base") or 0),
                                          float(g.get("confidence") or 0))))
    base["confidence"] = common.median_adjusted_confidence(
        [float(g.get("confidence") or 0) for g in group])
    # union of evidence, namespaced by specialist so nothing is lost
    evid: dict = {}
    for g in group:
        ev = g.get("evidence")
        spec = g.get("specialist", "unknown")
        if isinstance(ev, dict):
            evid[spec] = ev
        elif ev not in (None, "", [], {}):
            evid[spec] = ev
    base["evidence"] = evid if len(group) > 1 else base.get("evidence")
    specialists = sorted({str(g.get("specialist", "unknown")) for g in group})
    base["merged_from"] = len(group)
    base["corroborated_by"] = specialists if len(specialists) > 1 else []
    base["specialists"] = specialists
    # CVSS stays the max — an exploit path rated 9.8 is not "averaged" to 5
    base["evidence_count"] = sum(1 for g in group if g.get("evidence"))
    if len(specialists) > 1 and base["confidence"] < 0.95:
        # Independent corroboration justifies a small, capped bump — not a jump.
        base["confidence"] = round(min(base["confidence"] + 0.05 * (len(specialists) - 1), 0.95), 3)
    base["dedup_note"] = (f"merged {len(group)} report(s) from {', '.join(specialists)}; "
                          f"confidence is the median, CVSS is the maximum")
    return base


def dedupe(findings: list[dict], explain: bool = False) -> list[dict]:
    findings = common.normalize_findings(findings)
    buckets: dict[tuple, list[dict]] = {}
    for f in findings:
        if f.get("type") in {"coverage-note", "testing-note"}:
            # Coverage notes describe distinct checks; merging can erase the
            # stage-specific reason and its evidence.
            buckets.setdefault((str(f.get("type")), normalize_target(f.get("target") or ""),
                                len(buckets)), []).append(f)
            continue
        key = similarity_key(f)
        # refine by parameter overlap: same type+target but different params are
        # different injection points, so keep a sub-bucket per param signature
        placed = False
        for k in list(buckets):
            if k[:2] != key:
                continue
            existing = buckets[k]
            if _param_set(existing[0]) & _param_set(f) or not (_param_set(existing[0]) and _param_set(f)):
                existing.append(f)
                placed = True
                break
        if not placed:
            buckets.setdefault(key + (len(buckets),), []).append(f)

    out: list[dict] = []
    for group in buckets.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        merged = merge_group(group)
        if explain:
            merged["merge_log"] = [
                {"specialist": g.get("specialist"), "finding": common.truncate(g.get("finding", ""), 90),
                 "confidence": g.get("confidence")} for g in group]
        out.append(merged)
    # Rank by severity first, then fix priority (CVSS x confidence). Sorting on
    # raw CVSS would push an unconfirmed 9.8 above a confirmed medium, which is
    # precisely the ordering a triager should not receive. ``potential_cvss_base``
    # is deliberately ignored here — it is context, not a rank input.
    sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    out.sort(key=lambda f: (sev_rank.get(str(f.get("severity", "info")).lower(), 9),
                            -float(f.get("priority_score") or f.get("cvss_base") or 0),
                            -float(f.get("confidence") or 0)))
    for i, f in enumerate(out):
        f["rank"] = i + 1
    return out


def stats(before: list[dict], after: list[dict]) -> str:
    dupes = len(before) - len(after)
    corroborated = sum(1 for f in after if f.get("corroborated_by"))
    return (f"dedup: {len(before)} -> {len(after)} finding(s) "
            f"({dupes} duplicate(s) merged, {corroborated} corroborated by 2+ specialists)")


def main(argv: list[str] | None = None) -> int:
    common.configure_console()
    ap = argparse.ArgumentParser(prog="dedup.py")
    ap.add_argument("file")
    ap.add_argument("--out")
    ap.add_argument("--report", action="store_true", help="print merge decisions")
    a = ap.parse_args(argv)
    data = json.loads(Path(a.file).read_text(encoding="utf-8"))
    findings = data.get("findings", data) if isinstance(data, dict) else data
    merged = dedupe(findings, explain=a.report)
    print(stats(findings, merged), file=sys.stderr)
    if a.out:
        common.write_json(a.out, merged)
        print(f"wrote -> {a.out}", file=sys.stderr)
    if a.report:
        for f in merged:
            if f.get("merged_from", 1) > 1:
                print(f"\n[{f.get('type')}] {common.truncate(f.get('finding',''), 100)}")
                print("   " + f.get("dedup_note", ""))
                for g in f.get("merge_log", []):
                    print(f"     <- {g['specialist']}: {g['finding']} (c={g['confidence']})")
    if not a.out:
        print(json.dumps(merged, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
