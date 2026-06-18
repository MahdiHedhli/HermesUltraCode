"""Acceptance criterion 9 (security half): the read API enforces loopback bind
intent + session token + localhost CORS + Host-header check + secret redaction.
Exercised through the pure ``handle`` router — no socket opened."""

import json
import socket
import threading
import unittest
import urllib.error
import urllib.request

from tests.helpers import standard_meta, verdict_json, make_gate

from core.config import ReadApiConfig
from core.store_sqlite import SqliteAuditStore
from server.read_api import ReadApiContext, handle, serve

TOKEN = "test-token-abc123"
HOST = "127.0.0.1:9120"


def ctx_with_data():
    gate = make_gate(reviewer_responses=[verdict_json("pass", ["Only touch export/."])])
    res = gate.review_and_dispatch("Base task here.", standard_meta())
    cfg = ReadApiConfig(
        host="127.0.0.1", port=9120, session_token=TOKEN,
        allowed_hosts=(HOST, "localhost:9120", "127.0.0.1:9120"),
    )
    ctx = ReadApiContext(
        store=gate.store, config=cfg,
        surfaced_config={"reviewer_api_key": "sk-secret0123456789ABCDEF", "round_cap": 2},
        benchmark={"gate_on": {"first_pass_success_rate": 0.9}},
    )
    return ctx, res.record_id


def hdr(token=TOKEN, host=HOST, origin=None):
    h = {"Host": host}
    if token is not None:
        h["X-Gate-Session-Token"] = token
    if origin:
        h["Origin"] = origin
    return h


class ReadApiSecurityTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.rid = ctx_with_data()

    def test_valid_request_ok(self):
        r = handle(self.ctx, "GET", "/api/metrics", hdr())
        self.assertEqual(r.status, 200)
        self.assertIn("total_dispatches", r.body.decode())

    def test_missing_token_rejected(self):
        r = handle(self.ctx, "GET", "/api/metrics", hdr(token=None))
        self.assertEqual(r.status, 401)

    def test_wrong_token_rejected(self):
        r = handle(self.ctx, "GET", "/api/metrics", hdr(token="nope"))
        self.assertEqual(r.status, 401)

    def test_bad_host_rejected(self):
        r = handle(self.ctx, "GET", "/api/metrics", hdr(host="evil.example.com"))
        self.assertEqual(r.status, 403)

    def test_dns_rebinding_host_rejected_even_with_token(self):
        # An attacker page resolving to 127.0.0.1 still sends its own Host header.
        r = handle(self.ctx, "GET", "/api/audit", hdr(host="attacker.test:9120"))
        self.assertEqual(r.status, 403)

    def test_blank_token_config_locks_api(self):
        self.ctx.config = ReadApiConfig(host="127.0.0.1", port=9120, session_token="",
                                        allowed_hosts=(HOST,))
        r = handle(self.ctx, "GET", "/api/metrics", hdr(token=None))
        self.assertEqual(r.status, 503)

    def test_cors_allows_localhost_origin(self):
        r = handle(self.ctx, "GET", "/api/metrics", hdr(origin="http://localhost:9120"))
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "http://localhost:9120")

    def test_cors_denies_foreign_origin(self):
        r = handle(self.ctx, "GET", "/api/metrics", hdr(origin="http://evil.example.com"))
        self.assertNotIn("Access-Control-Allow-Origin", r.headers)

    def test_options_preflight_no_token_needed(self):
        r = handle(self.ctx, "OPTIONS", "/api/metrics", hdr(token=None, origin="http://localhost:9120"))
        self.assertEqual(r.status, 204)

    def test_options_with_bad_host_rejected(self):
        # Audit regression: OPTIONS must NOT bypass Host validation (DNS rebinding).
        r = handle(self.ctx, "OPTIONS", "/api/metrics", hdr(token=None, host="evil.example.com"))
        self.assertEqual(r.status, 403)

    def test_only_get_allowed(self):
        r = handle(self.ctx, "POST", "/api/metrics", hdr())
        self.assertEqual(r.status, 405)

    def test_config_is_redacted(self):
        r = handle(self.ctx, "GET", "/api/config", hdr())
        self.assertEqual(r.status, 200)
        self.assertNotIn("sk-secret0123456789ABCDEF", r.body.decode())
        self.assertIn("REDACTED", r.body.decode())

    def test_static_path_traversal_blocked(self):
        r = handle(self.ctx, "GET", "/static/..%2f..%2fcore%2fgate.py", hdr(token=None))
        # basename strips traversal; file won't exist in web/ -> 404, never escapes.
        self.assertIn(r.status, (404,))

    def test_health_no_token(self):
        r = handle(self.ctx, "GET", "/healthz", hdr(token=None))
        self.assertEqual(r.status, 200)


class ReadApiViewsTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.rid = ctx_with_data()

    def test_live(self):
        r = handle(self.ctx, "GET", "/api/live", hdr())
        data = json.loads(r.body)
        self.assertIn("orchestrator", data)
        self.assertIn("workers", data)

    def test_queue(self):
        r = handle(self.ctx, "GET", "/api/queue", hdr())
        self.assertIn("queue", json.loads(r.body))

    def test_gate_panel(self):
        r = handle(self.ctx, "GET", f"/api/dispatch/{self.rid}", hdr())
        data = json.loads(r.body)
        self.assertEqual(data["id"], self.rid)
        self.assertIn("added_directives", data)
        self.assertIn("base_prompt", data)

    def test_gate_panel_unknown_404(self):
        r = handle(self.ctx, "GET", "/api/dispatch/doesnotexist", hdr())
        self.assertEqual(r.status, 404)

    def test_audit_json_export_headers(self):
        r = handle(self.ctx, "GET", "/api/audit.json", hdr())
        self.assertEqual(r.status, 200)
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
        self.assertIsInstance(json.loads(r.body), list)

    def test_audit_csv_export_headers(self):
        r = handle(self.ctx, "GET", "/api/audit.csv", hdr())
        self.assertEqual(r.status, 200)
        self.assertIn("text/csv", r.content_type)
        self.assertIn("attachment", r.headers.get("Content-Disposition", ""))

    def test_metrics_includes_failclosed_and_benchmark(self):
        r = handle(self.ctx, "GET", "/api/metrics", hdr())
        data = json.loads(r.body)
        self.assertIn("fail_closed_count", data)
        self.assertIn("benchmark", data)

    def test_neckbeard_view(self):
        r = handle(self.ctx, "GET", "/api/neckbeard", hdr())
        data = json.loads(r.body)
        self.assertIn("debt_ledger", data)
        self.assertIn("protected_set_violations", data)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ReadApiLiveSocketTest(unittest.TestCase):
    """Exercise the real ThreadingHTTPServer over a socket — catches handler/logging
    wiring bugs the pure-handler tests cannot (e.g. the reserved-LogRecord-field bug)."""

    def setUp(self):
        port = _free_port()
        gate = make_gate(reviewer_responses=[verdict_json("pass", ["only x/"])])
        gate.review_and_dispatch("Base.", standard_meta())
        cfg = ReadApiConfig(
            host="127.0.0.1", port=port, session_token=TOKEN,
            allowed_hosts=(f"127.0.0.1:{port}", f"localhost:{port}"),
        )
        self.ctx = ReadApiContext(store=gate.store, config=cfg, benchmark={"x": 1})
        self.httpd = serve(self.ctx)
        self.base = f"http://127.0.0.1:{port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _get(self, path, token=TOKEN):
        req = urllib.request.Request(self.base + path)
        if token is not None:
            req.add_header("X-Gate-Session-Token", token)
        return urllib.request.urlopen(req, timeout=3)

    def test_healthz_live(self):
        with self._get("/healthz", token=None) as r:
            self.assertEqual(r.status, 200)

    def test_dashboard_shell_live(self):
        with self._get("/", token=None) as r:
            body = r.read().decode()
            self.assertIn("HermesUltraCode", body)

    def test_metrics_live_with_token(self):
        with self._get("/api/metrics") as r:
            self.assertEqual(r.status, 200)
            self.assertIn("total_dispatches", json.loads(r.read()))

    def test_metrics_live_without_token_401(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/metrics", token=None)
        self.assertEqual(ctx.exception.code, 401)
        ctx.exception.close()

    def test_static_css_live(self):
        with self._get("/static/styles.css", token=None) as r:
            self.assertEqual(r.status, 200)
            self.assertIn("text/css", r.headers.get("Content-Type", ""))


if __name__ == "__main__":
    unittest.main()
