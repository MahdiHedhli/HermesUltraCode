"""Optional, read-only MCP server so the Hermes agent itself can answer "show me
today's gate verdicts." Minimal stdio JSON-RPC over the audit store — no write tools,
no mutation surface (lift, don't fork: invariant 5).

ponytail: a hand-rolled line-delimited JSON-RPC loop on stdlib (rung 2: stdlib does
it). Upgrade path: the official `mcp` Python SDK with Content-Length framing if a
client needs strict MCP transport. The request handler ``handle_rpc`` is pure so it
is unit-testable without stdio.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any

from core.store import AuditFilter, AuditStore
from server import views

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "hermesultracode-gate", "version": "1.0.0"}


@dataclass
class McpContext:
    store: AuditStore
    benchmark: dict | None = None


TOOLS = [
    {
        "name": "gate_verdicts_today",
        "description": "List gate dispatch verdicts for a given ISO date (default: all). Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "YYYY-MM-DD; omit for all"}},
        },
    },
    {
        "name": "get_dispatch",
        "description": "Return the full gate panel for one dispatch id (verdict, directives, decision).",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string"}},
            "required": ["id"],
        },
    },
    {
        "name": "audit_query",
        "description": "Query the immutable audit trail, filterable by tier/verdict/decision/fail_closed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tier": {"type": "string"},
                "verdict": {"type": "string"},
                "decision": {"type": "string"},
                "fail_closed": {"type": "boolean"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "gate_metrics",
        "description": "Gate metrics: counts, fail-closed counter, latency p50/p95, added tokens.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _content(obj: Any) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False, indent=2)}]}


def _tool_call(ctx: McpContext, name: str, args: dict) -> dict:
    if name == "gate_verdicts_today":
        date = args.get("date")
        rows = ctx.store.all()
        if date:
            rows = [r for r in rows if r.ts.startswith(date)]
        return _content([views.record_summary(r) for r in rows])
    if name == "get_dispatch":
        rec = ctx.store.get(args.get("id", ""))
        if rec is None:
            return _content({"error": "not found"})
        return _content(views.gate_panel(rec))
    if name == "audit_query":
        flt = AuditFilter(
            tier=args.get("tier"), verdict=args.get("verdict"),
            decision=args.get("decision"), fail_closed=args.get("fail_closed"),
            limit=args.get("limit"),
        )
        return _content([views.record_summary(r) for r in ctx.store.query(flt)])
    if name == "gate_metrics":
        return _content(views.compute_metrics(ctx.store, ctx.benchmark))
    raise KeyError(name)


def handle_rpc(ctx: McpContext, request: dict) -> dict | None:
    """Handle one JSON-RPC request; return the response (or None for notifications)."""
    method = request.get("method")
    req_id = request.get("id")
    params = request.get("params") or {}

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    if method == "initialize":
        return ok({
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method == "notifications/initialized" or (method and method.startswith("notifications/")):
        return None
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            return ok(_tool_call(ctx, name, args))
        except KeyError:
            return err(-32601, f"unknown tool: {name}")
        except Exception as exc:  # noqa: BLE001
            return err(-32603, f"tool error: {exc}")
    if req_id is None:
        return None
    return err(-32601, f"method not found: {method}")


def serve_stdio(ctx: McpContext, stdin=None, stdout=None) -> None:  # pragma: no cover
    """Line-delimited JSON-RPC loop over stdio."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle_rpc(ctx, request)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main(argv=None) -> int:  # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="HermesUltraCode read-only gate MCP server")
    ap.add_argument("--store", default="gate_audit.sqlite3", help="path to the audit store")
    args = ap.parse_args(argv)
    from core.store_sqlite import SqliteAuditStore

    serve_stdio(McpContext(store=SqliteAuditStore(args.store)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
