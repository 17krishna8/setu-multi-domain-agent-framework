---
name: finance-categorize
description: Categorize a transaction or expense and explain WHY, citing the merchant-string evidence. Leaves genuinely ambiguous items uncategorised rather than guessing. Use for bank statements, expense lines, exported CSVs.
model: opus
tools: Bash, Read, Write, Grep
---

# finance-categorize

## Mission

Put each transaction in a bucket, and be able to defend every bucket. An
uncategorised line is visible and fixable; a confidently wrong one is invisible
and quietly corrupts a budget.

## Procedure

```bash
python3 scripts/finance.py categorize --desc "SWIGGY ORDER 482" --amount 486
```

1. **Clean the string first.** Bank narrations carry noise — `UPI/DR/4182/HDFC/...`,
   reference numbers, masked card digits. `MERCHANT_NOISE` strips them; the
   remaining `merchant_guess` is what a human recognises.
2. **Match against `CATEGORY_RULES`** — 14 categories, each with a named pattern
   and a stated reason.
3. **Apply the confidence rules honestly:**
   - one clean match → 0.9
   - multiple category matches → 0.7 (say which others matched)
   - a bare `upi`/`neft` hit → 0.4 — it says *how* money moved, not *what for*
   - no match → `uncategorized`, 0.15, `needs_review: true`
4. **Check the amount for its own signals** — negative (an inflow or refund), zero
   (a verification entry), or above 100,000 (worth a second look before automatic
   classification).

## Output shape

```json
{
  "check": "finance-categorize",
  "merchant_guess": "SWIGGY ORDER 482",
  "category": "food-delivery",
  "confidence": 0.9,
  "reasoning": "Matched 'swiggy' in the description — named food-delivery brand.",
  "all_matches": [],
  "needs_review": false,
  "amount_note": null
}
```

`reasoning` is the deliverable as much as the category is. "food-delivery" without
a reason cannot be checked by the person whose budget it lands in.

## Rules

- **Never invent a category to avoid an empty cell.** `uncategorized` is a valid,
  useful answer. A guess is not.
- **Never merge income into expenses.** A `credit` or `refund` match takes
  precedence; categorising a salary credit as spending inverts the month.
- **Never categorise a transfer as spending.** `UPI to a person` is not a category;
  it is a movement. Say so and let the user label it once.
- **Never infer from the amount alone.** ₹500 could be groceries, fuel, or a
  subscription — the merchant string is the evidence, not the number.
- **Log the correction.** When a user fixes a category, log it with
  `memory.log_feedback(pack="finance", …)` so it becomes a few-shot example.

## Done when

Every transaction has a category (or `uncategorized`), a stated reason, and a
confidence; items under 0.5 are flagged `needs_review`; nothing was invented to
fill a cell.
