"""Safety tests for Cyber's authorization and redirect boundaries."""
from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import cyber_recon as cr  # noqa: E402
import cyber_scan  # noqa: E402


def test_http_get_refuses_active_request_without_authorization(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("network opener must not be reached without authorization")

    monkeypatch.setattr(cr.urllib.request, "build_opener", unexpected)
    with pytest.raises(cr.ScopeError, match="authorization"):
        cr.http_get("https://example.com/")


def test_active_helpers_enforce_authorization_before_network(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("network request must not be reached without authorization")

    monkeypatch.setattr(cr, "resolve_host", unexpected)
    with pytest.raises(cr.ScopeError, match="authorization"):
        cr.tech_fingerprint("https://example.com/")
    with pytest.raises(cr.ScopeError, match="authorization"):
        cr.endpoint_discovery("https://example.com/")
    with pytest.raises(cr.ScopeError, match="authorization"):
        cr.header_check("https://example.com/")
    with pytest.raises(cr.ScopeError, match="authorization"):
        cyber_scan.inject_params("https://example.com/?id=1")
    with pytest.raises(cr.ScopeError, match="authorization"):
        cr.subdomain_enum("example.com", wordlist=[], passive=False)


def test_http_get_checks_private_redirect_before_opening_it(monkeypatch):
    opened = []

    class RedirectOnce:
        def open(self, request, timeout):
            opened.append(request.full_url)
            if len(opened) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 302, "Found", {"Location": "http://127.0.0.1/admin"},
                    io.BytesIO(b""),
                )
            pytest.fail("redirect target should be blocked before it is opened")

    def public_dns(host):
        if host == "public.example":
            return ["93.184.216.34"]
        if host == "127.0.0.1":
            return ["127.0.0.1"]
        pytest.fail(f"unexpected DNS lookup: {host}")

    monkeypatch.setattr(cr, "resolve_host", public_dns)
    monkeypatch.setattr(cr.urllib.request, "build_opener", lambda *handlers: RedirectOnce())

    with pytest.raises(cr.ScopeError, match="non-public"):
        cr.http_get("https://public.example/start", authorized=True)

    assert opened == ["https://public.example/start"]


def test_http_get_follows_authorized_public_redirect(monkeypatch):
    opened = []

    class Response(io.BytesIO):
        status = 200
        headers = {}

    class RedirectThenOk:
        def open(self, request, timeout):
            opened.append(request.full_url)
            if len(opened) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 302, "Found", {"Location": "/next"},
                    io.BytesIO(b""),
                )
            return Response(b"ok")

    monkeypatch.setattr(cr, "resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(cr.urllib.request, "build_opener", lambda *handlers: RedirectThenOk())

    result = cr.http_get("https://public.example/start", authorized=True)

    assert opened == ["https://public.example/start", "https://public.example/next"]
    assert result["chain"] == ["https://public.example/start"]


def test_http_get_rejects_unsupported_or_credentialed_urls_before_open(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid URL must be rejected before network access")

    monkeypatch.setattr(cr.urllib.request, "build_opener", unexpected)
    for url in ("file:///etc/passwd", "ftp://example.com/file",
                "https://user:password@example.com/"):
        with pytest.raises(cr.ScopeError, match="invalid or unsupported"):
            cr.http_get(url, authorized=True)
