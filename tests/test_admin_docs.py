#!/usr/bin/env python3
"""Regression tests for the Admin/Legal human finalization gate."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import admin_docs  # noqa: E402


def _complete_general_text() -> str:
    return ("To,\nPublic Information Officer\n\nSubject: Request\n\nFacts supplied.\n\n"
            "Date: 1 January 2025\n\nYours faithfully,\nAsha Rao\n")


def test_finalize_requires_explicit_confirmation_and_name():
    result = admin_docs.admin_finalize(_complete_general_text(), None, False)

    assert result["finalized"] is False
    assert result["status"] == "draft"


def test_finalize_recomputes_format_check_instead_of_trusting_missing_report():
    result = admin_docs.admin_finalize("unfinished draft", "Asha Rao", True,
                                       format_report={"verdict": "PASS"})

    assert result["finalized"] is False
    assert any("admin-format" in reason for reason in result["reasons"])


def test_finalize_recomputes_law_check_instead_of_trusting_caller_report():
    text = (_complete_general_text()
            + '\nSection 6(1) of the Right to Information Act, 2005 "invented legal text".\n')
    result = admin_docs.admin_finalize(text, "Asha Rao", True,
                                       law_report={"verdict": "OK"},
                                       format_report={"verdict": "PASS"})

    assert result["finalized"] is False
    assert any("admin-verify-law" in reason for reason in result["reasons"])


def test_finalize_accepts_confirmed_text_that_passes_recomputed_checks():
    result = admin_docs.admin_finalize(_complete_general_text(), "Asha Rao", True,
                                       when="2025-01-01")

    assert result["finalized"] is True
    assert result["status"] == "final"
    assert "human-confirmed on 2025-01-01 by Asha Rao" in result["text"]


def test_finalize_refuses_unfilled_placeholders_even_with_confirmation():
    text = _complete_general_text() + "[[NEEDS: missing fact]]\n"
    result = admin_docs.admin_finalize(text, "Asha Rao", True)

    assert result["finalized"] is False
    assert any("unfilled placeholder" in reason for reason in result["reasons"])
