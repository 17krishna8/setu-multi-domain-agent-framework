#!/usr/bin/env python3
"""Setu CLI — one entry point that wires memory + model wrapper into every pack.

The packs can each be called directly (`cyber_chain.py`, `finance.py`, …). This
CLI is the seam that makes them one framework rather than five scripts: it does
the two things every pack needs and no pack should implement twice.

    1. ``few-shot``   — before a pack runs, fetch its most relevant past wins and
                        format them as context to inject into the prompt.
    2. ``remember``   — after a pack runs, log the outcome so the next run is
                        better.

Commands
--------
    python3 setu.py doctor                       # environment + backend report
    python3 setu.py packs                        # what each pack is for
    python3 setu.py context --pack cyber --request "enumerate subs for app.example.com"
    python3 setu.py remember --pack cyber --input "..." --output "..." --helpful true
    python3 setu.py stats
    python3 setu.py route "is this EMI offer a scam?"
    python3 setu.py selftest                     # run every gate

``doctor`` and ``selftest`` exist because a framework with five packs and a memory
layer fails in quiet ways. ``selftest`` asserts the safety gates still hold, so a
refactor cannot silently remove one.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _resolve_layout() -> dict:
    """Find the framework root whatever layout we were installed into.

    Three layouts exist in practice, and guessing one is how a health check
    reports "0 agents" on a perfectly good install:

      source tree      setu/scripts/…             agents/ and skills/ beside scripts/
      --target claude  ~/.claude/setu/scripts/…   agents/ and skills/ two levels up
      --target cursor  .cursor/setu/scripts/…     agents become *.mdc in .cursor/rules/
      --target codex   ./setu/scripts/…           agents in ./setu/agents/, skills in ./setu/skills/

    Important: stop at the FIRST ancestor that holds agents/ or skills/. Walking
    further (up past the install root) finds the *source tree* on a developer's
    machine and reports 34 agents for an install that has 23 — a health check that
    counts files it is not responsible for is worse than no health check.
    """
    candidates = [HERE.parent, HERE.parent.parent, HERE.parent.parent.parent]
    root = None
    agents_dirs: list[Path] = []
    skills_dirs: list[Path] = []
    for c in candidates:
        found_agents = [c / "agents", c / "setu" / "agents"]
        found_skills = [c / "skills", c / "setu" / "skills"]
        hit_a = [d for d in found_agents if d.is_dir()]
        hit_s = [d for d in found_skills if d.is_dir()]
        hit_m = (c / "rules").is_dir() and any((c / "rules").glob("*.mdc"))
        if hit_a or hit_s or hit_m:
            root = c
            agents_dirs, skills_dirs = hit_a, hit_s
            break
    if root is None:
        root = HERE.parent
    mdc_dirs = [d for d in (root / "rules", root / ".cursor" / "rules")
                if d.is_dir() and any(d.glob("*.mdc"))]
    return {"root": root, "agents_dirs": agents_dirs, "skills_dirs": skills_dirs,
            "mdc_dirs": mdc_dirs, "candidates": candidates}


LAYOUT = _resolve_layout()
ROOT = LAYOUT["root"]
sys.path.insert(0, str(HERE))

import common            # noqa: E402
import memory            # noqa: E402
import model_wrapper     # noqa: E402

PACKS = {
    "cyber": {
        "skill": "setu-cyber",
        "agents": 20,
        "purpose": "authorized security testing: recon, vulnerabilities, CVSS triage, "
                   "bug-bounty reports",
        "entry": "scripts/cyber_chain.py",
        "gate": "authorization required for active testing; passive recon always allowed",
    },
    "webdev": {
        "skill": "setu-webdev",
        "agents": 5,
        "purpose": "build sites/components with a design critic; diagnose, fix and "
                   "verify code bugs",
        "entry": "scripts/design_check.py + scripts/webdev_fix.py",
        "gate": "no fix is accepted without a green test run",
    },
    "agri": {
        "skill": "setu-agri",
        "agents": 3,
        "purpose": "crop disease/pest classification, treatment advice, regional-language "
                   "advisory",
        "entry": "scripts/agri.py",
        "gate": "no remedy below 0.6 classification confidence",
    },
    "admin": {
        "skill": "setu-admin",
        "agents": 3,
        "purpose": "RTI applications, grievance letters, consumer complaints; verbatim "
                   "legal citation checks",
        "entry": "scripts/admin_docs.py",
        "gate": "a named human must confirm before anything is final",
    },
    "finance": {
        "skill": "setu-finance",
        "agents": 3,
        "purpose": "expense categorisation, scam screening, deterministic EMI maths",
        "entry": "scripts/finance.py",
        "gate": "no model-generated numbers; disclaimer always attached",
    },
}

# Cheap keyword router — the same intent/object logic the orchestrator skill uses,
# in code, so `setu.py route` can be tested and so a wrong route is visible.
ROUTE_SIGNALS = {
    "cyber": [("penetration", 3), ("pentest", 3), ("vulnerab", 3), ("exploit", 2),
              ("bug bounty", 3), ("cve", 2), ("xss", 3), ("sqli", 3), ("injection", 2),
              ("recon", 2), ("subdomain", 3), ("attack surface", 3), ("security audit", 3)],
    "webdev": [("build a", 2), ("create a site", 3), ("landing page", 3), ("component", 2),
               ("stylesheet", 2), ("css", 2), ("html", 2), ("react", 2), ("redesign", 3),
               ("fix", 2), ("broken", 2), ("traceback", 3), ("failing test", 3),
               ("error", 1), ("crash", 2)],
    "agri": [("crop", 3), ("leaf", 3), ("plant", 2), ("disease", 2), ("pest", 3),
             ("pesticide", 3), ("fungus", 3), ("blight", 3), ("aphid", 3), ("harvest", 2),
             ("farmer", 2), ("soil", 2)],
    "admin": [("rti", 3), ("right to information", 3), ("grievance", 3), ("complaint", 2),
              ("consumer", 2), ("legal", 2), ("statute", 3), ("act 20", 1), ("official letter", 3),
              ("affidavit", 3)],
    "finance": [("emi", 3), ("loan", 3), ("interest rate", 2), ("scam", 3), ("fraud", 3),
                ("expense", 2), ("budget", 2), ("transaction", 2), ("categor", 2),
                ("instalment", 3), ("credit card", 2), ("otp", 1)],
}
# Things that override the keyword score, because the object is decisive.
OVERRIDES = [
    (("is this", "scam"), "finance"),
    (("loan", "offer"), "finance"),
    (("fix", "xss"), "cyber"),
    (("audit", "site"), "cyber"),
]


def route(request: str) -> dict:
    low = " " + (request or "").lower() + " "
    scores = {p: 0 for p in PACKS}
    hits = {p: [] for p in PACKS}
    for pack, signals in ROUTE_SIGNALS.items():
        for phrase, weight in signals:
            if phrase in low:
                scores[pack] += weight
                hits[pack].append(phrase)
    override = None
    for phrases, pack in OVERRIDES:
        if all(p in low for p in phrases):
            override = pack
            break
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top, top_score = ranked[0]
    second, second_score = ranked[1]
    chosen = override or (top if top_score > 0 else None)
    if override:
        reason = f"override rule: {override} matches the object of the request"
    elif chosen is None:
        reason = "no pack matched — ask the user which one they mean"
    else:
        reason = f"keywords: {', '.join(hits[top][:4])} (score {top_score})"
    return {
        "request": request,
        "route": chosen,
        "confidence": "override" if override else
                      ("high" if top_score >= 3 and top_score - second_score >= 2 else
                       "medium" if top_score > 0 else "none"),
        "reason": reason,
        "runner_up": second if second_score > 0 else None,
        "all_scores": scores,
        "ambiguous": top_score > 0 and second_score > 0 and abs(top_score - second_score) < 2,
    }


# --------------------------------------------------------------------------- #
# memory wiring
# --------------------------------------------------------------------------- #
def build_context(pack: str, request: str | None = None, n: int = 3) -> dict:
    """The self-learning seam: past wins, formatted for prompt injection."""
    shots = memory.get_few_shot_examples(pack, n=n, query=request)
    score = memory.pack_score(pack)
    blocks = []
    for i, s in enumerate(shots, 1):
        blocks.append(f"Example {i} (previously rated helpful):\n"
                      f"  REQUEST: {s['input_summary']}\n"
                      f"  GOOD OUTPUT: {s['output_summary']}")
    context = "\n\n".join(blocks) if blocks else ""
    return {
        "pack": pack,
        "examples_found": len(shots),
        "context_block": context,
        "injection_prompt": (
            f"You are running Setu's {pack} pack. These are outputs that were rated "
            f"helpful before; use them for tone, depth and structure, not as facts.\n\n"
            f"{context}") if context else
        f"You are running Setu's {pack} pack. No prior examples are stored yet.",
        "pack_stats": score,
        "caution": ("this pack's helpfulness rate is below 0.5 over 3+ samples; route "
                    "more carefully or ask the user to correct the output"
                    if score.get("warn") else None),
        "memory_path": str(memory.MEMORY_PATH),
    }


def remember(pack: str, input_summary: str, output_summary: str, helpful: bool,
             meta: str | None = None) -> dict:
    """Always logs `pack` — never infers it, so stats stay attributable."""
    if pack not in PACKS:
        raise ValueError(f"unknown pack '{pack}'; have: {', '.join(PACKS)}")
    extra = None
    if meta:
        try:
            extra = json.loads(meta)
        except json.JSONDecodeError:
            extra = {"note": meta}
    entry = memory.log_feedback(pack, input_summary, output_summary, helpful, extra)
    return {"logged": entry["id"], "pack": pack, "helpful": entry["was_helpful"],
            "pack_stats": memory.pack_score(pack), "memory_path": str(memory.MEMORY_PATH)}


# --------------------------------------------------------------------------- #
# doctor + selftest
# --------------------------------------------------------------------------- #
def doctor() -> dict:
    checks = {}
    checks["python"] = sys.version.split()[0]
    checks["backends"] = model_wrapper.available_backends()
    checks["SETU_MODEL"] = os.environ.get("SETU_MODEL", "(unset → claude)")
    checks["resolved_model"] = model_wrapper.resolve_model()[0]
    checks["memory_path"] = str(memory.MEMORY_PATH)
    checks["memory_writable"] = _writable(memory.MEMORY_PATH.parent)
    # Rule files may sit beside the scripts (source tree) or one level up (the
    # cursor/codex layouts), so look in both instead of guessing.
    def _rule(name: str) -> bool:
        return any(c.exists() for c in (HERE / "rules" / name,
                                        HERE.parent / "rules" / name,
                                        HERE.parent / "scripts" / "rules" / name))
    checks["rules"] = {n: _rule(n) for n in
                       ("patterns.json", "dependencies.json", "legal_corpus.json")}
    # Only the rules a *selected* pack needs must be present. A missing rule file
    # for a pack that was never installed is correct, not a failure.
    PACK_RULES = {"cyber": ["patterns.json", "dependencies.json"],
                  "finance": ["patterns.json"],
                  "admin": ["legal_corpus.json"]}
    required_rules: list[str] = []
    for pack, rules in PACK_RULES.items():
        marker = {"cyber": "cyber_scan.py", "finance": "finance.py",
                  "admin": "admin_docs.py"}[pack]
        if (HERE / marker).exists() or (HERE.parent / "scripts" / marker).exists():
            required_rules += [r for r in rules if r not in required_rules]
    agent_files: set[Path] = set()
    for d in LAYOUT["agents_dirs"]:
        agent_files |= set(d.glob("*.md"))
    for d in LAYOUT["mdc_dirs"]:
        agent_files |= set(d.glob("*.mdc"))
    skill_dirs: list[Path] = []
    for d in LAYOUT["skills_dirs"]:
        skill_dirs += list(d.glob("*/SKILL.md"))
    for d in LAYOUT["mdc_dirs"]:
        skill_dirs += list(d.glob("setu-*.mdc"))
    checks["agents"] = len(agent_files)
    checks["skills"] = len(skill_dirs)
    checks["layout"] = {"root": str(ROOT),
                        "agents_dirs": [str(d) for d in LAYOUT["agents_dirs"]],
                        "skills_dirs": [str(d) for d in LAYOUT["skills_dirs"]]}
    # Only complain about packs the user selected. A `--profile minimal` install
    # legitimately lacks webdev/admin/agri, and a health check that calls that a
    # failure is a health check people learn to ignore.
    present = {p.parent.name.replace("setu-", "") if p.name == "SKILL.md"
               else p.stem.replace("setu-", "") for p in skill_dirs}
    skill_names = {p.parent.name if p.name == "SKILL.md" else p.stem for p in skill_dirs}
    missing = [a["skill"] for name, a in PACKS.items()
               if name in present and a["skill"] not in skill_names]
    checks["installed_packs"] = sorted(present)
    checks["not_installed"] = sorted(set(PACKS) - present)
    checks["missing_skills"] = missing
    rules_ok = all(checks["rules"].get(r, False) for r in required_rules)
    checks["required_rules"] = required_rules
    checks["ok"] = (not missing and checks["memory_writable"] and rules_ok)
    return checks


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def _writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".setu-write-probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def selftest() -> dict:
    """Assert every pack's safety gate still holds. Fails loudly."""
    results: list[dict] = []

    def check(name: str, fn, needs: str | None = None) -> None:
        # A gate for a pack that is not installed is SKIPPED, not failed. Saying
        # "FAILED" for something that was deliberately not installed teaches
        # people to ignore the whole health check.
        if needs and not _module_available(needs):
            results.append({"check": name, "pass": True, "skipped": True,
                            "detail": f"pack not installed (module '{needs}' absent)"})
            return
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"raised {type(e).__name__}: {e}"
        results.append({"check": name, "pass": bool(ok), "skipped": False,
                        "detail": detail})

    # cyber: authorization gate
    def cyber_auth():
        import cyber_recon
        try:
            cyber_recon.assert_authorized(False, passive=False)
            return False, "active testing was allowed without authorization"
        except cyber_recon.ScopeError:
            return True, "refused active testing without the flag"
    check("cyber: authorization gate", cyber_auth)

    # cyber: SSRF guard
    def cyber_ssrf():
        import cyber_recon
        if cyber_recon.is_private("169.254.169.254") and cyber_recon.is_private("127.0.0.1"):
            return True, "metadata and loopback addresses blocked"
        return False, "private-range detection is not working"
    check("cyber: SSRF guard", cyber_ssrf)

    # cyber: CVSS implementation
    def cyber_cvss():
        import severity_scorer
        got = severity_scorer.base_score(
            "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H")["base_score"]
        return (got == 9.8), f"9.8 vector scored {got}"
    check("cyber: CVSS v3.1 base score", cyber_cvss)

    # webdev: critic rejects a generic page (built inline so the test is hermetic)
    def webdev_critic():
        import design_check, tempfile
        generic = ('<html><head><style>body{font-family:Inter,system-ui}'
                   '.hero{background:linear-gradient(135deg,#667eea,#764ba2);text-align:center}'
                   '</style></head><body><header class="hero"><h1>Your headline here</h1>'
                   '<button>🚀 Go</button></header><div class="card">Feature One</div>'
                   '<div class="card">Feature Two</div><div class="card">Feature Three</div>'
                   '<footer>Lorem ipsum</footer></body></html>')
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "x.html"
            f.write_text(generic, encoding="utf-8")
            res = design_check.run_checks([f])
        return (res["verdict"] == "REJECT"), f"verdict={res['verdict']} blocking={res['blocking']}"
    check("webdev: design critic rejects generic page", webdev_critic,
          needs="design_check")

    # webdev: verify refuses to accept without tests
    def webdev_verify():
        import webdev_fix, tempfile
        with tempfile.TemporaryDirectory() as d:
            res = webdev_fix.verify(d, test_cmd=None)
        return (res["verified"] is False), res.get("reason", "")[:80]
    check("webdev: no green suite means no acceptance", webdev_verify,
          needs="webdev_fix")

    # agri: confidence gate
    def agri_gate():
        import agri
        low = agri.treat({"label": "Early blight", "crop": "tomato", "confidence": 0.42,
                          "treatment_allowed": True})
        high = agri.treat({"label": "Early blight", "crop": "tomato", "confidence": 0.9,
                           "treatment_allowed": True})
        ok = (low["status"] == "uncertain" and low["remedies"] == []
              and high["status"] == "ok" and len(high["remedies"]) > 0)
        return ok, f"0.42->{low['status']}({len(low['remedies'])} remedies), " \
                   f"0.90->{high['status']}({len(high['remedies'])} remedies)"
    check("agri: 0.6 confidence gate", agri_gate, needs="agri")

    # admin: never auto-finalize
    def admin_gate():
        import admin_docs
        res = admin_docs.admin_finalize("some draft text", None, False)
        return (not res["finalized"]), f"refused with: {res['reasons'][0]}"
    check("admin: human confirmation required", admin_gate, needs="admin_docs")

    # finance: deterministic maths + math guard
    def finance_math():
        import finance
        emi = finance.emi(500_000, 9.2, 60)["emi"]
        sched = finance.emi(500_000, 9.2, 60, schedule=True)
        ok = emi == "10427.78" and sched["reconciled"] is True
        return ok, f"emi={emi}, reconciled={sched['reconciled']}"
    check("finance: deterministic EMI + reconciled schedule", finance_math,
          needs="finance")

    def finance_guard():
        try:
            model_wrapper.call_model("calculate the EMI for 500000 at 9 percent")
            return False, "model was allowed to compute a number"
        except ValueError:
            return True, "math-shaped prompts refused without allow_math"
    check("finance: model cannot compute numbers", finance_guard)

    # memory round-trip
    def memory_roundtrip():
        import tempfile
        old = memory.MEMORY_PATH
        with tempfile.TemporaryDirectory() as d:
            memory.MEMORY_PATH = Path(d) / "m.json"
            try:
                memory.log_feedback("selftest", "probe in", "probe out", True)
                shots = memory.get_few_shot_examples("selftest", 1, "probe in")
                return (len(shots) == 1 and shots[0]["input_summary"] == "probe in"), \
                    "log → few-shot retrieval works"
            finally:
                memory.MEMORY_PATH = old
    check("memory: log and retrieve", memory_roundtrip)

    passed = sum(1 for r in results if r["pass"] and not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))
    return {"passed": passed, "skipped": skipped,
            "total": len(results) - skipped,
            "ok": passed == len(results) - skipped, "results": results}


