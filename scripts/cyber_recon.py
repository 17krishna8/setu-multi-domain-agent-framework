#!/usr/bin/env python3
"""Setu Cyber — safety net + reconnaissance + infrastructure hygiene.

Split out of ``cyber_scan.py`` so that the *safety* code is small enough to
audit in one sitting. Everything a Setu cyber agent does over the network goes
through ``http_get`` here, which means the two hard rules live in exactly one
place:

1. **Authorization gate** — ``assert_authorized`` refuses active testing unless
   the caller passed ``--i-have-authorization``. Passive mode is read-only.
2. **SSRF guard on ourselves** — a recon tool that cheerfully fetches
   ``http://169.254.169.254/`` is a vulnerability with a CLI. Targets that
   resolve into loopback / private / link-local / metadata space are refused
   *after DNS resolution*, so a friendly-looking public hostname that points at
   127.0.0.1 is still caught.

Every function returns findings in the pack's single shape:
``{"finding", "severity", "confidence", "evidence"}`` (+ bookkeeping).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

UA = "Setu-Cyber/0.1 (+authorized-testing; contact the operator)"
PRIVATE_NETS = [
    ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
        "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10", "0.0.0.0/8",
    )
]


class ScopeError(PermissionError):
    """Target is out of scope, unresolvable, or resolves somewhere dangerous."""


# --------------------------------------------------------------------------- #
# safety net
# --------------------------------------------------------------------------- #
def assert_authorized(flag: bool, passive: bool) -> None:
    if not flag and not passive:
        raise ScopeError(
            "Refusing active testing without --i-have-authorization.\n"
            "Setu will not send probe payloads to a host you have not told it you "
            "are allowed to test. Use --passive for read-only recon."
        )


def resolve_host(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ScopeError(f"cannot resolve host '{host}': {e}")
    return sorted({i[4][0] for i in infos})


def is_private(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in PRIVATE_NETS)


def assert_public(host: str, allow_private: bool = False) -> None:
    if allow_private or not host:
        return
    for addr in resolve_host(host):
        if is_private(addr):
            raise ScopeError(
                f"'{host}' resolves to non-public address {addr}. Refusing to fetch.\n"
                "If this is an intentional lab target, pass --allow-private."
            )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Return redirects to the caller so every destination can be revalidated."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(url: str, timeout: float = 10.0, max_redirects: int = 3, *,
             authorized: bool = False, allow_private: bool = False) -> dict:
    """Safely GET an authorized target; validate authorization and every redirect."""
    assert_authorized(authorized, passive=False)
    hops: list[str] = []
    cur = url
    for _ in range(max_redirects + 1):
        parts = urllib.parse.urlsplit(cur)
        host = parts.hostname or ""
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            raise ScopeError(f"refusing invalid or unsupported target URL: {cur}")
        assert_public(host, allow_private)
        req = urllib.request.Request(cur, headers={"user-agent": UA, "accept": "*/*"})
        t0 = time.perf_counter()
        try:
            handlers = [_NoRedirectHandler()]
            if parts.scheme == "https":
                handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
            opener = urllib.request.build_opener(*handlers)
            with opener.open(req, timeout=timeout) as r:
                body = r.read(400_000).decode("utf-8", errors="replace")
                return {"status": r.status, "headers": dict(r.headers), "body": body,
                        "url": cur, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                        "chain": hops}
        except urllib.error.HTTPError as e:
            headers = dict(e.headers or {})
            location = headers.get("Location") or headers.get("location")
            if e.code in {301, 302, 303, 307, 308} and location:
                if len(hops) >= max_redirects:
                    return {"status": 0, "headers": {}, "body": "", "url": cur,
                            "error": "too many redirects", "chain": hops}
                hops.append(cur)
                cur = urllib.parse.urljoin(cur, location)
                continue
            body = e.read(200_000).decode("utf-8", errors="replace")
            return {"status": e.code, "headers": headers, "body": body,
                    "url": cur, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "chain": hops}
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as e:
            return {"status": 0, "headers": {}, "body": "", "url": cur,
                    "error": str(e), "chain": hops}
    return {"status": 0, "headers": {}, "body": "", "url": url, "error": "too many redirects",
            "chain": hops}


# --------------------------------------------------------------------------- #
# RECON  (agent frontmatter uses model: haiku — cheap, high volume)
# --------------------------------------------------------------------------- #
COMMON_SUBS = [
    "www", "api", "app", "admin", "dev", "staging", "test", "mail", "smtp", "vpn",
    "cdn", "static", "assets", "docs", "status", "portal", "internal", "jenkins",
    "git", "ci", "dashboard", "auth", "sso", "beta", "demo", "blog", "shop",
    "pay", "payments", "backup", "db", "ftp", "remote", "gateway", "files",
]


def subdomain_enum(domain: str, wordlist: list[str] | None = None,
                   passive: bool = True, allow_private: bool = False,
                   timeout: float = 2.0, authorized: bool = False) -> list[dict]:
    """DNS-first enumeration. Passive mode never opens an HTTP connection.

    This is the cheap 80% of coverage. Pair it with ``ct_subdomains`` (crt.sh)
    and a real resolver for production use; the orchestrator does both.
    """
    if not passive:
        assert_authorized(authorized, passive=False)
    domain = domain.strip().lower().lstrip(".")
    out: list[dict] = []
    seen: set[str] = set()

    def check(host: str) -> None:
        if host in seen:
            return
        seen.add(host)
        try:
            addrs = resolve_host(host)
        except ScopeError:
            return
        private = [a for a in addrs if is_private(a)]
        if private and not allow_private:
            # Interesting on its own: a public subdomain pointing inward.
            out.append(common.finding(
                finding=f"Subdomain '{host}' resolves to non-public address {private[0]} "
                        f"— possible proxy or DNS-rebinding surface.",
                severity="medium", confidence=0.7,
                evidence={"host": host, "addresses": addrs,
                          "note": "Not fetched: passive mode never connects."},
                specialist="cyber-subdomain-enum", target=domain,
                ftype="exposed-internal-service"))
            return
        ev = {"host": host, "addresses": addrs}
        if not passive:
            r = http_get(f"https://{host}/", timeout=timeout,
                         authorized=authorized, allow_private=allow_private)
            if r.get("status"):
                ev["http_status"] = r["status"]
                ev["server"] = r["headers"].get("Server")
        out.append(common.finding(
            finding=f"Live subdomain discovered: {host} -> {', '.join(addrs)}",
            severity="info", confidence=0.9, evidence=ev,
            specialist="cyber-subdomain-enum", target=domain, ftype="attack-surface"))

    check(domain)
    for w in (wordlist or COMMON_SUBS):
        check(f"{w}.{domain}")
    return out


def ct_subdomains(domain: str, timeout: float = 12.0) -> list[str]:
    """Certificate-transparency lookup via crt.sh — passive and high yield.

    Fails *loudly*: if crt.sh is down you get a warning and an empty list,
    never a silent empty list that a report would read as "no subdomains exist".
    """
    url = f"https://crt.sh/?q=%25.{urllib.parse.quote(domain)}&output=json"
    req = urllib.request.Request(url, headers={"user-agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rows = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001 — third-party service, any failure is expected
        print(f"warning: crt.sh unavailable ({e}); CT lookup skipped", file=sys.stderr)
        return []
    hosts: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        for name in str(row.get("name_value", "")).splitlines():
            name = name.strip().lower().lstrip("*.")
            if name.endswith(domain) and name != domain:
                hosts.add(name)
    return sorted(hosts)


TECH_SIGNATURES = [
    (r'wp-content|wp-includes|/wp-json/', "WordPress"),
    (r'Drupal\.settings|sites/default/files', "Drupal"),
    (r'/media/system/js/|Joomla!', "Joomla"),
    (r'__NEXT_DATA__|/_next/static/', "Next.js"),
    (r'data-reactroot|react(-dom)?[.@]', "React"),
    (r'__VUE__|vue(\.runtime)?\.js', "Vue"),
    (r'ng-version=|ng-app=', "Angular"),
    (r'csrfmiddlewaretoken|__admin_media_prefix__', "Django"),
    (r'csrf-token" content=|X-Runtime', "Ruby on Rails"),
    (r'laravel_session', "Laravel"),
    (r'X-Powered-By: Express', "Express"),
    (r'PHPSESSID', "PHP"),
    (r'__VIEWSTATE|ASP\.NET', "ASP.NET"),
    (r'cdn\.shopify\.com|Shopify\.theme', "Shopify"),
    (r'cf-ray|cloudflare', "Cloudflare"),
    (r'\bnginx\b', "nginx"),
    (r'\bApache\b', "Apache httpd"),
]


def tech_fingerprint(url: str, allow_private: bool = False,
                     authorized: bool = False) -> list[dict]:
    assert_authorized(authorized, passive=False)
    assert_public(urllib.parse.urlsplit(url).hostname or "", allow_private)
    r = http_get(url, authorized=authorized, allow_private=allow_private)
    if not r.get("status"):
        return [common.finding(
            finding=f"Target unreachable: {r.get('error')}", severity="info", confidence=0.9,
            evidence={"url": url, "error": r.get("error")},
            specialist="cyber-tech-fingerprint", target=url, ftype="recon")]
    haystack = r["body"][:60_000] + "\n" + "\n".join(f"{k}: {v}" for k, v in r["headers"].items())
    found = [label for regex, label in TECH_SIGNATURES if re.search(regex, haystack, re.I)]
    banners = {}
    for h in ("server", "x-powered-by", "x-generator", "via", "x-aspnet-version"):
        for k, v in r["headers"].items():
            if k.lower() == h:
                banners[k] = v
    return [common.finding(
        finding=f"Technology stack: {', '.join(found) or 'not identified'}",
        severity="info", confidence=0.8 if found else 0.3,
        evidence={"url": r["url"], "status": r["status"], "tech": found,
                  "banner_headers": banners,
                  "note": "Fingerprinting is inference. Confirm before a report asserts it."},
        specialist="cyber-tech-fingerprint", target=url, ftype="recon")]


PATH_PROBES = [
    "/.git/HEAD", "/.env", "/admin", "/api", "/api/v1", "/swagger.json",
    "/openapi.json", "/robots.txt", "/sitemap.xml", "/server-status", "/phpinfo.php",
    "/.well-known/security.txt", "/actuator/health", "/graphql", "/debug",
]

INTERESTING_PATHS = {
    "/.git/HEAD": ("high", "Exposed .git directory — source disclosure", 0.9),
    "/.env": ("critical", "Exposed .env file — possible secret disclosure", 0.85),
    "/server-status": ("medium", "Apache server-status exposed", 0.8),
    "/phpinfo.php": ("high", "phpinfo() exposed — full environment disclosure", 0.9),
    "/actuator/health": ("medium", "Spring Actuator endpoint exposed", 0.7),
    "/swagger.json": ("low", "API schema exposed (aids attacker, not a break alone)", 0.6),
    "/openapi.json": ("low", "API schema exposed (informational)", 0.6),
    "/admin": ("low", "Admin panel reachable without an auth redirect", 0.4),
    "/debug": ("medium", "Debug endpoint reachable", 0.5),
    "/graphql": ("low", "GraphQL endpoint present — test introspection", 0.5),
}


def endpoint_discovery(url: str, paths: list[str] | None = None,
                       allow_private: bool = False, timeout: float = 6.0,
                       authorized: bool = False) -> list[dict]:
    assert_authorized(authorized, passive=False)
    parts = urllib.parse.urlsplit(url)
    assert_public(parts.hostname or "", allow_private)
    base = f"{parts.scheme}://{parts.netloc}"
    out: list[dict] = []

    # Soft-404 suppression: measure the baseline noise before trusting a 200.
    baseline = http_get(base + "/setu-nonexistent-" + os.urandom(4).hex(), timeout=timeout,
                        authorized=authorized, allow_private=allow_private)
    base_status, base_len = baseline.get("status", 404), len(baseline.get("body", ""))

    for p in (paths or PATH_PROBES):
        r = http_get(base + p, timeout=timeout, authorized=authorized,
                     allow_private=allow_private)
        if not r.get("status"):
            continue
        if (r["status"] == 200 and base_status == 200
                and abs(len(r["body"]) - base_len) < 16):
            continue    # identical to the 404 page => wildcard route, ignore
        sev, desc, conf = INTERESTING_PATHS.get(
            p, ("info", f"Path reachable (HTTP {r['status']})", 0.5))
        if r["status"] == 403 and p in INTERESTING_PATHS:
            desc += " (403 = exists but forbidden)"
        out.append(common.finding(
            finding=f"{p} -> HTTP {r['status']}: {desc}",
            severity=sev if r["status"] in (200, 500) else "info",
            confidence=conf if r["status"] in (200, 500) else round(conf * 0.6, 2),
            evidence={"path": p, "status": r["status"], "length": len(r["body"]),
                      "snippet": r["body"][:200] if p in ("/.env", "/.git/HEAD") else ""},
            specialist="cyber-endpoint-discovery", target=base, ftype="exposed-endpoint"))
    return out


def osint(domain: str) -> list[dict]:
    """Passive OSINT: mail posture + CT hostnames. Sends nothing to the target."""
    out: list[dict] = []
    spf = ""
    try:
        import shutil
        import subprocess
        if shutil.which("dig"):
            spf = subprocess.run(["dig", "+short", "TXT", domain], capture_output=True,
                                 text=True, timeout=6).stdout
    except Exception:  # noqa: BLE001
        spf = ""
    if "v=spf1" not in spf:
        out.append(common.finding(
            finding=f"No SPF record observed for {domain} — sender forgery is easier.",
            severity="low", confidence=0.5,
            evidence={"domain": domain, "spf_raw": spf[:300],
                      "caveat": "Absence detected via a short dig; re-verify before reporting."},
            specialist="cyber-osint", target=domain, ftype="email-spoofing"))
    try:
        socket.getaddrinfo(f"_dmarc.{domain}", None)
        dmarc_found = True
    except socket.gaierror:
        dmarc_found = False
    if not dmarc_found:
        out.append(common.finding(
            finding=f"No DMARC record observed for {domain}.",
            severity="low", confidence=0.5,
            evidence={"domain": domain, "checked": f"_dmarc.{domain}"},
            specialist="cyber-osint", target=domain, ftype="email-spoofing"))
    hosts = ct_subdomains(domain)
    if hosts:
        out.append(common.finding(
            finding=f"{len(hosts)} hostname(s) found in certificate transparency.",
            severity="info", confidence=0.95,
            evidence={"hosts": hosts[:50], "source": "crt.sh", "truncated": len(hosts) > 50},
            specialist="cyber-osint", target=domain, ftype="attack-surface"))
    return out


# --------------------------------------------------------------------------- #
# INFRASTRUCTURE HYGIENE
# --------------------------------------------------------------------------- #
SECURITY_HEADERS = {
    "content-security-policy": ("medium", "No CSP — reflected XSS executes unmitigated"),
    "x-content-type-options": ("low", "No nosniff — MIME sniffing possible"),
    "x-frame-options": ("medium", "No framing protection — clickjacking possible"),
    "strict-transport-security": ("medium", "No HSTS — downgrade/SSL-strip possible"),
    "referrer-policy": ("low", "No Referrer-Policy — URL leakage to third parties"),
    "permissions-policy": ("low", "No Permissions-Policy"),
}


def header_check(url: str, allow_private: bool = False,
                 authorized: bool = False) -> list[dict]:
    assert_authorized(authorized, passive=False)
    assert_public(urllib.parse.urlsplit(url).hostname or "", allow_private)
    r = http_get(url, authorized=authorized, allow_private=allow_private)
    if not r.get("status"):
        return []
    hdrs = {k.lower(): v for k, v in r["headers"].items()}
    out: list[dict] = []
    for name, (sev, desc) in SECURITY_HEADERS.items():
        if name not in hdrs:
            out.append(common.finding(
                finding=f"Missing {name}: {desc}", severity=sev, confidence=0.95,
                evidence={"header": name, "observed_headers": sorted(hdrs)},
                specialist="cyber-cloud-misconfig", target=url,
                ftype="misconfiguration", cwe="CWE-693"))
    server = hdrs.get("server", "")
    # Grab just the product/version token ("Apache/2.4.41"), not the whole header,
    # which many servers pad with OS and module noise.
    banner = re.search(r"[A-Za-z][\w.\-]*/\d+(?:\.\d+)+", server)
    if banner:
        out.append(common.finding(
            finding=f"Server banner discloses version: {banner.group(0)}",
            severity="low", confidence=0.9, evidence={"server": server},
            specialist="cyber-cloud-misconfig", target=url,
            ftype="information-disclosure", cwe="CWE-200"))
    acao = hdrs.get("access-control-allow-origin", "")
    if acao == "*" and "access-control-allow-credentials" in hdrs:
        out.append(common.finding(
            finding="CORS allows any origin with credentials — cross-site data theft.",
            severity="high", confidence=0.85,
            evidence={"acao": acao,
                      "credentials": hdrs.get("access-control-allow-credentials")},
            specialist="cyber-cloud-misconfig", target=url, ftype="cors", cwe="CWE-942"))
    elif acao and "null" in acao.lower():
        out.append(common.finding(
            finding="CORS reflects a 'null' origin — sandboxed-iframe bypass.",
            severity="medium", confidence=0.7, evidence={"acao": acao},
            specialist="cyber-cloud-misconfig", target=url, ftype="cors", cwe="CWE-942"))
    if url.startswith("http://"):
        out.append(common.finding(
            finding="Target served over plaintext HTTP with no redirect to HTTPS.",
            severity="medium", confidence=0.9,
            evidence={"url": url, "status": r["status"]},
            specialist="cyber-cloud-misconfig", target=url, ftype="transport", cwe="CWE-319"))
    return out


def _selftest() -> int:
    print("private nets loaded:", len(PRIVATE_NETS))
    print("is_private(127.0.0.1):", is_private("127.0.0.1"))
    print("is_private(169.254.169.254):", is_private("169.254.169.254"))
    print("is_private(8.8.8.8):", is_private("8.8.8.8"))
    try:
        assert_authorized(False, passive=False)
        print("AUTH GATE: FAILED")
        return 1
    except ScopeError as e:
        print("auth gate: OK ->", str(e).splitlines()[0])
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("import me from cyber_scan.py; try: python3 cyber_scan.py --help")
