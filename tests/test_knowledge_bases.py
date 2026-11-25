from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import admin_docs
import cyber_scan


def test_common_rti_provisions_are_in_offline_legal_corpus():
    for provision in ("6(1)", "6(3)", "7(1)", "7(2)", "8(1)", "8(1)(j)",
                      "19(1)", "19(3)", "20"):
        result = admin_docs.verify_quotes("Section " + provision +
            " of the Right to Information Act, 2005.")[0]
        assert result["status"] in {"cited-not-quoted", "verified-verbatim"}


def test_common_consumer_provisions_are_in_offline_legal_corpus():
    for provision in ("2(7)", "2(9)", "2(11)", "2(47)", "34", "35", "69"):
        result = admin_docs.verify_quotes("Section " + provision +
            " of the Consumer Protection Act, 2019.")[0]
        assert result["status"] in {"cited-not-quoted", "verified-verbatim"}


def test_dependency_subset_detects_added_common_packages(tmp_path):
    manifest = tmp_path / "package.json"
    manifest.write_text(json.dumps({"dependencies": {
        "word-wrap": "1.2.3", "json5": "2.2.1", "ip": "1.1.8"
    }}), encoding="utf-8")

    findings = cyber_scan.dependency_scan(str(manifest), "npm")

    evidence = [finding["evidence"] for finding in findings]
    assert {item["package"] for item in evidence} == {"word-wrap", "json5", "ip"}
    assert all(item["advisory"].startswith("SETU-ADV-") for item in evidence)


def test_dependency_fixes_are_not_reported_as_vulnerable(tmp_path):
    manifest = tmp_path / "package.json"
    manifest.write_text(json.dumps({"dependencies": {
        "word-wrap": "1.2.4", "json5": "2.2.2", "ip": "1.1.9"
    }}), encoding="utf-8")

    findings = cyber_scan.dependency_scan(str(manifest), "npm")

    assert len(findings) == 1
    assert findings[0]["type"] == "coverage-note"
