#!/usr/bin/env python3
"""Setu Finance — categorization, scam screening, deterministic loan maths.

THE RULE THAT SHAPES THIS FILE
------------------------------
``finance-emi-calc`` must never produce a number by language model. So the maths
lives here, in ``Decimal`` arithmetic, and the model's only job is to *explain*
what this module computed. ``explain_emi`` is the boundary: it takes a finished
result dict and asks a model for prose about it, and the prompt explicitly
contains the numbers so nothing has to be inferred.

Two supporting choices:

* **Decimal, not float.** `0.1 + 0.2` is not `0.3`, and a currency calculation
  that is off by a paisa per instalment is off by rupees over a tenure. Banker's
  rounding is used for the final currency figures and the amortisation schedule is
  reconciled so the closing balance lands on exactly zero, not on 0.003.
* **Scam screening reuses the shared pattern engine.** The same
  ``scripts/rules/patterns.json`` "scam" category that `cyber-injection` uses for
  its matching, with the same probabilistic-OR scorer, so "confidence 0.8" means
  the same thing in both packs. That is the shared-memory idea applied to signal
  definitions rather than to examples.

Disclaimer handling: every advice-shaped output passes through
``common.with_disclaimer(text, "finance")``. Not optional, not configurable.

CLI
---
    python3 finance.py emi --principal 500000 --rate 9.2 --months 60
    python3 finance.py emi --principal 500000 --rate 9.2 --months 60 --explain
    python3 finance.py categorize --desc "SWIGGY ORDER 482" --amount 486
    python3 finance.py scam --text "Dear customer, your KYC is pending..."
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, ROUND_HALF_EVEN, getcontext
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common         # noqa: E402
import pattern_engine  # noqa: E402

getcontext().prec = 28

PAISE = Decimal("0.01")
RUPEES = Decimal("1")


def _money(d: Decimal, quantum: Decimal = PAISE) -> Decimal:
    return d.quantize(quantum, rounding=ROUND_HALF_EVEN)


# --------------------------------------------------------------------------- #
# EMI — deterministic
# --------------------------------------------------------------------------- #
def emi(principal, annual_rate, months, *, schedule: bool = False,
        currency: str = "INR") -> dict:
    """EMI = P*r*(1+r)^n / ((1+r)^n - 1), with r the monthly rate.

    Handles the r = 0 case separately (the formula divides by zero) and
    reconciles the amortisation schedule so the final closing balance is exactly
    zero rather than a rounding residue.
    """
    P = Decimal(str(principal))
    n = int(months)
    rate_pct = Decimal(str(annual_rate))
    if P <= 0:
        raise ValueError("principal must be positive")
    if n <= 0:
        raise ValueError("months must be a positive integer")
    if rate_pct < 0:
        raise ValueError("annual_rate cannot be negative")

    r = rate_pct / Decimal(12) / Decimal(100)
    if r == 0:
        raw_emi = P / Decimal(n)
    else:
        factor = (Decimal(1) + r) ** n
        raw_emi = P * r * factor / (factor - Decimal(1))
    emi_val = _money(raw_emi, PAISE)
    # A sub-paisa EMI can round to zero, or fail to cover even rounded monthly
    # interest. Use the smallest payment that makes principal progress; the final
    # scheduled instalment still absorbs the exact remaining balance.
    minimum_progress_payment = _money(P * r) + PAISE
    emi_val = max(emi_val, minimum_progress_payment)

    # The schedule is the single source of truth for totals. Rounding the EMI
    # means the loan can close a paisa or two before instalment n,
    # so 60 x EMI is NOT what the borrower actually pays. Two different "total"
    # numbers in one result would look like a bug and undermine trust in the
    # whole calculation, so one number is truth and the other is labelled naive.
    rows = _amortise(P, r, n, emi_val, currency)
    summary = rows[-1]
    total_payment = Decimal(summary["total_paid"])
    total_interest = Decimal(summary["total_interest"])
    fixed_instalment_total = _money(emi_val * n)

    result = {
        "check": "finance-emi-calc",
        "method": "deterministic Decimal arithmetic — no model involved in the numbers",
        "inputs": {"principal": str(P), "annual_rate_pct": str(rate_pct),
                   "months": n, "currency": currency},
        "monthly_rate": float(r * 100),
        "emi_raw": str(raw_emi.quantize(PAISE)),
        "emi": format(emi_val.normalize(), "f"),
        "total_payment": str(_money(total_payment)),
        "total_interest": str(_money(total_interest)),
        "interest_share_pct": round(float(total_interest / total_payment * 100), 2)
        if total_payment else 0.0,
        "instalments": summary["instalments"],
        "final_instalment": rows[-2]["payment"] if len(rows) > 1 else str(emi_val),
        "payment_if_fixed_instalment": str(fixed_instalment_total),
        "totals_note": "total_payment is the sum of the actual schedule; the final "
                       "instalment absorbs the rounding residue, so it differs from EMI. "
                       "payment_if_fixed_instalment is the naive n x EMI figure.",
        "reconciled": summary["reconciled"],
        "formula": "EMI = P*r*(1+r)^n / ((1+r)^n - 1)   [r = annual/12/100]",
        "rounding": "EMI rounded to the nearest paisa (half-to-even); schedule "
                    "reconciled so the closing balance is exactly zero",
    }

    if schedule:
        result["amortisation"] = rows
    return result


def _amortise(P: Decimal, r: Decimal, n: int, emi_val: Decimal,
              currency: str) -> list[dict]:
    """Month-by-month schedule that ends at exactly zero.

    The final instalment absorbs the residue instead of leaving 0.02 outstanding,
    which is what a real lender's schedule does and what a naive loop does not.
    """
    rows: list[dict] = []
    balance = P
    paid_interest = Decimal(0)
    for month in range(1, n + 1):
        interest = _money(balance * r)
        principal_part = _money(emi_val - interest)
        if month == n:
            # absorb residue: the last payment closes whatever is left
            principal_part = balance
            payment = _money(balance + interest)
        else:
            payment = emi_val
        closing = _money(balance - principal_part)
        if closing < 0:
            closing = Decimal("0.00")
        paid_interest += interest
        rows.append({
            "month": month,
            "opening": str(_money(balance)),
            "payment": str(payment),
            "interest": str(interest),
            "principal": str(principal_part),
            "closing": str(closing),
        })
        balance = closing
        if balance == 0:
            break
    rows.append({
        "summary": True,
        "instalments": len(rows),
        "total_paid": str(_money(sum(Decimal(x["payment"]) for x in rows))),
        "total_interest": str(_money(paid_interest)),
        "closing_balance": str(balance),
        "reconciled": balance == 0,
    })
    return rows


def explain_emi(result: dict, model: str | None = None) -> str:
    """Prose about already-computed numbers. The model never sees an uncomputed value."""
    from model_wrapper import call_model  # local import: keeps `emi` dependency-free
    prompt = (
        "You are explaining a loan calculation to a borrower. Every number below was "
        "computed in Python; do not recompute, estimate or alter any of them. Use only "
        "these figures, in plain language, in at most 5 sentences. Do not give advice "
        "about whether to take the loan.\n\n"
        f"Principal: {result['inputs']['principal']} {result['inputs']['currency']}\n"
        f"Annual rate: {result['inputs']['annual_rate_pct']}%\n"
        f"Tenure: {result['inputs']['months']} months\n"
        f"Monthly EMI: {result['emi']}\n"
        f"Total repayment: {result['total_payment']}\n"
        f"Total interest: {result['total_interest']}\n"
        f"Interest as share of total repayment: {result['interest_share_pct']}%\n\n"
        "Explain what the borrower will pay, and what the difference between the "
        "principal and the total repayment represents."
    )
    text = call_model(prompt, model=model, allow_math=True,
                      system="You explain numbers you were given. You never produce new ones.")
    return common.with_disclaimer(text, "finance")


# --------------------------------------------------------------------------- #
# CATEGORIZE
# --------------------------------------------------------------------------- #
CATEGORY_RULES: list[tuple[str, str, str]] = [
    # (category, pattern, why)
    ("food-delivery", r"swiggy|zomato|eatsure|dominos|pizza|kfc|mcdonald|blinkit\s+food",
     "named food-delivery brand in the merchant string"),
    ("groceries", r"bigbasket|dmart|reliance\s+fresh|more\s+retail|zepto|instamart|kirana|"
                  r"supermarket|grocery", "grocery/supermarket merchant"),
    ("transport", r"uber|ola|rapido|irctc|metro|fastag|petrol|diesel|hp\s+petrol|"
                  r"indian\s+oil|bharat\s+petrol|toll", "transport or fuel merchant"),
    ("utilities", r"electricity|bescom|tsspdcl|apspdcl|water\s+bill|gas\s+bill|broadband|"
                  r"act\s+fibernet|jio\s+fiber|airtel\s+xstream", "utility provider"),
    ("telecom", r"jio|airtel|vodafone|vi\s+postpaid|bsnl|recharge", "telecom operator"),
    ("subscriptions", r"netflix|spotify|prime\s+video|hotstar|disney|youtube\s+premium|"
                     r"icloud|google\s+one|adobe|chatgpt|claude", "recurring digital subscription"),
    ("health", r"apollo|pharmeasy|1mg|netmeds|hospital|clinic|diagnostic|lab\s+test|"
               r"medical", "healthcare merchant"),
    ("education", r"udemy|coursera|byju|unacademy|school\s+fee|college\s+fee|tuition|"
                  r"textbook", "education expense"),
    ("rent-housing", r"rent|maintenance\s+charges|society|housing\s+loan|emi\s+home",
     "housing-related payment"),
    ("emi-loan", r"emi|loan\s+instal|bajaj\s+finserv|hdb\s+financial|tata\s+capital",
     "loan instalment"),
    ("transfer", r"upi|neft|imps|rtgs|transfer\s+to|sent\s+to|p2p",
     "person-to-person transfer, category not inferable from the string"),
    ("income", r"salary|credited\s+by|interest\s+credit|refund|reversal|cashback",
     "inflow, not an expense"),
    ("investment", r"mutual\s+fund|sip|zerodha|groww|kuvera|ppf|nps|fixed\s+deposit",
     "investment or savings instrument"),
    ("cash-withdrawal", r"atm\s+withdraw|cash\s+withdrawal|awb", "cash withdrawal"),
]

MERCHANT_NOISE = re.compile(r"\b(upi|pos|neft|imps|ref|txn|payment|paid|to|from|"
                            r"\d{6,}|xx+\d+)\b", re.I)


def categorize(description: str, amount: float | str | None = None) -> dict:
    """Classify one transaction and explain *why*, with the matched evidence."""
    desc = (description or "").strip()
    low = desc.lower()
    matches = []
    for category, pattern, why in CATEGORY_RULES:
        m = re.search(pattern, low)
        if m:
            matches.append({"category": category, "matched": m.group(0), "why": why,
                            "weight": 1.0})
    cleaned = MERCHANT_NOISE.sub(" ", desc)
    merchant = re.sub(r"\s+", " ", cleaned).strip(" -_*#")

    amount_note = None
    if amount is not None:
        try:
            amt = float(str(amount).replace(",", ""))
            if amt < 0:
                amount_note = "negative amount recorded — treated as an inflow/refund"
            elif amt == 0:
                amount_note = "zero amount — check whether this is a verification entry"
            elif amt > 100000:
                amount_note = ("large amount — worth a second look before categorising "
                               "automatically")
        except ValueError:
            amount_note = "amount was not numeric; ignored for classification"

    if not matches:
        return {
            "check": "finance-categorize",
            "merchant_guess": merchant or desc,
            "category": "uncategorized",
            "confidence": 0.15,
            "reasoning": "No rule matched the merchant string. Guessing a category here "
                         "would be worse than leaving it uncategorised, because an "
                         "uncategorised line is visible and a wrong one is not.",
            "needs_review": True,
            "amount_note": amount_note,
            "suggestion": "Label it manually once; the memory layer will surface it as a "
                          "few-shot example next time a similar string appears.",
        }

    primary = matches[0]
    conf = 0.9 if len(matches) == 1 else 0.7
    # A bare "upi" hit is a weak signal — it says how money moved, not what for.
    if primary["category"] == "transfer" and len(primary["matched"]) <= 3:
        conf = 0.4
    return {
        "check": "finance-categorize",
        "merchant_guess": merchant or desc,
        "category": primary["category"],
        "confidence": conf,
        "reasoning": (f"Matched '{primary['matched']}' in the description — {primary['why']}."
                      + (f" Also matched: {', '.join(m['category'] for m in matches[1:])}."
                         if len(matches) > 1 else "")),
        "all_matches": matches,
        "needs_review": conf < 0.5,
        "amount_note": amount_note,
    }


# --------------------------------------------------------------------------- #
# SCAM CHECK — reuses the shared pattern engine
# --------------------------------------------------------------------------- #
def scam_check(text: str, channel: str | None = None) -> dict:
    """Screen a message for fraud patterns using the shared rules + scorer."""
    verdict = pattern_engine.match_category(text, "scam")
    low = (text or "").lower()

    # Channel context modulates risk: a KYC demand by SMS is a different animal
    # from an email from a known address, and URLs in DMs are the usual vector.
    channel_notes = {
        "sms": "SMS sender ids are trivially spoofed; treat any bank SMS link as hostile.",
        "whatsapp": "WhatsApp groups impersonating banks or task-job recruiters are the "
                    "most common vector in India.",
        "email": "Check the From domain character by character; lookalikes differ by a letter.",
        "call": "Voice calls can display a spoofed caller id; never share an OTP verbally.",
    }
    has_link = bool(re.search(r"https?://|www\.", low))
    asks_money = bool(re.search(r"\b(pay|deposit|transfer|send\s+money|processing\s+fee|"
                                r"registration\s+(fee|charge))\b", low))
    asks_credentials = bool(re.search(r"\b(otp|cvv|pin|password|aadhaar|pan)\b", low))

    band = verdict.band
    confidence = verdict.confidence
    # A link plus a credential ask is the canonical phishing shape; the individual
    # patterns may each be weak, so record the combination explicitly.
    if has_link and asks_credentials and confidence < 0.9:
        confidence = min(confidence + 0.1, 0.97)
        band = pattern_engine.band_of(confidence)

    advice = []
    if confidence >= 0.45:
        advice = [
            "Do not click any link in this message.",
            "Never share an OTP, CVV, PIN or password — no bank asks for these.",
            "Verify by calling the number printed on your card or the bank's official site, "
            "not a number from this message.",
            "Forward to 1930 (India cyber-fraud helpline) or report at cybercrime.gov.in.",
        ]
        if asks_money:
            advice.insert(1, "Do not pay any 'processing' or 'registration' fee; "
                             "legitimate refunds never require an advance payment.")
    else:
        advice = ["No known fraud pattern matched. That is not a clean bill of health — "
                  "new scams reuse patterns this list does not have yet."]

    return {
        "check": "finance-scam-check",
        "risk_band": band,
        "confidence": round(confidence, 3),
        "verdict": ("likely-scam" if confidence >= 0.7 else
                    "suspicious" if confidence >= 0.45 else "no-pattern-matched"),
        "signal_count": len(verdict.matches),
        "signals": [m.as_dict() for m in verdict.matches],
        "channel": channel,
        "channel_note": channel_notes.get((channel or "").lower()),
        "observations": {"contains_link": has_link, "asks_for_money": asks_money,
                         "asks_for_credentials": asks_credentials},
        "advice": advice,
        "engine": "shared pattern_engine (same rules and scorer as cyber-injection)",
        "disclaimer": common.DISCLAIMERS["finance"],
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="finance.py", description="Setu Finance pack")
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("emi", help="deterministic EMI calculation")
    e.add_argument("--principal", required=True, type=float)
    e.add_argument("--rate", required=True, type=float, help="annual interest rate, percent")
    e.add_argument("--months", required=True, type=int)
    e.add_argument("--schedule", action="store_true")
    e.add_argument("--explain", action="store_true", help="ask the model for prose (numbers stay Python's)")
    e.add_argument("--model", default=None)

    c = sub.add_parser("categorize")
    c.add_argument("--desc", required=True)
    c.add_argument("--amount", default=None)

    s = sub.add_parser("scam")
    s.add_argument("--text", default=None)
    s.add_argument("--file", default=None)
    s.add_argument("--channel", default=None)

    a = ap.parse_args(argv)
    if a.cmd == "emi":
        try:
            res = emi(a.principal, a.rate, a.months, schedule=a.schedule)
        except ValueError as err:
            print(json.dumps({"error": str(err)}, indent=2))
            return 2
        if a.explain:
            res["explanation"] = explain_emi(res, a.model)
        print(json.dumps(res, indent=2))
        return 0
    if a.cmd == "categorize":
        print(json.dumps(categorize(a.desc, a.amount), indent=2))
        return 0
    if a.cmd == "scam":
        text = a.text or (Path(a.file).read_text(encoding="utf-8") if a.file else "")
        if not text:
            ap.error("scam requires --text or --file")
        print(json.dumps(scam_check(text, a.channel), indent=2))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