def main(argv: list[str] | None = None) -> int:
    # Windows consoles often use cp1252, which cannot encode arrows or regional
    # language text in diagnostics. Preserve the message with \u escapes instead
    # of crashing an otherwise successful command.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    ap = argparse.ArgumentParser(prog="setu.py", description="Setu framework CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor")
    sub.add_parser("packs")
    sub.add_parser("stats")
    sub.add_parser("selftest")

    c = sub.add_parser("context")
    c.add_argument("--pack", required=True)
    c.add_argument("--request", default=None)
    c.add_argument("--n", type=int, default=3)

    r = sub.add_parser("remember")
    r.add_argument("--pack", required=True)
    r.add_argument("--input", required=True, dest="inp")
    r.add_argument("--output", required=True, dest="out")
    r.add_argument("--helpful", default="true")
    r.add_argument("--meta", default=None)

    rt = sub.add_parser("route")
    rt.add_argument("request")

    a = ap.parse_args(argv)

    if a.cmd == "doctor":
        res = doctor()
        print(json.dumps(res, indent=2))
        return 0 if res["ok"] else 1
    if a.cmd == "packs":
        for name, meta in PACKS.items():
            print(f"{name:8s} {meta['agents']:>2} agents  ->  {meta['skill']}")
            print(f"         {meta['purpose']}")
            print(f"         gate: {meta['gate']}")
        return 0
    if a.cmd == "stats":
        print(json.dumps(memory.usage_stats(), indent=2))
        return 0
    if a.cmd == "selftest":
        res = selftest()
        for r_ in res["results"]:
            tag = "SKIP" if r_.get("skipped") else ("PASS" if r_["pass"] else "FAIL")
            print(f"  [{tag}] {r_['check']}")
            print(f"         {r_['detail']}")
        extra = f", {res['skipped']} skipped" if res["skipped"] else ""
        print(f"\n{res['passed']}/{res['total']} gates hold{extra}")
        return 0 if res["ok"] else 1
    if a.cmd == "context":
        print(json.dumps(build_context(a.pack, a.request, a.n), indent=2))
        return 0
    if a.cmd == "remember":
        try:
            res = remember(a.pack, a.inp, a.out,
                           str(a.helpful).lower() in {"1", "true", "yes"}, a.meta)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(json.dumps(res, indent=2))
        return 0
    if a.cmd == "route":
        print(json.dumps(route(a.request), indent=2))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
