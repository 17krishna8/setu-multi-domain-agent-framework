#!/usr/bin/env python3
"""Setu Admin/Legal — document drafting, citation verification, formatting.

Three jobs, three functions, and one rule that governs all of them:

    NOTHING LEAVES THIS MODULE MARKED "FINAL".

A legal document that is wrong is worse than no document: RTI requests get
rejected on a formatting technicality, grievances miss their statutory window,
and a paraphrased statute in a complaint is a misrepresentation. So:

* ``admin-draft``        builds a skeleton with every field either filled from
                         the request or left as an explicit ``[[NEEDS: …]]``
                         marker. Nothing is invented to fill space.
* ``admin-verify-law``   checks every citation against a local corpus and reports
                         **verified-verbatim / paraphrase / unverifiable /
                         not-in-corpus / probably-fabricated** separately. It can
                         also flag a section number that does not exist in the
                         Act it is attributed to.
* ``admin-format``       checks the mechanical requirements (addressing, subject
                         line, date, enclosures, signature block, page limits)
                         and scores them.
* ``admin_finalize``     is the ONLY function that sets ``status: final``, and it
                         refuses unless a human confirmation with a name and a
                         timestamp is supplied.

The corpus in ``scripts/rules/legal_corpus.json`` is deliberately small and
marked as such. An agent that says "verified" about a law it cannot see is the
single most dangerous failure mode in this pack, so ``verify`` distinguishes
"not in my corpus" from "does not exist" and never conflates them.

CLI
---
    python3 admin_docs.py draft --type rti --subject "…" --to "PIO, Dept of X" \
        --facts "…" --out draft.md --json
    python3 admin_docs.py verify --text draft.md --json
    python3 admin_docs.py format --text draft.md --type grievance
    python3 admin_docs.py finalize --text draft.md --confirmed-by "Asha R." --yes
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

CORPUS_PATH = Path(__file__).resolve().parent / "rules" / "legal_corpus.json"


# --------------------------------------------------------------------------- #
# corpus
# --------------------------------------------------------------------------- #
def load_corpus(path: str | Path | None = None) -> dict:
    p = Path(path) if path else CORPUS_PATH
    if not p.exists():
        return {"acts": {}, "note": "corpus missing"}
    return json.loads(p.read_text(encoding="utf-8"))


def _norm(text: str) -> str:
    """Normalise for comparison: collapse whitespace, unify quotes/dashes."""
    t = text or ""
    t = t.replace("\u2018", "'").replace("\u2019", "'")
    t = t.replace("\u201c", '"').replace("\u201d", '"')
    t = t.replace("\u2014", "-").replace("\u2013", "-").replace("\u00a7", "s.")
    t = re.sub(r"\s+", " ", t)
    return t.strip().lower()


CITATION_RE = re.compile(
    r"(?:section|sec\.?|s\.)?\s*(?P<section>\d+[A-Za-z]?(?:\(\d+\))?(?:\([a-z]\))?)"
    r"\s+of\s+(?:the\s+)?(?P<act>[A-Z][A-Za-z0-9,'’\-&\s()]{4,80}?"
    r"(?:Act|Code|Rules|Regulations|Constitution)(?:,\s*\d{4})?)", re.I)
SECTION_ONLY_RE = re.compile(r"\bsection\s+(\d+[A-Za-z]?(?:\(\d+\))?)\b", re.I)


def _find_act(act_text: str, corpus: dict) -> tuple[str, dict] | tuple[None, None]:
    """Match a cited act name against the corpus, tolerantly (year + punctuation)."""
    target = _norm(act_text)
    target = re.sub(r"\b(the|of|and)\b", " ", target)
    target = re.sub(r"[^a-z0-9 ]", " ", target)
    target = re.sub(r"\s+", " ", target).strip()
    best, best_score = None, 0.0
    for key, body in (corpus.get("acts") or {}).items():
        names = [key, body.get("short_name", ""), *body.get("aliases", [])]
        for name in names:
            cand = _norm(name)
            cand = re.sub(r"\b(the|of|and)\b", " ", cand)
            cand = re.sub(r"[^a-z0-9 ]", " ", cand)
            cand = re.sub(r"\s+", " ", cand).strip()
            if not cand:
                continue
            score = difflib.SequenceMatcher(None, target, cand).ratio()
            if target == cand:
                score = 1.0
            if score > best_score:
                best, best_score = key, score
    if best and best_score >= 0.82:
        return best, corpus["acts"][best]
    return None, None


# --------------------------------------------------------------------------- #
# VERIFY-LAW
# --------------------------------------------------------------------------- #
def verify_quotes(text: str, corpus: dict | None = None) -> list[dict]:
    """Find every 'section X of the Y Act' and check the text against the corpus.

    Statuses, and what each one obliges the agent to do:

      verified-verbatim  the quoted words match a corpus provision word for word
      paraphrase         close, but not verbatim -> must be replaced with the
                         exact text, or marked as a paraphrase labelled as such
      not-in-corpus      the Act is known, the provision is not loaded here ->
                         MUST be confirmed against the official source
      unknown-act        the Act is not in the corpus at all -> cannot verify
      no-quote           the provision is cited but no text is quoted -> the
                         document asserts law it has not shown, which is flagged
    """
    corpus = corpus or load_corpus()
    results: list[dict] = []
    for m in CITATION_RE.finditer(text):
        cited_act = m.group("act").strip()
        cited_sec = m.group("section").strip()
        act_key, act = _find_act(cited_act, corpus)

        # the "quote" is the surrounding sentence(s) that follow the citation
        window_start = m.end()
        window = text[window_start: window_start + 700]
        quoted = _extract_quote_after(text, m.start(), m.end())

        base = {"citation": m.group(0).strip(), "act_cited": cited_act,
                "section_cited": cited_sec, "position": m.start(),
                "quoted_text": quoted}

        if act is None:
            results.append({**base, "status": "unknown-act", "confidence": 0.0,
                            "severity": "advisory",
                            "action": "Cannot verify: this Act is not in Setu's local corpus. "
                                      "Quote it verbatim from the official text and cite the source.",
                            "act_in_corpus": False})
            continue

        sections = act.get("sections") or {}
        key = _match_section_key(cited_sec, sections)
        if key is None:
            results.append({**base, "status": "not-in-corpus", "confidence": 0.0,
                            "severity": "advisory", "act_in_corpus": True,
                            "act": act.get("short_name", act_key),
                            "available_sections": sorted(sections),
                            "action": f"Section {cited_sec} is not loaded for "
                                      f"{act.get('short_name', act_key)}. Verify against the "
                                      f"official gazette copy before citing it.",
                            "exists_in_corpus": None})
            continue

        provision = sections[key]
        if not quoted:
            # A provision cited as the legal *basis* ("under Section 6(1)…") is
            # normal drafting and needs no quotation. Flagging it as an error would
            # make the checker cry wolf on every correct document.
            results.append({**base, "status": "cited-not-quoted", "confidence": 0.5,
                            "severity": "advisory",
                            "act_in_corpus": True, "act": act.get("short_name", act_key),
                            "provision_text": provision.get("text", ""),
                            "action": "Cited as a basis without quotation — acceptable. If the "
                                      "document relies on the provision's specific wording, quote "
                                      "it verbatim instead.",
                            "exists_in_corpus": True})
            continue

        ratio = difflib.SequenceMatcher(None, _norm(quoted), _norm(provision.get("text", ""))).ratio()
        if ratio >= 0.985:
            status, conf, sev = "verified-verbatim", 0.98, "pass"
            action = "Verbatim match against the local corpus. Confirm the corpus copy is current."
        elif ratio >= 0.62:
            status, conf, sev = "paraphrase-quoted", 0.4, "blocking"
            action = ("This is presented as a quotation but is a paraphrase. A paraphrase in "
                      "quotation marks is a misrepresentation. Replace with provision_text "
                      "exactly, or remove the quotation marks and label it a paraphrase.")
        else:
            status, conf, sev = "mismatch", 0.1, "blocking"
            action = ("The quoted words do not match the corpus provision. Either the wrong "
                      "section is cited or the quote is invented. Fix before this leaves draft.")
        results.append({**base, "status": status, "confidence": conf, "severity": sev,
                        "ratio": round(ratio, 3),
                        "act_in_corpus": True, "act": act.get("short_name", act_key),
                        "provision_text": provision.get("text", ""),
                        "source": provision.get("source"),
                        "action": action, "exists_in_corpus": True})
    return results


def _extract_quote_after(text: str, start: int, end: int) -> str:
    """Pull a quoted span immediately following a citation, if there is one."""
    tail = text[end: end + 800]
    tail = tail.lstrip(" ,:-\n")
    for open_q, close_q in (('"', '"'), ("\u201c", "\u201d"), ("'", "'")):
        if tail.startswith(open_q):
            close = tail.find(close_q, 1)
            if close > 0:
                return tail[1:close].strip()
    # "provides: \"...\"", "states that \"...\"", "reads as follows: \"...\""
    m = re.match(r"(?:provides|states|reads|says|mandates|lays\s+down)"
                 r"(?:\s+as\s+follows)?\s*:?\s*(?:that\s+)?[\"“']?"
                 r"(.{15,600}?)[\"”']?(?:\.\s|\.$|\n|$)", tail, re.S)
    if m:
        return m.group(1).strip()
    return ""


def _match_section_key(cited: str, sections: dict) -> str | None:
    if cited in sections:
        return cited
    m = re.match(r"(\d+[A-Za-z]?)", cited)
    if not m:
        return None
    for k in sections:
        if k.startswith(m.group(1)) or k == m.group(1):
            return k
    return None


def flag_fabricated_sections(text: str, corpus: dict | None = None) -> list[dict]:
    """Catch a section number that cannot exist in the Act it is attributed to.

    This is the highest-value check in the module: an LLM inventing 'Section 41A of
    the RTI Act' produces a document that looks authoritative and is void.
    """
    corpus = corpus or load_corpus()
    out = []
    for m in CITATION_RE.finditer(text):
        act_key, act = _find_act(m.group("act"), corpus)
        if not act:
            continue
        cited = m.group("section")
        sections = act.get("sections") or {}
        max_sec = max((int(re.match(r"\d+", k).group(0)) for k in sections), default=0)
        num = int(re.match(r"\d+", cited).group(0))
        if max_sec and num > max_sec:
            out.append({
                "status": "suspicious-section",
                "citation": m.group(0).strip(),
                "reason": f"{act.get('short_name')} is loaded only up to section {max_sec}; "
                          f"section {num} is beyond it.",
                "caveat": "This Act may have been amended — 'beyond my loaded range' is not "
                          "proof of non-existence. Verify against the official text before "
                          "declaring it fabricated.",
                "action": "Check the official source. If the section does not exist, remove the "
                          "citation entirely — a void provision invalidates the document.",
            })
    return out


def verify_law(text: str, corpus: dict | None = None) -> dict:
    corpus = corpus or load_corpus()
    quotes = verify_quotes(text, corpus)
    suspicious = flag_fabricated_sections(text, corpus)
    statuses: dict[str, int] = {}
    for q in quotes:
        statuses[q["status"]] = statuses.get(q["status"], 0) + 1
    # A wrong or invented quotation is the only thing that can BLOCK: it is a
    # misrepresentation. Everything the corpus cannot confirm is REVIEW — a human
    # must check it, but a small offline corpus must not pretend to be authority.
    blocking = [q for q in quotes if q.get("severity") == "blocking"] + suspicious
    advisory = [q for q in quotes if q.get("severity") == "advisory"]
    verdict = "BLOCK" if [q for q in quotes if q.get("severity") == "blocking"] else \
              ("REVIEW" if (suspicious or advisory) else "OK")
    return {
        "check": "admin-verify-law",
        "citations_found": len(quotes),
        "status_counts": statuses,
        "results": quotes,
        "suspicious_sections": suspicious,
        "blocking": len(blocking),
        "advisory": len(advisory),
        "verdict": verdict,
        "corpus_note": corpus.get("note", ""),
        "rule": "Quote verbatim or do not quote. A paraphrase presented as statute is a "
                "misrepresentation; a citation nobody verified is a liability.",
    }


# --------------------------------------------------------------------------- #
# DRAFT
# --------------------------------------------------------------------------- #
DOC_TYPES = {"rti", "grievance", "consumer", "general"}

RTI_TEMPLATE = """To,
The Public Information Officer,
{to}

