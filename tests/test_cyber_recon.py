"""Safety tests for Cyber's authorization and redirect boundaries."""
from __future__ import annotations

import io
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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

    result = cr.http_get("https://public.example/start", authorized=True)

    assert result["status"] == 0
    assert "redirect" in result["error"]
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

    def resolve_only_original(host):
        if host != "public.example":
            pytest.fail(f"out-of-scope redirect must not trigger DNS lookup: {host}")
        return ["93.184.216.34"]

    monkeypatch.setattr(cr, "resolve_host", resolve_only_original)
    monkeypatch.setattr(cr.urllib.request, "build_opener", lambda *handlers: CrossOriginRedirect())
    result = cr.http_get("https://public.example/start", authorized=True, allow_private=True)

    assert result["status"] == 0
    assert "outside the authorized origin" in result["error"]
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

    result = cr.http_get("https://public.example/start", authorized=True)

    assert result["status"] == 0
    assert "downgrade" in result["error"]
    assert opened == ["https://public.example/start"]


def test_http_get_allows_same_host_http_to_https_upgrade(monkeypatch):
    opened = []

    class UpgradeThenOk:
        def open(self, request, timeout):
            opened.append(request.full_url)
            if len(opened) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 301, "Moved", {"Location": "https://public.example/secure"},
                    io.BytesIO(b""),
                )
            class Response(io.BytesIO):
                status = 200
                headers = {}
            return Response(b"ok")

    monkeypatch.setattr(cr, "resolve_host", lambda host: ["93.184.216.34"])
    monkeypatch.setattr(cr.urllib.request, "build_opener", lambda *handlers: UpgradeThenOk())

    result = cr.http_get("http://public.example/start", authorized=True)

    assert result["status"] == 200
    assert opened == ["http://public.example/start", "https://public.example/secure"]


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
    event_handler = pattern_engine.match_category('<img src=x onerror="alert(1)">', "xss")

    assert reflected.matches
    assert reflected.confidence >= 0.45
    assert not reflected.outcome_confirmed
    assert event_handler.matches
    assert not event_handler.outcome_confirmed


def test_json_reflection_of_xss_payload_stays_low_severity(monkeypatch):
    payload = '<svg onload=alert(1)>'
    monkeypatch.setattr(cr, "assert_public", lambda *args, **kwargs: None)
    monkeypatch.setattr(cr, "http_get", lambda *args, **kwargs: {
        "status": 200, "headers": {"Content-Type": "application/json"},
        "body": '{"value": "' + payload + '"}', "url": args[0], "chain": [],
    })

    findings = cyber_scan.inject_params(
        "https://example.com/?q=original", probes={"xss": [payload]}, authorized=True,
    )

    assert len(findings) == 1
    assert findings[0]["severity"] == "low"
    assert findings[0]["outcome_confirmed"] is False


def test_http_get_pins_connection_to_the_validated_dns_answer(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pinned")

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    resolutions = []

    def changing_dns(host):
        resolutions.append(host)
        return ["127.0.0.1"] if len(resolutions) == 1 else ["169.254.169.254"]

    monkeypatch.setattr(cr, "resolve_host", changing_dns)
    try:
        url = f"http://rebind.example:{server.server_address[1]}/"
        result = cr.http_get(url, authorized=True, allow_private=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result["status"] == 200
    assert result["body"] == "pinned"
    assert resolutions == ["rebind.example"]


def test_http_get_follows_real_relative_redirect_and_case_insensitive_location():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("LOCATION", "/final")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"reached")

        def log_message(self, fmt, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/redirect"
        result = cr.http_get(url, authorized=True, allow_private=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result["status"] == 200
    assert result["body"] == "reached"
    assert result["url"].endswith("/final")
    assert result["chain"] == [url]


def test_http_get_rejects_unsupported_or_credentialed_urls_before_open(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("invalid URL must be rejected before network access")

    monkeypatch.setattr(cr.urllib.request, "build_opener", unexpected)
    for url in ("file:///etc/passwd", "ftp://example.com/file",
                "https://user:password@example.com/"):
        with pytest.raises(cr.ScopeError, match="invalid or unsupported"):
            cr.http_get(url, authorized=True)
