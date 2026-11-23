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

import http.client
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
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16",
        "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4",
        "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96",
        "100::/64", "2001::/23", "2001:db8::/32", "2002::/16",
        "64:ff9b:1::/48", "::/96", "fec0::/10",
        "fc00::/7", "fe80::/10", "ff00::/8",
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
    """Return true for non-global addresses, including mapped/transition IPv6."""
    try:
        ip = ipaddress.ip_address(addr.strip("[]"))
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return is_private(str(ip.ipv4_mapped))
        if ip.sixtofour:
            return is_private(str(ip.sixtofour))
        if ip.teredo:
            server, client = ip.teredo
            return is_private(str(server)) or is_private(str(client))
    return not ip.is_global or any(ip in net for net in PRIVATE_NETS)


def assert_public(host: str, allow_private: bool = False) -> list[str]:
    """Resolve once, enforce address policy, and return IPs to pin at connect time."""
    if not host:
        raise ScopeError("refusing target without a hostname")
    literal = host.strip("[]")
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        addresses = resolve_host(host)
    else:
        addresses = [literal]
    if not addresses:
        raise ScopeError(f"cannot resolve host '{host}': no addresses returned")
    if not allow_private:
        for addr in addresses:
            if is_private(addr):
                raise ScopeError(
                    f"'{host}' resolves to non-public address {addr}. Refusing to fetch.\n"
                    "If this is an intentional lab target, pass --allow-private."
                )
    return addresses


def _pinned_handlers(addresses: list[str]):
    """Build direct HTTP handlers that connect only to previously checked IPs."""
    class PinnedHTTPConnection(http.client.HTTPConnection):
        def connect(self):
            errors = []
            for address in addresses:
                try:
                    self.sock = socket.create_connection(
                        (address, self.port), self.timeout, self.source_address)
                    break
                except OSError as e:
                    errors.append(e)
            else:
                raise errors[-1]
            if self._tunnel_host:
                self._tunnel()

    class PinnedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            errors = []
            for address in addresses:
                try:
                    self.sock = socket.create_connection(
                        (address, self.port), self.timeout, self.source_address)
                    break
                except OSError as e:
                    errors.append(e)
            else:
                raise errors[-1]
            if self._tunnel_host:
                self._tunnel()
            server_hostname = self._tunnel_host or self.host
            self.sock = self._context.wrap_socket(self.sock, server_hostname=server_hostname)

    class PinnedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(PinnedHTTPConnection, req)

    class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(PinnedHTTPSConnection, req,
                                context=self._context, check_hostname=self._check_hostname)

    return PinnedHTTPHandler(), PinnedHTTPSHandler(context=ssl.create_default_context())


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
    try:
        initial = urllib.parse.urlsplit(url)
        initial_port = initial.port or (443 if initial.scheme == "https" else 80)
    except ValueError as e:
        raise ScopeError(f"refusing invalid target URL: {url}: {e}") from e
    initial_host = (initial.hostname or "").rstrip(".").lower()
    if initial.scheme not in {"http", "https"} or not initial_host or \
            initial.username or initial.password:
        raise ScopeError(f"refusing invalid or unsupported target URL: {url}")
    pinned_addresses = assert_public(initial_host, allow_private)
    for _ in range(max_redirects + 1):
        try:
            parts = urllib.parse.urlsplit(cur)
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError as e:
            if hops:
                return {"status": 0, "headers": {}, "body": "", "url": cur,
                        "error": f"invalid redirect URL: {e}", "chain": hops + [cur]}
            raise ScopeError(f"refusing invalid target URL: {cur}: {e}") from e
        host = (parts.hostname or "").rstrip(".").lower()
        if parts.scheme not in {"http", "https"} or not host or parts.username or parts.password:
            if hops:
                return {"status": 0, "headers": {}, "body": "", "url": cur,
                        "error": "invalid or unsupported redirect URL", "chain": hops + [cur]}
            raise ScopeError(f"refusing invalid or unsupported target URL: {cur}")
        if initial.scheme == "https" and parts.scheme != "https":
            return {"status": 0, "headers": {}, "body": "", "url": cur,
                    "error": "HTTPS downgrade redirect refused", "chain": hops + [cur]}
        upgrade = (initial.scheme == "http" and parts.scheme == "https"
                   and initial_port == 80 and port == 443)
        if host != initial_host or (port != initial_port and not upgrade):
            # Refuse without resolving attacker-controlled redirect hosts.
            return {"status": 0, "headers": {}, "body": "", "url": cur,
                    "error": "redirect outside the authorized origin", "chain": hops + [cur]}
        try:
            req = urllib.request.Request(cur, headers={"user-agent": UA, "accept": "*/*"})
        except (TypeError, ValueError) as e:
            if hops:
                return {"status": 0, "headers": {}, "body": "", "url": cur,
                        "error": f"invalid redirect URL: {e}", "chain": hops + [cur]}
            raise ScopeError(f"refusing invalid target URL: {e}") from e
        t0 = time.perf_counter()
        try:
            handlers = [urllib.request.ProxyHandler({}), _NoRedirectHandler(),
                        *_pinned_handlers(pinned_addresses)]
            opener = urllib.request.build_opener(*handlers)
            with opener.open(req, timeout=timeout) as r:
                body = r.read(400_000).decode("utf-8", errors="replace")
                return {"status": r.status, "headers": dict(r.headers), "body": body,
                        "url": cur, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                        "chain": hops}
        except urllib.error.HTTPError as e:
            # HTTP header lookup is case-insensitive on the original message;
            # converting it to a plain dict first loses that guarantee.
            location = e.headers.get("Location") if e.headers else None
            headers = dict(e.headers or {})
            if e.code in {301, 302, 303, 307, 308} and location:
                if len(hops) >= max_redirects:
                    return {"status": 0, "headers": {}, "body": "", "url": cur,
                            "error": "too many redirects", "chain": hops}
                hops.append(cur)
                try:
                    cur = urllib.parse.urljoin(cur, location)
                except (TypeError, ValueError) as e:
                    return {"status": 0, "headers": {}, "body": "", "url": cur,
                            "error": f"invalid redirect Location: {e}", "chain": hops}
                continue
            body = e.read(200_000).decode("utf-8", errors="replace")
            return {"status": e.code, "headers": headers, "body": body,
                    "url": cur, "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "chain": hops}
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, ConnectionError) as e:
            return {"status": 0, "headers": {}, "body": "", "url": cur,
                    "error": str(e), "chain": hops}
        except (http.client.InvalidURL, ValueError) as e:
            if hops:
                return {"status": 0, "headers": {}, "body": "", "url": cur,
                        "error": f"invalid redirect URL: {e}", "chain": hops + [cur]}
            raise ScopeError(f"refusing invalid target URL: {e}") from e
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
    domain = domain.strip().lower().rstrip(".")
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
            if name == domain or name.endswith("." + domain):
                if name != domain:
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


