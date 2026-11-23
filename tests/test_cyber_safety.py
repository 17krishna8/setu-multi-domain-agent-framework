"""Regression tests for Cyber's passive signals and scope handling."""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cyber_recon as cr  # noqa: E402


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