**Subject:** Application under Section 6(1) of the Right to Information Act, 2005 — request for information

Respected Sir/Madam,

1. Full name of the applicant: {applicant}
2. Address for correspondence: {address}
3. Contact details (phone / email): {contact}

**Particulars of the information requested:**

{facts}

**Period to which the information relates:** {period}

**Whether the information relates to the applicant:** {relation}

4. I state that I am a citizen of India and that the information sought does not
   fall under any of the exemptions under Section 8 or Section 9 of the Act.

5. The prescribed application fee of {fee} has been paid by {fee_mode}. Payment
   receipt is enclosed.

6. If the information is held by another public authority, I request that this
   application be transferred under Section 6(3) within five days.

7. I request a reply within thirty days as required by Section 7(1).

Yours faithfully,

{applicant}
{address}
{contact}

Date: {date}
Place: {place}

**Enclosures:** {enclosures}

---
_DRAFT — not yet reviewed, verified or signed by a human. Section numbers in this
template are placeholders for admin-verify-law to check; they are not asserted as
verified merely because they were generated._
"""

GRIEVANCE_TEMPLATE = """To,
{to}

**Subject:** {subject}

Date: {date}

Respected Sir/Madam,

1. **Who I am:** {applicant}, residing at {address}, contactable at {contact}.