def _request_coverage_note(stage: str, url: str, error: str | None) -> dict:
    return common.finding(
        finding=f"{stage} was not fully tested because the request did not complete.",
        severity="info", confidence=1.0,
        evidence={"url": url, "error": error or "request returned no status",
                  "result": "incomplete; no negative security conclusion"},
        specialist="cyber-coverage", target=url, ftype="coverage-note")


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
    if not base_status:
        out.append(_request_coverage_note("Endpoint discovery baseline", base,
                                          baseline.get("error")))

    for p in (paths or PATH_PROBES):
        r = http_get(base + p, timeout=timeout, authorized=authorized,
                     allow_private=allow_private)
        if not r.get("status"):
            out.append(_request_coverage_note(f"Endpoint probe {p}", base + p,
                                              r.get("error")))
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


def _query_txt_records(name: str) -> list[str] | None:
    """Query TXT records using common system tools; None means unavailable/failed."""
    import shutil
    import subprocess

    if shutil.which("dig"):
        try:
            result = subprocess.run(["dig", "+noall", "+comments", "+answer", "TXT", name],
                                    capture_output=True, text=True, timeout=6, check=False)
            status = re.search(r"status:\s*(\w+)", result.stdout, re.I)
            if result.returncode == 0 and status:
                rcode = status.group(1).upper()
                if rcode in {"NOERROR", "NXDOMAIN"}:
                    records = []
                    for line in result.stdout.splitlines():
                        if re.search(r"\sIN\s+TXT\s", line, re.I):
                            chunks = re.findall(r'"([^"]*)"', line)
                            if chunks:
                                records.append("".join(chunks))
                    # NOERROR with no TXT answer (NODATA) and NXDOMAIN are both
                    # definitive absence for this name/type. Other rcodes are unknown.
                    return records
                return None
        except (OSError, subprocess.SubprocessError):
            pass

    if shutil.which("nslookup"):
        try:
            result = subprocess.run(["nslookup", "-type=TXT", name], capture_output=True,
                                    text=True, timeout=6, check=False)
            output = result.stdout + "\n" + result.stderr
            if re.search(r"non-existent domain|NXDOMAIN", output, re.I):
                return []
            if re.search(r"timed out|no response|server failure|SERVFAIL|REFUSED", output, re.I):
                return None
            if result.returncode == 0:
                quoted = re.findall(r'"([^"]*)"', output)
                if quoted:
                    return quoted
                if re.search(r"no answer|no records?", output, re.I):
                    return []
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def osint(domain: str) -> list[dict]:
    """Passive OSINT: mail posture + CT hostnames. Sends nothing to the target."""
    out: list[dict] = []
    checks = (("SPF", domain, "v=spf1"),
              ("DMARC", f"_dmarc.{domain}", "v=DMARC1"))
    for label, name, marker in checks:
        records = _query_txt_records(name)
        matching = [record for record in records or []
                    if record.lstrip().lower().startswith(marker.lower())]
        if records is None:
            out.append(common.finding(
                finding=f"{label} TXT record could not be checked for {domain}.",
                severity="info", confidence=1.0,
                evidence={"domain": domain, "checked": name,
                          "reason": "no supported TXT lookup tool or DNS query failed",
                          "result": "unknown; no absence claim made"},
                specialist="cyber-osint", target=name, ftype="coverage-note"))
        elif not matching:
            out.append(common.finding(
                finding=f"No {label} record observed for {domain}.",
                severity="low", confidence=0.5,
                evidence={"domain": domain, "checked": name, "txt_records": records[:10],
                          "caveat": "TXT lookup found no matching record; verify before reporting."},
                specialist="cyber-osint", target=name, ftype="email-spoofing"))
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
        return [_request_coverage_note("Security-header check", url, r.get("error"))]
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
    if (r.get("url") or url).startswith("http://"):
        out.append(common.finding(
            finding="Target served over plaintext HTTP with no redirect to HTTPS.",
            severity="medium", confidence=0.9,
            evidence={"url": r.get("url") or url, "redirect_chain": r.get("chain", []),
                      "status": r["status"]},
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
