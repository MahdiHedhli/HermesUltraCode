"""Read-only HTTP API + dashboard, mirroring the Hermes web-dashboard security
conventions so it can sit beside ``hermes dashboard`` (default port 9120).

Security (criterion 9), all enforced here in ``authorize`` before any data leaves:
  * bind 127.0.0.1 (loopback) by default,
  * an ephemeral session token required in the ``X-Gate-Session-Token`` header on
    every ``/api/*`` route,
  * CORS restricted to localhost origins (no wildcard),
  * Host-header validation against an allowlist to defend against DNS rebinding,
  * secret redaction on any config surfaced,
  * if bound to a non-loopback host, the auth gate stays engaged (token required)
    and startup refuses a blank token.

neckbeard: stdlib ``http.server`` — no Flask/FastAPI dependency (rung 2: stdlib does
it). The routing is a pure function over (method, path, headers) so the security
controls are unit-testable without opening a socket. Upgrade path: a real ASGI app
(FastAPI + uvicorn) and the React 19 + Vite + Tailwind SPA in ``web/`` if these views
outgrow server-rendered HTML.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from core.config import ReadApiConfig, ensure_session_token
from core.redact import redact_obj
from core.store import AuditFilter, AuditStore
from server import views

log = logging.getLogger("hermesultracode.read_api")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_DIR = os.path.join(REPO_ROOT, "web")
TOKEN_HEADER = "x-gate-session-token"

_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


@dataclass
class ReadApiContext:
    """Everything the router needs. Storage and config behind interfaces."""

    store: AuditStore
    config: ReadApiConfig
    repo_root: str = REPO_ROOT
    web_dir: str = WEB_DIR
    benchmark: dict | None = None
    surfaced_config: dict | None = None  # redacted before exposure
    live_source: views.LiveSource = views.default_live_source
    queue_source: views.QueueSource = views.default_queue_source


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


def _json(obj: Any, status: int = 200, extra: dict | None = None) -> Response:
    body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(status, body, "application/json; charset=utf-8", extra or {})


def _err(status: int, message: str) -> Response:
    return _json({"error": message}, status=status)


# ---------------------------------------------------------------------------
# Security middleware
# ---------------------------------------------------------------------------


def _allowed_origins(cfg: ReadApiConfig) -> set[str]:
    port = cfg.port
    origins = {
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
    }
    if cfg.host not in ("127.0.0.1", "localhost", "::1"):
        origins.add(f"http://{cfg.host}:{port}")
    return origins


def _host_ok(cfg: ReadApiConfig, host_header: str) -> bool:
    if not host_header:
        return False
    allowed = set(cfg.allowed_hosts) or {
        f"{cfg.host}:{cfg.port}",
        f"localhost:{cfg.port}",
        f"127.0.0.1:{cfg.port}",
    }
    # Compare host:port exactly (DNS-rebinding defense: an attacker page resolves to
    # 127.0.0.1 but carries its own Host header, which won't be in the allowlist).
    return host_header in allowed


def authorize(ctx: ReadApiContext, method: str, path: str, headers: dict[str, str]) -> Response | None:
    """Return a Response to short-circuit (deny), or None to allow.

    Host validation applies to ALL routes. Token applies to ``/api/*`` only — the
    shell HTML and static assets carry no data, and the user supplies the token to
    the shell after reading it from the server's startup banner."""
    h = {k.lower(): v for k, v in headers.items()}

    # 1. Host-header validation everywhere (DNS rebinding defense).
    if not _host_ok(ctx.config, h.get("host", "")):
        return _err(403, "host header not allowed")

    # 2. Token required for data routes.
    if path.startswith("/api/"):
        supplied = h.get(TOKEN_HEADER, "")
        expected = ctx.config.session_token
        if not expected:
            # Non-loopback or any deployment must have a token; blank = locked.
            return _err(503, "read API has no session token configured (locked)")
        if not hmac.compare_digest(supplied, expected):
            return _err(401, "missing or invalid session token")
    return None


def cors_headers(ctx: ReadApiContext, origin: str | None) -> dict[str, str]:
    """CORS restricted to localhost origins; never a wildcard."""
    allowed = _allowed_origins(ctx.config)
    out = {"Vary": "Origin"}
    if origin and origin in allowed:
        out["Access-Control-Allow-Origin"] = origin
        out["Access-Control-Allow-Headers"] = "X-Gate-Session-Token, Content-Type"
        out["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    return out


# ---------------------------------------------------------------------------
# Routing (pure)
# ---------------------------------------------------------------------------


def handle(ctx: ReadApiContext, method: str, raw_path: str, headers: dict[str, str]) -> Response:
    """Pure request handler: (method, path, headers) -> Response. No socket needed."""
    parsed = urlparse(raw_path)
    path = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)
    h = {k.lower(): v for k, v in headers.items()}
    origin = h.get("origin")
    cors = cors_headers(ctx, origin)

    # Host-header validation applies to ALL routes AND methods (incl. OPTIONS) — the
    # DNS-rebinding defense must run before any early return.
    if not _host_ok(ctx.config, h.get("host", "")):
        r = _err(403, "host header not allowed")
        r.headers.update(cors)
        return r

    if method == "OPTIONS":
        return Response(204, b"", "text/plain", cors)

    if method != "GET":
        r = _err(405, "read-only API: only GET is allowed")
        r.headers.update(cors)
        return r

    denied = authorize(ctx, method, path, headers)
    if denied is not None:
        denied.headers.update(cors)
        return denied

    try:
        resp = _route(ctx, path, query)
    except FileNotFoundError:
        resp = _err(404, "not found")
    except Exception as exc:  # noqa: BLE001
        log.exception("read_api.error")
        resp = _err(500, f"internal error: {exc}")
    resp.headers.update(cors)
    return resp


def _route(ctx: ReadApiContext, path: str, query: dict) -> Response:
    store = ctx.store

    if path == "/" or path == "/dashboard":
        return _static("dashboard.html", ctx.web_dir)
    if path == "/healthz":
        return _json({"status": "ok"})
    if path.startswith("/static/"):
        name = os.path.basename(path[len("/static/"):])
        return _static(name, ctx.web_dir)

    if path == "/api/live":
        return _json(ctx.live_source(store))
    if path == "/api/queue":
        return _json({"queue": ctx.queue_source(store)})
    if path == "/api/metrics":
        return _json(views.compute_metrics(store, ctx.benchmark))
    if path == "/api/neckbeard":
        return _json(views.neckbeard_view(store, ctx.repo_root))
    if path == "/api/config":
        # Secret redaction on any config surfaced (criterion 9).
        return _json(redact_obj(ctx.surfaced_config or {}))
    if path == "/api/failclosed":
        return _json({"fail_closed_count": store.count_fail_closed()})

    if path.startswith("/api/dispatch/"):
        rid = path[len("/api/dispatch/"):]
        rec = store.get(rid)
        if rec is None:
            return _err(404, "dispatch not found")
        return _json(views.gate_panel(rec))

    if path == "/api/audit" or path == "/api/audit.json" or path == "/api/audit.csv":
        flt = _audit_filter(query)
        if path == "/api/audit.csv":
            body = store.export_csv(flt).encode("utf-8")
            return Response(200, body, "text/csv; charset=utf-8",
                            {"Content-Disposition": "attachment; filename=gate_audit.csv"})
        if path == "/api/audit.json":
            body = store.export_json(flt).encode("utf-8")
            return Response(200, body, "application/json; charset=utf-8",
                            {"Content-Disposition": "attachment; filename=gate_audit.json"})
        return _json({"rows": [views.record_summary(r) for r in store.query(flt)]})

    return _err(404, "not found")


def _audit_filter(query: dict) -> AuditFilter:
    def one(key):
        v = query.get(key)
        return v[0] if v else None

    fc = one("fail_closed")
    return AuditFilter(
        tier=one("tier"),
        verdict=one("verdict"),
        decision=one("decision"),
        fail_closed=(fc == "1" or fc == "true") if fc is not None else None,
        ts_from=one("from"),
        ts_to=one("to"),
        limit=int(one("limit")) if one("limit") else None,
    )


def _static(name: str, web_dir: str) -> Response:
    # Defense in depth against path traversal: basename only, must stay in web_dir.
    safe = os.path.basename(name)
    full = os.path.join(web_dir, safe)
    if os.path.commonpath([os.path.abspath(full), os.path.abspath(web_dir)]) != os.path.abspath(web_dir):
        raise FileNotFoundError(name)
    if not os.path.isfile(full):
        raise FileNotFoundError(name)
    ext = os.path.splitext(safe)[1].lower()
    with open(full, "rb") as fh:
        body = fh.read()
    return Response(200, body, _STATIC_TYPES.get(ext, "application/octet-stream"))


# ---------------------------------------------------------------------------
# HTTP server shell
# ---------------------------------------------------------------------------


def make_handler(ctx: ReadApiContext) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        server_version = "HermesUltraCodeReadAPI/1.0"

        def _dispatch(self) -> None:
            headers = {k: v for k, v in self.headers.items()}
            resp = handle(ctx, self.command, self.path, headers)
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(resp.body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in resp.headers.items():
                self.send_header(k, v)
            self.end_headers()
            if resp.body:
                self.wfile.write(resp.body)

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._dispatch()

        def log_message(self, fmt, *args) -> None:  # quiet — DEBUG so dashboard polling
            # doesn't flood the Hermes log (the UI hits /api/* every few seconds).
            # NB: 'msg' is a reserved LogRecord field — never put it in `extra`.
            log.debug("read_api.request", extra={"event": "request", "request_line": fmt % args})

    return _Handler


def serve(ctx: ReadApiContext) -> ThreadingHTTPServer:
    """Create (but do not block on) the HTTP server. Caller runs ``serve_forever``."""
    cfg = ctx.config
    if not cfg.is_loopback and cfg.require_auth_when_nonloopback and not cfg.session_token:
        raise RuntimeError(
            "refusing to bind a non-loopback host without a session token (fail closed)"
        )
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), make_handler(ctx))
    return httpd


def run(ctx: ReadApiContext) -> None:  # pragma: no cover - blocking entry point
    cfg = ensure_session_token(ctx.config)
    ctx.config = cfg
    httpd = serve(ctx)
    banner = (
        f"\nHermesUltraCode read API on http://{cfg.host}:{cfg.port}\n"
        f"  session token (paste into the dashboard): {cfg.session_token}\n"
        f"  loopback={cfg.is_loopback}  allowed_hosts={cfg.allowed_hosts}\n"
    )
    print(banner)
    log.info("read_api.start", extra={"event": "start", "host": cfg.host, "port": cfg.port})
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.shutdown()