2. **What happened:** {facts}

3. **When and where:** {when_where}

4. **What I have already tried:** {prior_action}

5. **What I am asking for:** {relief}

6. **Time limit relied upon:** {timeline}

I request that this grievance be registered, acknowledged with a reference
number, and disposed of within the applicable time limit.

Yours faithfully,

{applicant}
{address}
{contact}

**Enclosures:** {enclosures}

---
_DRAFT — not yet reviewed, verified or signed by a human._
"""

CONSUMER_TEMPLATE = """Before the District Consumer Disputes Redressal Commission,
{to}

**Complaint No.:** [[NEEDS: to be assigned by the Commission]]

**Complainant:** {applicant}, {address}, {contact}

**Opposite Party:** {opposite_party}

**Subject:** Complaint under the Consumer Protection Act, 2019 — deficiency in service
/ unfair trade practice

**Facts of the case:**

{facts}

**Cause of action and limitation:** {timeline}

**Relief sought:** {relief}

**Value of goods/services and compensation claimed:** {amount}

**Documents relied upon:** {enclosures}

**Declaration:** I declare that the facts stated above are true to the best of my
knowledge and belief, and that this complaint is not frivolous or vexatious.

Date: {date}
Place: {place}

Complainant

{applicant}

---
_DRAFT — not yet reviewed, verified or signed by a human. This is a template, not
legal advice; a competent adviser should review it before filing._
"""

TEMPLATES = {"rti": RTI_TEMPLATE, "grievance": GRIEVANCE_TEMPLATE,
             "consumer": CONSUMER_TEMPLATE, "general": GRIEVANCE_TEMPLATE}

REQUIRED_FIELDS = {
    "rti": ["to", "applicant", "address", "contact", "facts", "period", "relation",
            "fee", "fee_mode", "date", "place", "enclosures"],
    "grievance": ["to", "subject", "applicant", "address", "contact", "facts",
                  "when_where", "prior_action", "relief", "timeline", "date",
                  "enclosures"],
    "consumer": ["to", "applicant", "address", "contact", "opposite_party", "facts",
                 "timeline", "relief", "amount", "enclosures", "date", "place"],
    "general": ["to", "applicant", "address", "facts"],
}


def draft(doc_type: str, fields: dict, corpus: dict | None = None) -> dict:
    """Fill a template. Missing fields become explicit [[NEEDS: …]] markers."""
    doc_type = doc_type.lower()
    if doc_type not in TEMPLATES:
        raise ValueError(f"unknown document type '{doc_type}'; have: {sorted(TEMPLATES)}")
    fields = dict(fields or {})
    fields.setdefault("date", date.today().strftime("%d %B %Y"))
    fields.setdefault("place", "[[NEEDS: place of signing]]")

    missing = []
    for key in REQUIRED_FIELDS[doc_type]:
        if not str(fields.get(key) or "").strip():
            missing.append(key)
            fields[key] = f"[[NEEDS: {key.replace('_', ' ')}]]"

    body = TEMPLATES[doc_type].format(**{k: str(v) for k, v in fields.items()})
    return {
        "check": "admin-draft",
        "doc_type": doc_type,
        "status": "draft",
        "text": body,
        "missing_fields": missing,
        "instruction": ("Every [[NEEDS: …]] marker must be filled by a human. Do not invent "
                        "an address, a fee, a date, a section number or a department name to "
                        "make the document look complete."),
    }


# --------------------------------------------------------------------------- #
# FORMAT
# --------------------------------------------------------------------------- #
FORMAT_RULES = {
    "rti": [
        ("addressee", r"^\s*To,\s*\n\s*The\s+Public\s+Information\s+Officer", 0.2,
         "Addressed to 'The Public Information Officer' on its own line"),
        ("subject_line", r"\*\*Subject:\*\*|^Subject:", 0.15, "Subject line present"),
        ("statutory_basis", r"Right to Information Act,?\s*2005", 0.15,
         "Names the RTI Act, 2005"),
        ("applicant_identity", r"Full name of the applicant", 0.1,
         "Applicant identity block present"),
        ("fee_statement", r"fee", 0.1, "Fee statement present"),
        ("time_limit", r"thirty days|30 days", 0.1, "States the 30-day response expectation"),
        ("place_and_date", r"Date:\s*\S.*\n\s*Place:\s*\S", 0.1, "Both date and place"),
        ("signature", r"Yours faithfully", 0.1, "Signature block present"),
    ],
    "grievance": [
        ("addressee", r"^\s*To,", 0.15, "Addressed to an authority"),
        ("subject_line", r"\*\*Subject:\*\*|^Subject:", 0.2, "Subject line present"),
        ("identity", r"Who I am", 0.1, "Identifies the complainant"),
        ("chronology", r"What happened", 0.15, "States what happened"),
        ("prior_action", r"already tried", 0.1, "Shows prior attempts"),
        ("relief", r"asking for", 0.15, "States the relief sought"),
        ("date", r"Date:\s*\S", 0.05, "Dated"),
        ("signature", r"Yours faithfully", 0.1, "Signature block present"),
    ],
    "consumer": [
        ("forum", r"District Consumer Disputes Redressal Commission", 0.15,
         "Addressed to the correct forum"),
        ("parties", r"Complainant:|Opposite Party:", 0.2, "Both parties named"),
        ("facts", r"Facts of the case", 0.15, "Facts section present"),
        ("relief", r"Relief sought", 0.15, "Relief sought stated"),
        ("amount", r"compensation claimed|Value of goods", 0.1,
         "Value/compensation stated (needed for pecuniary jurisdiction)"),
        ("documents", r"Documents relied upon", 0.1, "Documents listed"),
        ("declaration", r"declare that the facts", 0.1, "Verification/declaration present"),
        ("date", r"Date:\s*\S", 0.05, "Dated"),
    ],
    "general": [
        ("addressee", r"^\s*To,", 0.3, "Addressed to someone"),
        ("subject_line", r"Subject:", 0.2, "Subject line present"),
        ("date", r"Date:\s*\S", 0.2, "Dated"),
        ("signature", r"Yours faithfully|Sincerely", 0.3, "Signature block present"),
    ],
}


def check_format(text: str, doc_type: str = "general") -> dict:
    doc_type = doc_type.lower()
    rules = FORMAT_RULES.get(doc_type, FORMAT_RULES["general"])
    passed, failed = [], []
    score = 0.0
    for name, pattern, weight, desc in rules:
        if re.search(pattern, text, re.I | re.M):
            passed.append({"rule": name, "weight": weight, "desc": desc})
            score += weight
        else:
            failed.append({"rule": name, "weight": weight, "desc": desc})
    # Mechanical hazards that are always wrong, regardless of doc type
    hazards = []
    if re.search(r"\[\[NEEDS:[^\]]+\]\]", text):
        hazards.append({"hazard": "unfilled-placeholder",
                        "detail": "Document still contains [[NEEDS: …]] markers.",
                        "fix": "Fill each one by hand. A filed document with a placeholder is "
                               "a rejected document."})
    if re.search(r"\b(lorem ipsum|TBD|XXX|insert here)\b", text, re.I):
        hazards.append({"hazard": "placeholder-text",
                        "detail": "Contains lorem ipsum / TBD / XXX.",
                        "fix": "Remove before review."})
    if re.search(r"\b(?:\d{1,2}[/-]){2}\d{2,4}\b.*\n.*\b(?:\d{1,2}[/-]){2}\d{2,4}\b", text) and \
       not re.search(r"Date:\s*\S", text, re.I):
        hazards.append({"hazard": "ambiguous-date",
                        "detail": "Date-like strings without a labelled 'Date:' field.",
                        "fix": "Use one unambiguous date field; state the format if numeric."})
    if "status: final" in text.lower() or "final" in text.lower().split("\n")[0].lower():
        hazards.append({"hazard": "claims-final",
                        "detail": "Document describes itself as final without human sign-off.",
                        "fix": "Set status to draft; only admin_finalize may mark it final."})
    return {
        "check": "admin-format",
        "doc_type": doc_type,
        "score": round(min(score, 1.0), 2),
        "pass": score >= 0.8 and not hazards,
        "passed": passed,
        "failed": failed,
        "hazards": hazards,
        "verdict": "PASS" if (score >= 0.8 and not hazards) else "FAIL",
    }


# --------------------------------------------------------------------------- #
# FINALIZE — the human gate
# --------------------------------------------------------------------------- #
FINALIZE_BANNER = ("---\n_STATUS: FINAL — human-confirmed on {when} by {who}. "
                   "Setu drafted; a human reviewed and accepted responsibility._\n")


def admin_finalize(text: str, confirmed_by: str | None, confirmed: bool,
                   when: str | None = None, law_report: dict | None = None,
                   format_report: dict | None = None) -> dict:
    """Mark a draft final. Refuses without an explicit human confirmation.

    There is no code path from draft to final that a model can take on its own.
    """
    reasons = []
    if not confirmed:
        reasons.append("no human confirmation flag was supplied")
    if not confirmed_by or not str(confirmed_by).strip():
        reasons.append("confirmed_by must name the human who reviewed it")
    if law_report and law_report.get("verdict") == "BLOCK":
        reasons.append("admin-verify-law returned BLOCK — unresolved citation problems")
    if format_report and format_report.get("verdict") == "FAIL":
        reasons.append("admin-format returned FAIL — mechanical requirements unmet")
    unfilled = re.findall(r"\[\[NEEDS:([^\]]+)\]\]", text)
    if unfilled:
        reasons.append(f"{len(unfilled)} unfilled placeholder(s): "
                       + ", ".join(u.strip() for u in unfilled[:5]))
    if reasons:
        return {
            "check": "admin-finalize",
            "status": "draft",
            "finalized": False,
            "refused": True,
            "reasons": reasons,
            "rule": "Setu never auto-finalizes a legal document. A named human must confirm.",
            "text": text,
        }
    stamp = FINALIZE_BANNER.format(who=confirmed_by, when=when or date.today().isoformat())
    return {"check": "admin-finalize", "status": "final", "finalized": True,
            "refused": False, "confirmed_by": confirmed_by,
            "text": text.rstrip() + "\n\n" + stamp}


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="admin_docs.py",
                                 description="Setu Admin/Legal drafting + verification")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("draft")
    d.add_argument("--type", required=True, choices=sorted(TEMPLATES))
    d.add_argument("--field", action="append", default=[],
                   help="key=value, repeatable")
    d.add_argument("--fields-json", default=None)
    d.add_argument("--out", default=None)
    d.add_argument("--json", action="store_true")

    v = sub.add_parser("verify")
    v.add_argument("--text", required=True, help="file path or literal text")
    v.add_argument("--json", action="store_true")

    f = sub.add_parser("format")
    f.add_argument("--text", required=True)
    f.add_argument("--type", default="general", choices=sorted(FORMAT_RULES))
    f.add_argument("--json", action="store_true")

    fi = sub.add_parser("finalize")
    fi.add_argument("--text", required=True)
    fi.add_argument("--confirmed-by", default=None)
    fi.add_argument("--yes", action="store_true")
    fi.add_argument("--out", default=None)

    a = ap.parse_args(argv)

    def read(p: str) -> str:
        path = Path(p)
        return path.read_text(encoding="utf-8") if path.exists() else p

    if a.cmd == "draft":
        fields: dict = {}
        if a.fields_json:
            fields.update(json.loads(a.fields_json))
        for pair in a.field:
            k, _, v_ = pair.partition("=")
            fields[k.strip()] = v_.strip()
        res = draft(a.type, fields)
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(res["text"], encoding="utf-8")
            print(f"wrote draft -> {a.out}", file=sys.stderr)
        if a.json:
            print(json.dumps(res, indent=2))
        else:
            print(res["text"])
            if res["missing_fields"]:
                print("\nMISSING FIELDS (must be filled by a human): "
                      + ", ".join(res["missing_fields"]), file=sys.stderr)
        return 0
    if a.cmd == "verify":
        res = verify_law(read(a.text))
        print(json.dumps(res, indent=2) if a.json else
              f"verdict={res['verdict']} citations={res['citations_found']} "
              f"blocking={res['blocking']} {res['status_counts']}")
        return 0 if res["verdict"] != "BLOCK" else 1
    if a.cmd == "format":
        res = check_format(read(a.text), a.type)
        print(json.dumps(res, indent=2) if a.json else
              f"verdict={res['verdict']} score={res['score']} hazards={len(res['hazards'])}")
        return 0 if res["pass"] else 1
    if a.cmd == "finalize":
        text = read(a.text)
        res = admin_finalize(text, a.confirmed_by, a.yes)
        if a.out and res["finalized"]:
            Path(a.out).write_text(res["text"], encoding="utf-8")
        print(json.dumps({k: v for k, v in res.items() if k != "text"}, indent=2))
        return 0 if res["finalized"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
