#!/usr/bin/env python3
"""A deliberately vulnerable local fixture for Setu's Cyber chain tests.

This exists so the chain can be tested end-to-end without pointing it at anyone's
real server. It is a *target*, not a tool: it simulates a few classic flaws so
that recon -> injection -> scoring -> report can be exercised deterministically.

Serves on 127.0.0.1:8731 by default. Prints SQL errors and reflects parameters
verbatim, which is exactly what the injection specialist looks for.

    python3 tests/fixture_server.py            # then, in another shell:
    python3 scripts/cyber_chain.py --target "http://127.0.0.1:8731/item?id=1" \
        --i-have-authorization --allow-private --out-dir runs/fixture
"""
from __future__ import annotations

import html
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8731

INDEX = """<html><head><title>Fixture Store</title>
<script src="/static/app.js"></script></head>
<body><h1>Fixture Store</h1>
<p>Session: PHPSESSID=abc123</p>
<a href="/item?id=1">Item 1</a> | <a href="/admin">Admin</a>
<form action="/search" method="get"><input name="q"></form>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "FixtureApache/2.4.41"      # version banner => misconfig finding

    def log_message(self, fmt, *args):          # keep test output readable
        sys.stderr.write(f"[fixture] {self.address_string()} {fmt % args}\n")

    def _send(self, code: int, body: str, extra: dict | None = None) -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # Deliberately omit CSP / X-Frame-Options / HSTS => header_check findings.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Credentials", "true")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:                    # noqa: N802
        parts = urlsplit(self.path)
        qs = parse_qs(parts.query, keep_blank_values=True)
        path = parts.path

        if path == "/":
            return self._send(200, INDEX)

        if path == "/item":
            item = qs.get("id", [""])[0]
            # 1) SQL error signature when a quote appears
            if "'" in item:
                return self._send(500,
                    "<html><body>Warning: mysqli_query(): You have an error in your SQL "
                    f"syntax near '{html.escape(item)}' at line 1</body></html>")
            # 2) reflection of the parameter (XSS surface)
            return self._send(200, f"<html><body><h2>Item {html.escape(item)}</h2>"
                                   f"<div>You searched for: {item}</div></body></html>")

        if path == "/search":
            q = qs.get("q", [""])[0]
            return self._send(200, f"<html><body>Results for: {q}</body></html>")

        if path == "/admin":
            return self._send(200, "<html><body><h1>Admin panel</h1>"
                                   "<p>Users: admin, alice, bob</p></body></html>")

        if path == "/.env":
            return self._send(200, "DB_PASSWORD=hunter2\nAPI_KEY=AKIAIOSFODNN7EXAMPLE\n")

        if path == "/robots.txt":
            return self._send(200, "User-agent: *\nDisallow: /admin\n")

        if path == "/debug":
            return self._send(200, "debug=true\nversion=1.4.2\n")

        return self._send(404, "<html><body>Not found</body></html>")


def main() -> int:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"fixture target on http://127.0.0.1:{PORT} (Ctrl-C to stop)", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", file=sys.stderr)
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
