#!/usr/bin/env python3
"""Setu Cyber — HackerOne / Google VRP style report formatter.

Turns ranked findings JSON into a markdown report a triager can act on. The
structure follows what those programs actually ask for, in their order:

  1. Title that names the bug and the asset      ("SQL injection in /item id")
  2. Severity + CVSS vector (reproducible maths, not vibes)
  3. Summary — one paragraph, no hedging verbs
  4. Affected asset + exact request
  5. Steps to reproduce — numbered, copy-pasteable, minimal
  6. Evidence — the response that proves it
  7. Impact — what an attacker does with it, business terms
  8. Remediation — specific, ordered
  9. References (CWE, OWASP)

Deliberate choices
------------------
* **No severity inflation.** The report prints the CVSS band; it never upgrades
  it in prose ("CRITICAL!!"). Inconsistent prose is the #1 reason reports get
  downgraded on triage.
* **Unconfirmed findings go in their own section.** Mixing "I think this might
  be SQLi" into the confirmed list burns credibility on the confirmed ones.
* **Secrets are masked** before they touch markdown — a report is a document
  that gets emailed around.
* **No finding is silently dropped.** Anything with zero evidence is listed under
  "Needs verification". A silent drop is indistinguishable from a miss.

    python3 report_formatter.py ranked.json --title "Acme Corp" --out report.md
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

CWE_NAMES = {
    "CWE-22": "Path Traversal",
    "CWE-79": "Cross-site Scripting (XSS)",
    "CWE-89": "SQL Injection",
    "CWE-200": "Exposure of Sensitive Information",
    "CWE-319": "Cleartext Transmission of Sensitive Information",
    "CWE-352": "Cross-Site Request Forgery (CSRF)",
    "CWE-693": "Protection Mechanism Failure",
    "CWE-798": "Use of Hard-coded Credentials",
    "CWE-918": "Server-Side Request Forgery (SSRF)",
    "CWE-942": "Permissive Cross-domain Policy",
    "CWE-1104": "Use of Unmaintained Third Party Components",
    "CWE-1336": "Server-Side Template Injection",
}

IMPACT_BY_TYPE = {
    "sqli-injection": "An attacker can read, modify or destroy data in the backend database — "
                      "typically including user credentials and session tokens. For a "
                      "multi-tenant application this is a cross-customer breach.",
    "xss-injection": "An attacker can execute JavaScript in a victim's authenticated session: "
                     "steal session cookies or tokens, perform actions as the victim, and "
                     "rewrite the page (credential phishing).",
    "traversal-injection": "An attacker can read files outside the intended directory — "
                           "configuration, credentials, private keys and source code.",
    "ssti-injection": "Template injection generally escalates to remote code execution on the "
                      "application server, which is full compromise.",
    "secret-exposure": "A live credential is exposed. Anyone who can read this location — or "
                       "anyone who already scraped it — can act as the service. Rotation is "
                       "required even after the leak is closed.",
    "vulnerable-dependency": "A known vulnerability in a bundled library is reachable from the "
                             "application. Exploitability depends on the call path, but the "
                             "fix is a version bump and triage does not require proving RCE.",
    "exposed-endpoint": "An endpoint that should be internal is reachable. Each exposed panel "
                        "or metadata file is a starting point, not a finding by itself.",
    "cors": "A malicious site can read authenticated responses from the victim's browser, "
            "enabling account takeover without touching the victim's password.",
    "transport": "Traffic is interceptable and modifiable on the network path, enabling "
                 "session theft on hostile networks (public Wi-Fi, hotel, ISP).",
    "misconfiguration": "Weakens defence in depth: each missing control on its own is low "
                        "impact, but they chain with an injection to make exploitation easy.",
    "email-spoofing": "Attackers can send mail that appears to come from the domain, "
                      "enabling credential phishing against customers and staff.",
}

REMEDIATION_BY_TYPE = {
    "sqli-injection": ["Use parameterised queries / prepared statements everywhere — never "
                       "string-concatenate input into SQL.",
                       "Add a least-privilege database account for the application role.",
                       "Add a WAF rule as a stop-gap only; fix the query."],
    "xss-injection": ["Context-aware output encoding at the sink (HTML, attribute, JS, URL).",
                      "Deploy a strict Content-Security-Policy with nonces; avoid unsafe-inline.",
                      "Treat all input as untrusted; do not hand-roll sanitisers."],
    "traversal-injection": ["Resolve the requested path and assert it stays inside an allow-listed "
                            "base directory before opening it.",
                            "Prefer opaque ids over filenames in URLs."],
    "ssti-injection": ["Never build templates from user input. Pass user data as template "
                       "variables only.",
                       "Use a sandboxed template environment with a minimal context."],
    "secret-exposure": ["Rotate the credential now; assume it is compromised.",
                        "Remove from source and history; load from a secrets manager.",
                        "Add a pre-commit secret scanner to prevent recurrence."],
    "vulnerable-dependency": ["Upgrade to the fixed version listed in the finding.",
                              "Pin versions and add automated dependency scanning to CI."],
    "exposed-endpoint": ["Remove or restrict the endpoint at the network layer (deny by default).",
                         "Require authentication and log access."],
    "cors": ["Echo only an explicit allow-list of origins; never combine a wildcard with "
             "credentials.",
             "Validate Origin server-side on state-changing requests."],
    "transport": ["Redirect all HTTP to HTTPS with HSTS (includeSubDomains, preload once stable).",
                  "Set a secure, HttpOnly, SameSite cookie policy."],
    "misconfiguration": ["Add the missing security headers at the edge (CDN/WAF), not per-app.",
                         "Suppress version banners."],
    "email-spoofing": ["Publish SPF, DKIM and a DMARC policy; move DMARC toward p=reject.",
                       "Monitor DMARC aggregate reports."],
}

_SECRET_PATTERNS = [
    (re.compile(r"\b(AKIA[0-9A-Z]{16})\b"), r"AKIA****************"),
    (re.compile(r"(?i)\b(api[_-]?key|token|secret|password)(\s*[:=]\s*)['\"]?([A-Za-z0-9_\-]{8,})['\"]?"),
     r"\1\2<REDACTED>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
     "<REDACTED PRIVATE KEY>"),
]


KIND_LABEL = {"sqli": "SQLi", "xss": "XSS", "traversal": "Path traversal",
              "ssti": "SSTI", "csrf": "CSRF", "ssrf": "SSRF"}


def _titlecase_kind(text: str) -> str:
    """'Possible SQLI in parameter' -> 'Possible SQLi in parameter'.

    Only the vuln acronym is rewritten. Naive .title() would mangle product
    names ('FixtureApache' -> 'Fixtureapache') in the same string.
    """
    for low, good in KIND_LABEL.items():
        text = re.sub(rf"\b{low.upper()}\b", good, text)
    return text


def redact(text: str) -> str:
    out = str(text or "")
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _cvss_line(f: dict) -> str:
    if f.get("cvss_base") is None:
        return "_informational — no CVSS assigned_"
    vec = f.get("cvss_vector") or "n/a"
    return f"**{float(f['cvss_base']):.1f}** `{vec}`"


def _steps(f: dict) -> list[str]:
    ev = f.get("evidence") if isinstance(f.get("evidence"), dict) else {}
    # When dedup merged several specialists, evidence is namespaced per specialist.
    if ev and all(isinstance(v, dict) for v in ev.values()) and "specialist" not in ev:
        flat: dict = {}
        for sub in ev.values():
            flat.update(sub)
        ev = flat
    url = ev.get("url") or ev.get("target") or f.get("target") or "<target>"
    param = ev.get("parameter")
    payload = ev.get("payload")
    steps = [f"Open `{url}`."]
    if param and payload:
        steps.append(f"Set parameter `{param}` to:\n\n   ```\n   {payload}\n   ```")
    elif payload:
        steps.append(f"Send the payload:\n\n   ```\n   {payload}\n   ```")
    if ev.get("http_status") or ev.get("status"):
        steps.append(f"Observe the response (HTTP {ev.get('http_status') or ev.get('status')}).")
    if ev.get("error_signals"):
        steps.append("Response matches error signature(s): "
                     + ", ".join(f"`{s}`" for s in ev["error_signals"]) + ".")
    if ev.get("response_snippet"):
        steps.append("Response contains:\n\n   ```\n   "
                     + redact(str(ev["response_snippet"])[:600]) + "\n   ```")
    if len(steps) == 1:
        steps.append(f"Reach the affected path and observe: {f.get('finding','')}")
    return steps


def render_finding(f: dict, index: int) -> str:
    ftype = str(f.get("type", "generic"))
    title = _titlecase_kind(common.truncate(str(f.get("finding", "Untitled finding")), 140))
    target = f.get("target") or "<unspecified>"
    cwe = f.get("cwe")
    lines = [
        f"### {index}. {title}",
        "",
        f"- **Severity:** {str(f.get('severity', 'info')).upper()} "
        f"(confidence {float(f.get('confidence') or 0):.2f})",
        f"- **CVSS:** {_cvss_line(f)}",
        f"- **Affected asset:** `{target}`",
    ]
    if cwe:
        lines.append(f"- **Weakness:** {cwe} — {CWE_NAMES.get(cwe, 'see reference')}")
    if f.get("corroborated_by"):
        lines.append(f"- **Corroborated by:** {', '.join(f['corroborated_by'])}")
    if f.get("priority_score") is not None:
        lines.append(f"- **Fix priority:** {f['priority_score']} "
                     f"(CVSS × confidence)")
    lines += ["", "**Summary.** " + redact(str(f.get("finding", ""))), ""]

    lines.append("**Steps to reproduce.**")
    for i, s in enumerate(_steps(f), 1):
        lines.append(f"{i}. {s}")
    lines.append("")

    ev = f.get("evidence")
    if ev:
        lines.append("**Evidence.**")
        lines.append("")
        lines.append("```json")
        lines.append(redact(json.dumps(ev, indent=2, ensure_ascii=False))[:2000])
        lines.append("```")
        lines.append("")

    impact = IMPACT_BY_TYPE.get(ftype)
    if impact:
        lines += ["**Impact.** " + impact, ""]

    fixes = REMEDIATION_BY_TYPE.get(ftype)
    if fixes:
        lines.append("**Remediation.**")
        for i, r in enumerate(fixes, 1):
            lines.append(f"{i}. {r}")
        lines.append("")

    refs = []
    if cwe:
        refs.append(f"[{cwe}](https://cwe.mitre.org/data/definitions/{cwe.split('-')[1]}.html)")
    refs.append("[OWASP Top 10](https://owasp.org/www-project-top-ten/)")
    if f.get("cvss_vector"):
        refs.append(f"[CVSS v3.1 calculator](https://www.first.org/cvss/calculator/3.1"
                    f"#{f['cvss_vector'].replace('CVSS:3.1/', '')})")
    lines.append("**References.** " + " · ".join(refs))
    lines.append("")
    return "\n".join(lines)


def to_markdown(findings: list[dict], title: str = "Security Assessment",
                scope: str | None = None, author: str = "Setu Cyber") -> str:
    note_types = {"testing-note", "coverage-note"}
    notes = [f for f in findings if f.get("type") in note_types]
    security_findings = [f for f in findings if f.get("type") not in note_types]
    confirmed = [f for f in security_findings
                 if not f.get("needs_manual_review") and float(f.get("confidence") or 0) >= 0.45]
    unverified = [f for f in security_findings if f not in confirmed]

    counts: dict[str, int] = {}
    for f in confirmed:
        counts[str(f.get("severity", "info")).lower()] = \
            counts.get(str(f.get("severity", "info")).lower(), 0) + 1

    out = [
        f"# {title}",
        "",
        f"**Date:** {date.today().isoformat()}  ",
        f"**Prepared by:** {author}  ",
        f"**Scope:** {scope or 'as agreed with the target owner'}  ",
        f"**Testing basis:** authorized testing only; findings are observations, "
        f"not guarantees.",
        "",
        "## Summary",
        "",
        f"{len(confirmed)} confirmed finding(s) across {len({f.get('target') for f in confirmed})} "
        f"asset(s). Severity mix: "
        + ", ".join(f"{k} {v}" for k, v in sorted(
            counts.items(), key=lambda kv: ["critical", "high", "medium", "low", "info"]
            .index(kv[0]) if kv[0] in ["critical", "high", "medium", "low", "info"] else 9))
        + f". {len(unverified)} item(s) need verification before they can be reported.",
        "",
    ]
    if confirmed:
        out += ["| # | Severity | CVSS | Conf | Finding | Asset |",
                "|---|---|---|---|---|---|"]
        for i, f in enumerate(confirmed, 1):
            cvss = f"{float(f['cvss_base']):.1f}" if f.get("cvss_base") else "—"
            out.append(f"| {i} | {str(f.get('severity','info')).upper()} | {cvss} | "
                       f"{float(f.get('confidence') or 0):.2f} | "
                       f"{common.truncate(str(f.get('finding','')), 80)} | "
                       f"`{common.truncate(str(f.get('target','')), 40)}` |")
        out.append("")
        out += ["## Findings", ""]
        for i, f in enumerate(confirmed, 1):
            out.append(render_finding(f, i))

    if unverified:
        out += ["## Needs verification (not yet reportable)", "",
                "These were produced by a specialist but lack enough evidence or confidence "
                "to stand on their own. Confirm manually before including them; do not "
                "submit them as-is.", ""]
        for f in unverified:
            reason = f.get("review_reason") or "low confidence"
            out.append(f"- **{common.truncate(str(f.get('finding','')), 120)}** "
                       f"({f.get('specialist','?')}, c={float(f.get('confidence') or 0):.2f}) "
                       f"— _{reason}_")
        out.append("")

    if notes:
        out += ["## Coverage notes (not security findings)", "",
                "These notes identify checks that were incomplete or informational; they "
                "must not be interpreted as evidence that the target is secure or insecure.", ""]
        for f in notes:
            evidence = f.get("evidence") if isinstance(f.get("evidence"), dict) else {}
            details = []
            if evidence.get("url"):
                details.append(str(evidence["url"]))
            if evidence.get("error"):
                details.append(str(evidence["error"]))
            suffix = f" — {'; '.join(details)}" if details else ""
            out.append(f"- {common.truncate(str(f.get('finding','')), 200)}{suffix}")
        out.append("")

    out += ["---", "",
            "_Findings were produced under authorized testing. Every item should be "
            "independently confirmed before submission to any bug-bounty program. "
            "Secrets have been redacted in this document; rotate anything that leaked._", ""]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    common.configure_console()
    ap = argparse.ArgumentParser(prog="report_formatter.py")
    ap.add_argument("file", help="ranked findings JSON")
    ap.add_argument("--title", default="Security Assessment")
    ap.add_argument("--scope", default=None)
    ap.add_argument("--author", default="Setu Cyber")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    data = json.loads(Path(a.file).read_text(encoding="utf-8"))
    findings = data.get("findings", data) if isinstance(data, dict) else data
    md = to_markdown(findings, a.title, a.scope, a.author)
    if a.out:
        Path(a.out).write_text(md, encoding="utf-8")
        print(f"wrote report -> {a.out} ({len(md.splitlines())} lines)", file=sys.stderr)
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
