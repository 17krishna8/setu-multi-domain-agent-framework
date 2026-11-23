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
import pattern_engine  # noqa: E402


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


def test_http_get_refuses_redirect_to_another_public_origin(monkeypatch):
    opened = []

    class CrossOriginRedirect:
        def open(self, request, timeout):
            opened.append(request.full_url)
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found", {"Location": "https://attacker.example/collect"},
                io.BytesIO(b""),
            )

    monkeypatch.setattr(cr, "resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(cr.urllib.request, "build_opener", lambda *handlers: CrossOriginRedirect())

    with pytest.raises(cr.ScopeError, match="outside the authorized origin"):
        cr.http_get("https://public.example/start", authorized=True, allow_private=True)

    assert opened == ["https://public.example/start"]


def test_http_get_refuses_https_downgrade(monkeypatch):
    opened = []

    class DowngradeRedirect:
        def open(self, request, timeout):
            opened.append(request.full_url)
            raise urllib.error.HTTPError(
                request.full_url, 302, "Found", {"Location": "http://public.example/next"},
                io.BytesIO(b""),
            )

    monkeypatch.setattr(cr, "resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(cr.urllib.request, "build_opener", lambda *handlers: DowngradeRedirect())

    with pytest.raises(cr.ScopeError, match="HTTPS downgrade"):
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


@pytest.mark.parametrize("address", [
    "::", "::ffff:127.0.0.1", "::ffff:169.254.169.254", "100.64.0.1",
    "ff02::1", "240.0.0.1", "198.18.0.1", "192.0.0.1",
])
def test_non_global_ip_addresses_are_not_public(address):
    assert cr.is_private(address)


def test_assert_public_checks_ip_literals_without_dns():
    with pytest.raises(cr.ScopeError, match="non-public"):
        cr.assert_public("[::ffff:169.254.169.254]")


def test_http_get_rejects_metadata_literal_even_if_dns_is_unavailable(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("non-global IP literal must be rejected before opening a connection")

    monkeypatch.setattr(cr.urllib.request, "build_opener", unexpected)
    with pytest.raises(cr.ScopeError, match="non-public"):
        cr.http_get("http://[::]/", authorized=True)


def test_xss_detection_uses_dedicated_patterns():
    reflected = pattern_engine.match_category("<img src=x>", "xss")
    executed = pattern_engine.match_category('<img src=x onerror="alert(1)">', "xss")

    assert reflected.matches
    assert not reflected.outcome_confirmed
    assert executed.outcome_confirmed


def test_http_get_rejects_unsupported_or_credentialed_urls_before_open(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid URL must be rejected before network access")

    monkeypatch.setattr(cr.urllib.request, "build_opener", unexpected)
    for url in ("file:///etc/passwd", "ftp://example.com/file",
                "https://user:password@example.com/"):
        with pytest.raises(cr.ScopeError, match="invalid or unsupported"):
            cr.http_get(url, authorized=True)
