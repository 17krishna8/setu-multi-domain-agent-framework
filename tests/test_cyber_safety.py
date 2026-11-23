"""Regression tests for Cyber's passive signals and scope handling."""
from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cyber_recon as cr  # noqa: E402
import cyber_scan  # noqa: E402
import report_formatter  # noqa: E402


def test_ct_subdomains_requires_a_label_boundary(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps([
                {"name_value": "api.example.com"},
                {"name_value": "example.com"},
                {"name_value": "notexample.com"},
                {"name_value": "evil-example.com"},
            ]).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda *args, **kwargs: Response())

    assert cr.ct_subdomains("example.com") == ["api.example.com"]


def test_non_global_ipv6_ranges_are_blocked():
    for address in ("::127.0.0.1", "::169.254.169.254", "fec0::1", "64:ff9b:1::1"):
        assert cr.is_private(address), address


def test_header_check_uses_the_final_redirected_url(monkeypatch):
    monkeypatch.setattr(cr, "assert_public", lambda *args, **kwargs: None)
    monkeypatch.setattr(cr, "http_get", lambda *args, **kwargs: {
        "status": 200, "headers": {"strict-transport-security": "max-age=31536000"},
        "body": "ok", "url": "https://example.com/secure",
        "chain": ["http://example.com/start"],
    })

    findings = cr.header_check("http://example.com/start", authorized=True)

    assert not any(f["type"] == "transport" for f in findings)


def test_txt_lookup_parses_dig_records_and_concatenates_chunks(monkeypatch):
    import shutil
    import subprocess

    output = (
        ";; ->>HEADER<<- opcode: QUERY, status: NOERROR, id: 1\n"
        "example.com. 300 IN TXT \"v=spf1 include:_spf.example.net \" \"~all\"\n"
    )
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/dig" if name == "dig" else None)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, stdout=output, stderr=""))

    assert cr._query_txt_records("example.com") == ["v=spf1 include:_spf.example.net ~all"]


def test_txt_lookup_treats_dns_errors_as_unknown(monkeypatch):
    import shutil
    import subprocess

    output = ";; ->>HEADER<<- opcode: QUERY, status: SERVFAIL, id: 1\n"
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/dig" if name == "dig" else None)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, stdout=output, stderr=""))

    assert cr._query_txt_records("example.com") is None


def test_osint_queries_txt_records_for_spf_and_dmarc(monkeypatch):
    queried = []
    records = {
        "example.com": ["v=spf1 include:_spf.example.net ~all"],
        "_dmarc.example.com": ["v=DMARC1; p=reject"],
    }
    monkeypatch.setattr(cr, "_query_txt_records",
                        lambda name: queried.append(name) or records[name])
    monkeypatch.setattr(cr, "ct_subdomains", lambda domain: [])

    findings = cr.osint("example.com")

    assert queried == ["example.com", "_dmarc.example.com"]
    assert not any("No SPF" in f["finding"] or "No DMARC" in f["finding"]
                   for f in findings)


def test_osint_does_not_claim_missing_records_when_txt_lookup_is_unavailable(monkeypatch):
    monkeypatch.setattr(cr, "_query_txt_records", lambda name: None)
    monkeypatch.setattr(cr, "ct_subdomains", lambda domain: [])

    findings = cr.osint("example.com")

    assert len(findings) == 2
    assert all(f["severity"] == "info" for f in findings)
    assert all("could not be checked" in f["finding"] for f in findings)
    assert all("unknown; no absence claim made" in f["evidence"]["result"]
               for f in findings)


def test_osint_reports_absent_spf_and_dmarc_from_txt_results(monkeypatch):
    monkeypatch.setattr(cr, "_query_txt_records", lambda name: [])
    monkeypatch.setattr(cr, "ct_subdomains", lambda domain: [])

    findings = cr.osint("example.com")

    assert {f["finding"].split()[1] for f in findings} == {"SPF", "DMARC"}


