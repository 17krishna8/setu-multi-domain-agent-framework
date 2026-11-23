#!/usr/bin/env python3
"""Tests for the Finance pack — the deterministic maths most of all.

The EMI figures are checked against the closed-form formula computed independently
in floating point, so a regression in the Decimal implementation shows up as a
mismatch rather than as a plausible-looking wrong number.
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import finance  # noqa: E402


def _reference_emi(P: float, annual_pct: float, n: int) -> float:
    r = annual_pct / 12 / 100
    if r == 0:
        return P / n
    f = (1 + r) ** n
    return P * r * f / (f - 1)


def test_emi_matches_closed_form():
    cases = [(500_000, 9.2, 60), (200_000, 7.5, 24), (1_000_000, 10.5, 120),
             (750_000, 8.99, 84), (50_000, 12.0, 36)]
    for P, rate, n in cases:
        got = Decimal(finance.emi(P, rate, n)["emi"])
        want = _reference_emi(P, rate, n)
        assert abs(float(got) - want) <= 1.0, f"{P}@{rate}%/{n}m: {got} vs {want}"


def test_zero_rate_is_not_a_division_by_zero():
    res = finance.emi(120_000, 0, 12)
    assert res["emi"] == "10000"
    assert Decimal(res["total_interest"]) == 0


def test_schedule_reconciles_to_exactly_zero():
    res = finance.emi(500_000, 9.2, 60, schedule=True)
    summary = res["amortisation"][-1]
    assert summary["reconciled"] is True
    assert Decimal(summary["closing_balance"]) == Decimal("0.00")


def test_principal_parts_sum_to_the_principal():
    res = finance.emi(500_000, 9.2, 60, schedule=True)
    rows = [r for r in res["amortisation"] if not r.get("summary")]
    total_principal = sum(Decimal(r["principal"]) for r in rows)
    assert total_principal == Decimal(res["inputs"]["principal"])


def test_instalment_count_is_exactly_the_tenure():
    res = finance.emi(500_000, 9.2, 60, schedule=True)
    rows = [r for r in res["amortisation"] if not r.get("summary")]
    assert len(rows) == 60
    assert res["instalments"] == 60


def test_totals_are_internally_consistent():
    res = finance.emi(500_000, 9.2, 60, schedule=True)
    rows = [r for r in res["amortisation"] if not r.get("summary")]
    assert Decimal(res["total_payment"]) == sum(Decimal(r["payment"]) for r in rows)
    assert Decimal(res["total_interest"]) == sum(Decimal(r["interest"]) for r in rows)


def test_invalid_inputs_raise_rather_than_guessing():
    for args in [(0, 9, 12), (-1, 9, 12), (1000, 9, 0), (1000, -5, 12)]:
        try:
            finance.emi(*args)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {args}")


def test_categorize_is_uncertain_rather_than_wrong():
    res = finance.categorize("Random merchant 8812", 499)
    assert res["category"] == "uncategorized"
    assert res["needs_review"] is True
    assert res["confidence"] < 0.5


def test_categorize_names_its_evidence():
    res = finance.categorize("SWIGGY ORDER 482", 486)
    assert res["category"] == "food-delivery"
    assert "swiggy" in res["reasoning"].lower()


def test_scam_check_flags_credential_harvest():
    msg = ("Dear customer, your KYC update is pending. Account will be blocked "
           "within 24 hours. Share OTP at http://sbi-kyc.xyz/verify")
    res = finance.scam_check(msg, "sms")
    assert res["verdict"] == "likely-scam"
    assert res["confidence"] >= 0.7
    assert res["observations"]["asks_for_credentials"] is True
    assert any("OTP" in a for a in res["advice"])


def test_scam_check_never_claims_safe():
    res = finance.scam_check("Your Swiggy order has been delivered.", "sms")
    assert res["verdict"] == "no-pattern-matched"
    assert "not a clean bill of health" in " ".join(res["advice"])


def test_disclaimer_always_present():
    assert "not financial advice" in finance.scam_check("hello")["disclaimer"]
