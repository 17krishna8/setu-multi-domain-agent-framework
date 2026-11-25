from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import cyber_scan
import finance
import pattern_engine


def test_one_rule_added_once_improves_both_packs(tmp_path, monkeypatch):
    rules = copy.deepcopy(pattern_engine.load_rules())
    text = "setu-synthetic-shared-signal"
    before_cyber = pattern_engine.match_category(text, "secrets", rules)
    before_finance = pattern_engine.match_category(text, "scam", rules)
    assert before_cyber.matches == []
    assert before_finance.matches == []

    # The only edit is to the shared rule registry: it is one rule, not a copy
    # inserted into each pack's local list.
    rules.setdefault("shared_rules", []).append({
        "id": "test-shared-marker",
        "label": "Synthetic shared marker",
        "categories": ["secrets", "scam"],
        "regex": r"(?i)\bsetu-synthetic-shared-signal\b",
        "weight": 0.8,
        "confirms_outcome": True,
    })
    monkeypatch.setattr(pattern_engine, "load_rules", lambda path=None: rules)

    after_cyber = pattern_engine.match_category(text, "secrets")
    after_finance = pattern_engine.match_category(text, "scam")
    assert [match.rule_id for match in after_cyber.matches] == ["test-shared-marker"]
    assert [match.rule_id for match in after_finance.matches] == ["test-shared-marker"]
    assert after_cyber.confidence == after_finance.confidence == 0.8

    # Exercise both pack entry points using the same shared data.
    cyber = cyber_scan.scan_secrets(text, "fixture")
    scam = finance.scam_check(text)
    assert cyber and cyber[0]["evidence"]["matches"][0]["rule_id"] == "test-shared-marker"
    assert "test-shared-marker" in [signal["rule_id"] for signal in scam["signals"]]


def test_existing_category_specific_rules_still_match():
    rules = pattern_engine.load_rules()
    assert pattern_engine.match_category("AKIAABCDEFGHIJKLMNOP", "secrets", rules).matches
    assert pattern_engine.match_category("Your KYC update is pending", "scam", rules).matches
