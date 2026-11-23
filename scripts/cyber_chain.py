#!/usr/bin/env python3
"""Setu Cyber — end-to-end chain runner.

This is what `setu-cyber` actually executes. It models the pack's real
dataflow, which is the point of building one chain fully before the other 19
specialists:

    recon ──► specialist(s) ──► severity-scorer ──► dedup ──► report
    (findings JSON)            (CVSS + rank)        (merge)   (markdown)

Every stage is a separate process with a JSON file on disk between them. That
sounds wasteful and is deliberate: any stage can be re-run alone, a failure is
contained to one step, and the intermediate artifacts are exactly what a human
reviewer needs to audit a claim. No stage holds state in memory between runs.

    python3 cyber_chain.py --target https://lab.internal/item?id=1 \
        --i-have-authorization --allow-private --out-dir runs/run-001

    # passive recon only, no requests to the target
    python3 cyber_chain.py --target example.com --passive --out-dir runs/passive

Order matters: dedup runs *after* scoring so a merged finding keeps the max
CVSS, and the report runs last so it only ever sees ranked, deduplicated truth.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common            # noqa: E402
import cyber_recon as cr  # noqa: E402
import cyber_scan         # noqa: E402
import dedup              # noqa: E402
import memory             # noqa: E402
import report_formatter   # noqa: E402
import severity_scorer    # noqa: E402


def _stage(name: str, fn, *a, **kw):
    print(f"\n=== {name} ===", file=sys.stderr)
    try:
        return fn(*a, **kw), None
    except Exception as e:  # noqa: BLE001 — one stage failing must not kill the chain
        print(f"  stage '{name}' failed: {e}", file=sys.stderr)
        return [], str(e)


def run_chain(target: str, *, authorized: bool = False, passive: bool = False,
              allow_private: bool = False, out_dir: str = "runs/latest",
              title: str | None = None, log_memory: bool = True) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    findings: list[dict] = []
    errors: dict[str, str] = {}
    is_url = "://" in target
    host = urllib.parse.urlsplit(target).hostname if is_url else target

    # ---- 1. recon -----------------------------------------------------------
    cr.assert_authorized(authorized, passive=passive)
    if not is_url:
        cr.assert_public(host or "", allow_private)
        got, err = _stage("recon: subdomain-enum", cr.subdomain_enum,
                          host, None, passive, allow_private)
        findings += got
        if err:
            errors["subdomain-enum"] = err
        got, err = _stage("recon: osint", cr.osint, host)
        findings += got
        if err:
            errors["osint"] = err
    else:
        cr.assert_public(host or "", allow_private)
        got, err = _stage("recon: fingerprint", cr.tech_fingerprint, target, allow_private)
        findings += got
        if err:
            errors["fingerprint"] = err
        got, err = _stage("recon: endpoints", cr.endpoint_discovery, target,
                          None, allow_private)
        findings += got
        if err:
            errors["endpoints"] = err
        got, err = _stage("specialist: cloud-misconfig (headers)", cr.header_check,
                          target, allow_private)
        findings += got
        if err:
            errors["header-check"] = err

    # ---- 2. specialist: injection (only if authorized and a parameter exists) --
    if is_url and (authorized or passive):
        got, err = _stage("specialist: injection", cyber_scan.inject_params,
                          target, None, allow_private)
        findings += got
        if err:
            errors["injection"] = err

    common.write_json(out / "findings.raw.json", findings)
    print(f"\nraw findings: {len(findings)}", file=sys.stderr)

    # ---- 3. severity scoring ------------------------------------------------
    ranked, err = _stage("triage: severity-scorer", severity_scorer.rank, findings)
    if err:
        errors["severity_scorer"] = err
    common.write_json(out / "findings.ranked.json", ranked)

    # ---- 4. dedup -----------------------------------------------------------
    deduped, err = _stage("triage: dedup", dedup.dedupe, ranked)
    if err:
        errors["dedup"] = err
        deduped = ranked
    common.write_json(out / "findings.deduped.json", deduped)

    # ---- 5. report ----------------------------------------------------------
    report_title = title or f"Security Assessment — {host}"
    md, err = _stage("report: formatter", report_formatter.to_markdown,
                     deduped, report_title,
                     f"{target} (authorized testing)" if authorized else
                     f"{target} (passive recon only)")
    if err:
        errors["report"] = err
        md = f"# {report_title}\n\nReport generation failed: {err}\n"
    (out / "report.md").write_text(md, encoding="utf-8")

    # ---- 6. memory: the self-learning hook ----------------------------------
    summary = (f"{len(deduped)} finding(s) after dedup from {len(findings)} raw; "
               f"severities: " + ", ".join(
                   f"{s}={sum(1 for f in deduped if f.get('severity') == s)}"
                   for s in ("critical", "high", "medium", "low", "info")))
    if log_memory:
        try:
            memory.log_feedback(
                pack="cyber",
                input_summary=f"cyber chain on {target} "
                              f"({'passive' if passive else 'authorized active'})",
                output_summary=summary,
                was_helpful=bool(deduped) and not errors,
                meta={"run_dir": str(out), "stages_failed": sorted(errors)})
        except Exception as e:  # noqa: BLE001
            print(f"warning: memory log failed: {e}", file=sys.stderr)

    result = {
        "target": target,
        "mode": "passive" if passive else ("authorized" if authorized else "refused"),
        "out_dir": str(out),
        "raw_findings": len(findings),
        "ranked_findings": len(ranked),
        "final_findings": len(deduped),
        "by_severity": {s: sum(1 for f in deduped if f.get("severity") == s)
                        for s in ("critical", "high", "medium", "low", "info")},
        "stage_errors": errors,
        "report": str(out / "report.md"),
        "artifacts": sorted(p.name for p in out.glob("*")),
    }
    common.write_json(out / "run.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="cyber_chain.py",
                                 description="Setu Cyber end-to-end chain")
    ap.add_argument("--target", required=True, help="URL or bare domain")
    ap.add_argument("--i-have-authorization", action="store_true")
    ap.add_argument("--passive", action="store_true")
    ap.add_argument("--allow-private", action="store_true")
    ap.add_argument("--out-dir", default="runs/latest")
    ap.add_argument("--title", default=None)
    ap.add_argument("--no-memory", action="store_true")
    a = ap.parse_args(argv)
    try:
        res = run_chain(a.target, authorized=a.i_have_authorization, passive=a.passive,
                        allow_private=a.allow_private, out_dir=a.out_dir,
                        title=a.title, log_memory=not a.no_memory)
    except cr.ScopeError as e:
        print(f"SCOPE REFUSED: {e}", file=sys.stderr)
        return 3
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
