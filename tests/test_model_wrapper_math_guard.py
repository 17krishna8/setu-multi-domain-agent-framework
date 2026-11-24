from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pytest
import model_wrapper


def test_explanatory_emi_request_with_existing_numbers_is_allowed(monkeypatch):
    monkeypatch.setenv("SETU_OFFLINE", "1")
    prompt = "Explain why this EMI is high: the monthly payment is 10,428 at 9.2%."

    result = model_wrapper.call_model(prompt)

    assert result.startswith("[offline-stub]")


def test_explicit_emi_computation_is_still_refused(monkeypatch):
    monkeypatch.setenv("SETU_OFFLINE", "1")

    with pytest.raises(ValueError, match="refusing a math-shaped prompt"):
        model_wrapper.call_model("Calculate the EMI for 500000 at 9 percent for 60 months")


def test_basic_numeric_explanation_is_allowed_but_arithmetic_is_blocked(monkeypatch):
    monkeypatch.setenv("SETU_OFFLINE", "1")

    assert model_wrapper.call_model("Explain why interest share is 31%.").startswith(
        "[offline-stub]")
    with pytest.raises(ValueError, match="refusing a math-shaped prompt"):
        model_wrapper.call_model("What is 7 * 8?")
