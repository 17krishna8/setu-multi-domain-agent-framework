#!/usr/bin/env python3
"""Setu Cyber — vulnerability specialists + CLI.

``cyber_recon.py`` owns everything that touches the network and the two safety
gates (authorization, SSRF). This file owns the *specialist* logic that turns a
response into a finding, and the CLI that the sub-agents call.

The contract every specialist honours::

    {"finding": str, "severity": str, "confidence": float, "evidence": {...}}

``common.normalize_findings`` enforces it afterwards: a finding with no evidence
is knocked to confidence 0.0 and tagged ``needs_manual_review``, because an
un-evidenced claim is the single most expensive thing a security report can
contain.

Subcommand -> sub-agent
    subdomain-enum  cyber-subdomain-enum      fingerprint  cyber-tech-fingerprint
    endpoints       cyber-endpoint-discovery   osint        cyber-osint
    header-check    cyber-cloud-misconfig      injection    cyber-injection
    secrets         cyber-crypto-weakness      deps         cyber-dependency-scan
    payloads        cyber-xss-csrf / -ssrf / -access-control corpus
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common            # noqa: E402
import cyber_recon as cr  # noqa: E402
import pattern_engine    # noqa: E402


# --------------------------------------------------------------------------- #
# INJECTION — wraps pattern_engine, the same scorer finance-scam-check uses
# --------------------------------------------------------------------------- #
INJECTION_PROBES = {
    "sqli": ["'", "1' OR '1'='1", "1 AND SLEEP(3)--", "1 UNION SELECT NULL--"],
    "xss": ["<script>alert(1)</script>", "\"><svg onload=alert(1)>"],
    "traversal": ["../../../../etc/passwd", "%2e%2e%2fetc%2fpasswd"],
    "ssti": ["{{7*7}}", "${7*7}"],
}

CATEGORY_FOR_KIND = {"sqli": "sqli", "traversal": "traversal", "ssti": "ssti", "xss": "xss"}
# Display labels. Emitting "SQLI" and fixing it later in the formatter means the
# acronym is wrong in every intermediate artifact too, so it is fixed at the source.
KIND_LABEL = {"sqli": "SQLi", "xss": "XSS", "traversal": "path traversal",
              "ssti": "SSTI", "csrf": "CSRF", "ssrf": "SSRF"}
CWE_FOR_KIND = {"sqli": "CWE-89", "xss": "CWE-79", "traversal": "CWE-22", "ssti": "CWE-1336"}


def inject_params(url: str, probes: dict | None = None,
                  allow_private: bool = False,
                  authorized: bool = False) -> list[dict]:
    """Reflected-baseline injection check.

    Deliberately shallow: send payload, look for the payload or an error
    signature coming back. No timing attacks at scale, no data extraction —
    those are manual follow-ups, which is exactly what the ``confidence`` field
    is for. A reflection is not an execution, and the finding says so.
    """
    cr.assert_authorized(authorized, passive=False)
    parts = urllib.parse.urlsplit(url)
    cr.assert_public(parts.hostname or "", allow_private)
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    if not query:
        return [common.finding(
            finding="No query parameters to probe — injection testing needs a parameter.",
            severity="info", confidence=1.0,
            evidence={"url": url, "hint": "Supply ?id=1 style params, or test a POST body by hand."},
            specialist="cyber-injection", target=url, ftype="testing-note")]

    findings: list[dict] = []
    for pname in query:
        for kind, payloads in (probes or INJECTION_PROBES).items():
            for payload in payloads:
                q = {k: list(v) for k, v in query.items()}
                q[pname] = [payload]
                test_url = urllib.parse.urlunsplit(
                    parts._replace(query=urllib.parse.urlencode(q, doseq=True)))
                r = cr.http_get(test_url, timeout=8, authorized=authorized,
                                allow_private=allow_private)
                if not r.get("status"):
                    continue
                body = r["body"]
                verdict = pattern_engine.match_category(body, CATEGORY_FOR_KIND[kind])
                reflected = payload in body
                conf = max(verdict.confidence, 0.45 if reflected else 0.0)
                if conf < 0.45:
                    continue
                error_matched = verdict.confidence >= 0.7
                # Reflection with no observed *effect* is a candidate, not a break.
                # Cap the severity so a mirrored string cannot become a CVSS 9.8.
                if reflected and not verdict.outcome_confirmed:
                    severity = "low"
                elif error_matched:
                    severity = common.severity_hint(conf, boost="high")
                else:
                    severity = common.severity_hint(conf)
                findings.append(common.finding(
                    finding=(f"Possible {KIND_LABEL.get(kind, kind)} in parameter '{pname}'"
                             + (" — payload reflected verbatim" if reflected else "")
                             + (" with an error signature" if error_matched else "")
                             + ("" if verdict.outcome_confirmed else
                                " (no executed-effect signal; reflection only)")),
                    severity=severity,
                    confidence=min(conf, 0.95),
                    evidence={"parameter": pname, "payload": payload, "kind": kind,
                              "url": test_url, "status": r["status"], "reflected": reflected,
                              "error_signals": [m.rule_id for m in verdict.matches],
                              "outcome_confirmed": verdict.outcome_confirmed,
                              "response_snippet": (verdict.matches[0].snippet
                                                   if verdict.matches else body[:200]),
                              "oracle_note": "Reflection alone is not execution. Confirm in a "
                                             "browser or with an out-of-band canary before "
                                             "rating above medium."},
                    specialist="cyber-injection", target=url,
                    ftype=f"{kind}-injection", cwe=CWE_FOR_KIND[kind],
                    # top-level so the scorer can see it without digging into
                    # evidence (and so it survives dedup merging)
                    outcome_confirmed=verdict.outcome_confirmed))
                break   # one confirmed payload per kind/param is enough signal
    return findings


def scan_secrets(text: str, where: str = "<input>") -> list[dict]:
    v = pattern_engine.match_category(text, "secrets")
    if not v.matches:
        return []
    strong = [m for m in v.matches if m.weight >= 0.85]
    return [common.finding(
        finding=f"{len(v.matches)} secret/crypto weakness signal(s) in {where}: "
                f"{', '.join(m.rule_id for m in v.matches)}",
        severity="critical" if strong else "medium",
        confidence=min(v.confidence + 0.1, 0.99),
        evidence={"matches": [m.as_dict() for m in v.matches],
                  "redaction_note": "Rotate the credential first; report the pattern, never a "
                                    "live secret. Mask anything you paste."},
        specialist="cyber-crypto-weakness", target=where,
        ftype="secret-exposure", cwe="CWE-798")]


# --------------------------------------------------------------------------- #
# DEPENDENCIES
# --------------------------------------------------------------------------- #
def _parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def _ver_lt(a: str, b: str) -> bool:
    pa, pb = _parse_version(a), _parse_version(b)
    n = max(len(pa), len(pb))
    pa += (0,) * (n - len(pa))
    pb += (0,) * (n - len(pb))
    return pa < pb


def parse_manifest(path: str) -> list[tuple[str, str]]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    pairs: list[tuple[str, str]] = []
    if p.name == "package.json":
        data = json.loads(text)
        for section in ("dependencies", "devDependencies"):
            for name, ver in (data.get(section) or {}).items():
                pairs.append((name, str(ver).lstrip("^~>=< ")))
    else:
        for line in text.splitlines():
            line = line.split("#")[0].strip()
            if not line or line.startswith("-"):
                continue
            m = re.match(r"([A-Za-z0-9_.\-\[\]]+)\s*(?:==|>=|~=|===)?\s*([0-9][\w.]*)", line)
            if m:
                pairs.append((m.group(1).lower().replace("_", "-"), m.group(2)))
    return pairs


def dependency_scan(manifest: str, ecosystem: str | None = None) -> list[dict]:
    adv_path = Path(__file__).resolve().parent / "rules" / "dependencies.json"
    advisories = json.loads(adv_path.read_text(encoding="utf-8"))["advisories"]
    eco = ecosystem or ("npm" if manifest.endswith("package.json") else "pypi")
    pairs = parse_manifest(manifest)
    out: list[dict] = []
    for name, ver in pairs:
        for a in advisories:
            if a["ecosystem"] != eco or a["package"] != name:
                continue
            if _ver_lt(ver, a["fixed"]):
                out.append(common.finding(
                    finding=f"{name} {ver} is vulnerable: {a['title']} (fixed in {a['fixed']})",
                    severity=a["severity"], confidence=0.95,
                    evidence={"package": name, "installed": ver, "fixed_in": a["fixed"],
                              "advisory": a["id"], "ref": a["ref"], "manifest": manifest,
                              "note": "Bundled advisory set is a small offline subset. "
                                      "Cross-check OSV.dev / pip-audit / npm audit before reporting."},
                    specialist="cyber-dependency-scan", target=manifest,
                    ftype="vulnerable-dependency", cwe="CWE-1104"))
    if not out:
        out.append(common.finding(
            finding=f"No known-vulnerable versions among {len(pairs)} scanned package(s).",
            severity="info", confidence=0.7,
            evidence={"manifest": manifest, "packages_checked": len(pairs),
                      "caveat": "Absence here is absence from a bundled list — not absence of risk."},
            specialist="cyber-dependency-scan", target=manifest, ftype="coverage-note"))
    return out


# --------------------------------------------------------------------------- #
# PAYLOAD CORPUS — attack knowledge for the manual-testing specialists
# --------------------------------------------------------------------------- #
def payload_corpus(technique: str) -> dict:
    """Curated test strings, each paired with what a *true* positive looks like.

    The pairing is the point: an agent that only knows payloads rates reflections
    as executions. The corpus makes it state the oracle.
    """
    corpus = {
        "xss": {
            "contexts": ["html-body", "attribute-double", "attribute-single",
                         "script-string", "href-js"],
            "payloads": {
                "html-body": ["<img src=x onerror=alert(document.domain)>",
                              "<svg onload=alert(1)>"],
                "attribute-double": ['" onmouseover="alert(1)',
                                     '"><img src=x onerror=alert(1)>'],
                "attribute-single": ["' onmouseover='alert(1)"],
                "script-string": ["';alert(1);//", "\\';alert(1);//"],
                "href-js": ["javascript:alert(document.domain)",
                            "jav&#x09;ascript:alert(1)"],
            },
            "positive_signal": "Payload reaches the DOM in an executable context. Check CSP "
                               "first: a strict CSP can make a real XSS non-exploitable.",
        },
        "csrf": {
            "checks": ["state-changing endpoint accepts cross-origin POST with no token",
                       "session cookie lacks SameSite",
                       "Origin/Referer not validated server-side"],
            "positive_signal": "A browser-enforced cross-site form post succeeds with the "
                               "victim's cookie attached.",
            "modern_caveat": "SameSite=Lax is the browser default now and blocks most classic "
                             "CSRF. Test explicitly instead of assuming the old behaviour.",
        },
        "ssrf": {
            "targets": ["http://169.254.169.254/latest/meta-data/",
                        "http://metadata.google.internal/", "http://127.0.0.1:6379/",
                        "http://[::1]:80/", "http://0x7f000001/", "http://localtest.me/"],
            "positive_signal": "Response body or timing shows the server-side fetch happened.",
            "guardrail": "Only point these at infrastructure you own. Use your own canary host, "
                         "never someone else's listener.",
        },
        "access_control": {
            "checks": ["horizontal: swap an object id between two accounts you own",
                       "vertical: hit an admin route with a normal session",
                       "forced browsing: GET /admin as anon returns a body, not a 302",
                       "method swap: PATCH/DELETE on read-only routes"],
            "positive_signal": "Another account's data is returned, or a role gains an action "
                               "it should not have.",
            "rule": "Only ever test IDOR between accounts YOU created on the target.",
        },
        "auth_flaws": {
            "checks": ["no lockout after 20 wrong passwords",
                       "password-reset token predictability / reuse",
                       "JWT: alg=none, weak HS256 secret, missing exp, kid injection",
                       "session not rotated on login",
                       "remember-me token with no server-side revocation"],
            "positive_signal": "Access granted with invalid credentials, or a token outlives "
                               "the logout that should have killed it.",
        },
        "business_logic": {
            "checks": ["negative quantity / price manipulation in cart",
                       "coupon reuse and stacking", "race on redeem (parallel requests)",
                       "currency rounding abuse",
                       "workflow skipping (jump straight to /confirm)"],
            "positive_signal": "The system's invariant breaks: money or entitlement is "
                               "misallocated.",
            "rule": "Logic bugs pay best but need the most narrative. Capture exact steps.",
        },
    }
    return corpus.get(technique, {
        "error": f"unknown technique '{technique}'", "have": sorted(corpus)})


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _emit(findings: list[dict], as_json: bool, out_path: str | None) -> None:
    normalized = common.normalize_findings(findings)
    payload = json.dumps(normalized, indent=2, ensure_ascii=False)
    if out_path:
        Path(out_path).write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {len(normalized)} finding(s) -> {out_path}", file=sys.stderr)
    if as_json or out_path:
        print(payload)
    else:
        for f in normalized:
            print(f"[{f['severity']:8s}] c={f['confidence']:.2f}  {f['finding'][:104]}")
        print(f"-- {len(normalized)} finding(s) --")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cyber_scan.py",
        description="Setu Cyber recon + vulnerability specialists (authorized testing only)")
    ap.add_argument("subcommand", choices=[
        "subdomain-enum", "fingerprint", "endpoints", "osint", "header-check",
        "injection", "secrets", "deps", "payloads", "selftest"])
    ap.add_argument("target", nargs="?", default=None)
    ap.add_argument("--i-have-authorization", action="store_true",
                    help="confirm you are authorized to test this target")
    ap.add_argument("--passive", action="store_true",
                    help="DNS/read-only recon; no probe payloads")
    ap.add_argument("--allow-private", action="store_true",
                    help="allow targets resolving to private/lab address space")
    ap.add_argument("--wordlist", default=None)
    ap.add_argument("--manifest", default=None)
    ap.add_argument("--text-file", default=None)
    ap.add_argument("--kinds", default=None, help="comma list: sqli,xss,traversal,ssti")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)

    try:
        if a.subcommand == "selftest":
            return cr._selftest()
        if a.subcommand == "payloads":
            print(json.dumps(payload_corpus(a.target or "xss"), indent=2))
            return 0
        if a.subcommand == "deps":
            if not a.manifest:
                ap.error("deps requires --manifest requirements.txt|package.json")
            _emit(dependency_scan(a.manifest), a.json, a.out)
            return 0
        if a.subcommand == "secrets":
            if not a.text_file:
                ap.error("secrets requires --text-file")
            _emit(scan_secrets(Path(a.text_file).read_text(encoding="utf-8", errors="replace"),
                               a.text_file), a.json, a.out)
            return 0
        if not a.target:
            ap.error(f"{a.subcommand} requires a target")

        if a.subcommand == "subdomain-enum":
            words = Path(a.wordlist).read_text().split() if a.wordlist else None
            _emit(cr.subdomain_enum(a.target, words, a.passive, a.allow_private,
                                    authorized=a.i_have_authorization), a.json, a.out)
            return 0

        # Everything below sends requests, so the authorization gate applies.
        cr.assert_authorized(a.i_have_authorization, passive=a.passive)
        host = urllib.parse.urlsplit(a.target).hostname if "://" in a.target else a.target

        if a.subcommand == "fingerprint":
            _emit(cr.tech_fingerprint(a.target, a.allow_private,
                                      authorized=a.i_have_authorization), a.json, a.out)
        elif a.subcommand == "endpoints":
            _emit(cr.endpoint_discovery(a.target, allow_private=a.allow_private,
                                        authorized=a.i_have_authorization), a.json, a.out)
        elif a.subcommand == "osint":
            _emit(cr.osint(host or a.target), a.json, a.out)
        elif a.subcommand == "header-check":
            _emit(cr.header_check(a.target, a.allow_private,
                                  authorized=a.i_have_authorization), a.json, a.out)
        elif a.subcommand == "injection":
            probes = None
            if a.kinds:
                wanted = [k.strip() for k in a.kinds.split(",") if k.strip()]
                probes = {k: v for k, v in INJECTION_PROBES.items() if k in wanted}
            _emit(inject_params(a.target, probes, a.allow_private,
                                authorized=a.i_have_authorization), a.json, a.out)
        return 0
    except cr.ScopeError as e:
        print(f"SCOPE REFUSED: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
