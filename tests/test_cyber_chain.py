"""Cyber chain mode selection and authorization regression tests."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cyber_chain  # noqa: E402
import cyber_recon as cr  # noqa: E402


def test_passive_url_chain_uses_only_passive_domain_recon(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cr, "assert_public", lambda host, allow_private=False: None)
    monkeypatch.setattr(cr, "subdomain_enum",
                        lambda *args, **kwargs: calls.append(("subdomain", args, kwargs)) or [])
    monkeypatch.setattr(cr, "osint", lambda host: calls.append(("osint", host)) or [])
    monkeypatch.setattr(cr, "tech_fingerprint",
                        lambda *args, **kwargs: pytest.fail("passive mode must not fingerprint"))
    monkeypatch.setattr(cr, "endpoint_discovery",
                        lambda *args, **kwargs: pytest.fail("passive mode must not probe paths"))
    monkeypatch.setattr(cr, "header_check",
                        lambda *args, **kwargs: pytest.fail("passive mode must not fetch headers"))

    result = cyber_chain.run_chain(
        "https://example.com/item?id=1", passive=True, out_dir=str(tmp_path),
        log_memory=False,
    )

    assert result["mode"] == "passive"
    assert [c[0] for c in calls] == ["subdomain", "osint"]
    assert calls[0][1][2] is True


def test_active_helpers_are_called_with_authorization_in_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "assert_public", lambda host, allow_private=False: None)
    observed = []

    def active(name):
        def call(*args, **kwargs):
            observed.append((name, args, kwargs))
            return []
        return call

    monkeypatch.setattr(cr, "tech_fingerprint", active("fingerprint"))
    monkeypatch.setattr(cr, "endpoint_discovery", active("endpoints"))
    monkeypatch.setattr(cr, "header_check", active("headers"))
    monkeypatch.setattr(cyber_chain.cyber_scan, "inject_params", active("injection"))

    cyber_chain.run_chain(
        "https://example.com/item?id=1", authorized=True, out_dir=str(tmp_path),
        log_memory=False,
    )

    assert {name for name, _, _ in observed} == {
        "fingerprint", "endpoints", "headers", "injection"
    }
    assert all(kwargs.get("authorized") is True for _, _, kwargs in observed)
