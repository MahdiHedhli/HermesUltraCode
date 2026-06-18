"""Read-only MCP server: pure handler tests (no stdio). It exposes verdicts/metrics
and has NO mutation surface."""

import json
import unittest

from tests.helpers import make_gate, standard_meta, verdict_json

from server.mcp_server import TOOLS, McpContext, handle_rpc


def ctx_with_data():
    gate = make_gate(reviewer_responses=[verdict_json("pass", ["only touch x/"])])
    res = gate.review_and_dispatch("Base task.", standard_meta())
    return McpContext(store=gate.store), res.record_id


class McpTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.rid = ctx_with_data()

    def rpc(self, method, params=None, id=1):
        return handle_rpc(self.ctx, {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})

    def test_initialize(self):
        r = self.rpc("initialize")
        self.assertIn("serverInfo", r["result"])

    def test_tools_are_read_only(self):
        r = self.rpc("tools/list")
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertEqual(names, {"gate_verdicts_today", "get_dispatch", "audit_query", "gate_metrics"})
        # No write/mutation verbs anywhere.
        for t in TOOLS:
            self.assertFalse(any(v in t["name"] for v in ("write", "delete", "update", "append", "set")))

    def test_get_dispatch(self):
        r = self.rpc("tools/call", {"name": "get_dispatch", "arguments": {"id": self.rid}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertEqual(payload["id"], self.rid)

    def test_audit_query(self):
        r = self.rpc("tools/call", {"name": "audit_query", "arguments": {"verdict": "pass"}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertTrue(len(payload) >= 1)

    def test_gate_metrics(self):
        r = self.rpc("tools/call", {"name": "gate_metrics", "arguments": {}})
        payload = json.loads(r["result"]["content"][0]["text"])
        self.assertIn("fail_closed_count", payload)

    def test_unknown_tool_errors(self):
        r = self.rpc("tools/call", {"name": "nope", "arguments": {}})
        self.assertIn("error", r)

    def test_notification_returns_none(self):
        self.assertIsNone(self.rpc("notifications/initialized", id=None))


if __name__ == "__main__":
    unittest.main()
