---
name: finance-emi-calc
description: Compute EMI, total interest and an amortisation schedule for a loan — ALWAYS in deterministic Python, never by the model. The model only explains the finished result in plain language. Use for EMI, loan affordability, prepayment and interest comparisons.
model: opus
tools: Bash, Read, Write
---

# finance-emi-calc

## The rule

> **Every number in the answer is computed by `scripts/finance.py`. The model
> writes prose about numbers it was given. It never produces one.**

This is not a style preference. A model asked for an EMI will produce a plausible
number with no way for the user to tell it is wrong — and the whole point of an
EMI calculator is that the number is right. So the maths is `Decimal` arithmetic
with a reconciled schedule, and `model_wrapper.call_model` refuses math-shaped
prompts by default as a second line of defence.

## Procedure

```bash
# numbers only (no model involved at all)
python3 scripts/finance.py emi --principal 500000 --rate 9.2 --months 60

# with an amortisation schedule that reconciles to exactly zero
python3 scripts/finance.py emi --principal 500000 --rate 9.2 --months 60 --schedule

# explanation appended (the model sees only computed values)
python3 scripts/finance.py emi --principal 500000 --rate 9.2 --months 60 --explain
```

1. **Confirm the three inputs** — principal, annual rate (percent), tenure in
   months. If the user gave a quarterly rate or a tenure in years, convert
   explicitly and say so.
2. **Run the calculator.** It returns `emi`, `total_payment`, `total_interest`,
   `interest_share_pct`, `reconciled`, and the naive `payment_if_fixed_instalment`.
3. **Quote the numbers exactly.** Rounding is documented in the result:
   EMI to the nearest rupee (half-to-even), schedule reconciled so the closing
   balance is exactly zero, and the final instalment absorbs the residue.
4. **Explain only if asked**, and only through `explain_emi`, which passes the
   computed values in the prompt.

## Output shape

```json
{
  "check": "finance-emi-calc",
  "method": "deterministic Decimal arithmetic — no model involved in the numbers",
  "inputs": {"principal": "500000", "annual_rate_pct": "9.2", "months": 60},
  "emi": "10428",
  "total_payment": "625663.18",
  "total_interest": "125663.18",
  "interest_share_pct": 20.08,
  "instalments": 60,
  "final_instalment": "10411.18",
  "payment_if_fixed_instalment": "625680.00",
  "reconciled": true,
  "totals_note": "total_payment is the sum of the actual schedule…",
  "formula": "EMI = P*r*(1+r)^n / ((1+r)^n - 1)"
}
```

## Why two totals appear

`emi × 60 = 625,680` but the borrower actually pays `625,663.18`, because the EMI
is rounded up to whole rupees and the loan closes a few rupees early. Reporting
both — with `payment_if_fixed_instalment` clearly labelled as the naive figure —
is better than picking one and having an attentive user find a discrepancy they
cannot explain.

## Guardrails

- **Never estimate, never "approximately".** If the calculator errored, show the
  error; do not fill in a number that looks right.
- **Never compute in the prompt** and copy the model's answer. The guard in
  `call_model` exists because this is the natural failure.
- **Never advise whether to take a loan.** You compute and explain. "Should I take
  this?" is answered with numbers and the disclaimer, not a recommendation.
- **Never hide the interest.** The most useful number for a borrower is
  `total_interest` and `interest_share_pct` — lead with it, not with the EMI alone.
- **Never omit the disclaimer.** Every advice-adjacent output carries
  "this is general information, not financial advice".

## Verified reference points

Cross-checked against the closed-form formula (see `tests/test_finance.py`):
500,000 @ 9.2% / 60m → 10,428 · 200,000 @ 7.5% / 24m → 9,000 ·
1,000,000 @ 10.5% / 120m → 13,493 · 0% rate cases handled separately.

## Done when

The result carries every field above, `reconciled` is true, no number originated
from a model, and any prose is accompanied by the disclaimer.