@pytest.mark.parametrize("location", [
    "ftp://example.com/file", "file:///etc/passwd", "http://127.0.0.1:99999/",
    "https://user:password@example.com/path", "http://[invalid-ipv6/",
])
def test_malformed_or_unsupported_redirect_returns_error_result(monkeypatch, location):
    class Redirect:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found", {"Location": location}, io.BytesIO(b""),
            )

    monkeypatch.setattr(cr, "resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(cr.urllib.request, "build_opener", lambda *handlers: Redirect())

    result = cr.http_get("https://public.example/start", authorized=True)

    assert result["status"] == 0
    assert "error" in result
    assert result["chain"][0] == "https://public.example/start"


def test_failed_http_helpers_return_incomplete_coverage_notes(monkeypatch):
    monkeypatch.setattr(cr, "assert_public", lambda *args, **kwargs: None)
    monkeypatch.setattr(cr, "http_get", lambda url, **kwargs: {
        "status": 0, "headers": {}, "body": "", "url": url,
        "error": "redirect outside the authorized origin",
    })

    headers = cr.header_check("https://example.com/", authorized=True)
    endpoints = cr.endpoint_discovery(
        "https://example.com/", paths=["/admin"], authorized=True,
    )

    assert len(headers) == 1 and headers[0]["type"] == "coverage-note"
    assert len(endpoints) == 2
    assert all(f["type"] == "coverage-note" for f in endpoints)
    assert all("no negative security conclusion" in f["evidence"]["result"]
               for f in [*headers, *endpoints])


def test_injection_failure_is_reported_as_incomplete(monkeypatch):
    monkeypatch.setattr(cr, "assert_public", lambda *args, **kwargs: None)
    monkeypatch.setattr(cr, "http_get", lambda url, **kwargs: {
        "status": 0, "headers": {}, "body": "", "url": url,
        "error": "connection refused",
    })

    findings = cyber_scan.inject_params(
        "https://example.com/?q=1", probes={"sqli": ["' OR 1=1--"]}, authorized=True,
    )

    assert len(findings) == 1
    assert findings[0]["type"] == "coverage-note"
    assert findings[0]["evidence"]["failure_count"] == 1


def test_coverage_notes_are_not_reported_as_confirmed_security_findings():
    report = report_formatter.to_markdown([{
        "finding": "Header check was not fully tested because request failed.",
        "severity": "info", "confidence": 1.0,
        "evidence": {"error": "connection refused"},
        "specialist": "cyber-coverage", "type": "coverage-note",
        "target": "https://example.com/",
    }])

    assert "0 confirmed finding(s)" in report
    assert "## Coverage notes (not security findings)" in report
    assert "## Findings" not in report


def test_cyber_scan_console_escapes_unencodable_findings():
    import os
    import subprocess

    root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"
    code = (
        "import sys; sys.path.insert(0, 'scripts'); import cyber_scan; "
        "cyber_scan._emit([{'finding': 'unicode ' + chr(0x4e2d), 'severity': 'info', "
        "'confidence': 1.0, 'evidence': {}}], False, None)"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                            capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert "unicode" in result.stdout


def test_cyber_report_clis_escape_unencodable_text_in_cp1252():
    import os
    import subprocess

    root = Path(__file__).resolve().parent.parent
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "cp1252"
    cases = [
        ("report_formatter", "report_formatter.to_markdown([{'finding': 'unicode ' + chr(0x4e2d), "
         "'severity': 'info', 'confidence': 1.0, 'evidence': {'note': chr(0x4e2d)}, "
         "'type': 'recon'}])"),
        ("severity_scorer", "severity_scorer.to_markdown(severity_scorer.rank([{'finding': 'unicode ' + chr(0x4e2d), "
         "'severity': 'info', 'confidence': 1.0, 'evidence': {'note': chr(0x4e2d)}, "
         "'type': 'recon'}]))"),
    ]
    for module, expression in cases:
        code = (f"import sys; sys.path.insert(0, 'scripts'); import {module}; "
                f"{module}.common.configure_console(); print({expression})")
        result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, f"{module}: {result.stderr}"
