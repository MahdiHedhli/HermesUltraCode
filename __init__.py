"""HermesUltraCode — Hermes plugin entry point.

Hermes imports this module and calls ``register(ctx)`` once at startup. We wire the
pre-dispatch gate into the real Hermes seams:

  * ``tool_request`` middleware on ``delegate_task`` — append-only TIGHTEN.
  * ``pre_tool_call`` hook on ``delegate_task``       — BLOCK / fail-closed.

plus read-only gate-audit query tools, the neckbeard skill, and a dashboard CLI.

Fail-closed contract: ``register`` NEVER raises and ALWAYS installs the pre_tool_call
hook. If the gate cannot be configured (missing reviewer key, or the reviewer shares
the orchestrator's lab), the hook runs in fail-closed mode and blocks every
``delegate_task`` — enabling the plugin can never silently dispatch a worker un-vetted.

Heavy imports live inside ``register`` so plugin *discovery* (which imports this file)
stays light and cannot fail on an optional dependency.
"""

from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("hermesultracode.plugin")

_HERE = os.path.dirname(os.path.abspath(__file__))


def _ensure_importable() -> None:
    """Put the plugin root on sys.path so ``core``/``adapters``/``server`` resolve
    however Hermes loaded us."""
    if _HERE not in sys.path:
        sys.path.insert(0, _HERE)


def _default_store_path() -> str:
    base = os.environ.get(
        "HERMESULTRACODE_STORE",
        os.path.join(os.path.expanduser("~"), ".hermes", "hermesultracode", "audit.sqlite3"),
    )
    os.makedirs(os.path.dirname(base), exist_ok=True)
    return base


def _build_gate(store):
    """Build the Gate from env. Raises on misconfiguration (caught by ``register`` to
    enter fail-closed mode)."""
    from core.gate import Gate
    from core.providers import (
        HermesProvider,
        OpenRouterProvider,
        validate_distinct_providers,
    )
    from core.tiering import TieringConfig

    reviewer_lab = os.environ.get("HERMESULTRACODE_REVIEWER_LAB", "anthropic")
    reviewer_model = os.environ.get("HERMESULTRACODE_REVIEWER_MODEL", "anthropic/claude-3.5-sonnet")
    reviewer_key = os.environ.get("HERMESULTRACODE_REVIEWER_API_KEY", "")
    reviewer_base = os.environ.get("HERMESULTRACODE_REVIEWER_BASE_URL", "")
    orch_lab = os.environ.get("HERMESULTRACODE_ORCH_LAB", "nous")

    if not reviewer_key and not reviewer_base:
        raise RuntimeError(
            "HERMESULTRACODE_REVIEWER_API_KEY is not set (and no reviewer base URL/proxy "
            "configured); the gate has no reviewer."
        )

    reviewer_kwargs = {"lab": reviewer_lab, "model": reviewer_model, "api_key": reviewer_key or "proxy"}
    if reviewer_base:
        reviewer_kwargs["base_url"] = reviewer_base
    reviewer = OpenRouterProvider(**reviewer_kwargs)

    # The orchestrator side is the Hermes/Nous host — never called by the gate, only
    # lab-checked for distinctness.
    orchestrator = HermesProvider(lab=orch_lab, model="hermes-orchestrator")
    validate_distinct_providers(orchestrator, reviewer)  # raises if labs match

    return Gate(
        reviewer_provider=reviewer,
        orchestrator_provider=orchestrator,
        store=store,
        round_cap=int(os.environ.get("HERMESULTRACODE_ROUND_CAP", "2")),
        reviewer_timeout_s=float(os.environ.get("HERMESULTRACODE_REVIEWER_TIMEOUT_S", "30")),
        tiering_config=TieringConfig(),
    )


# ---------------------------------------------------------------------------
# Read-only query tools (handler(args, **kwargs) -> JSON string; never raises)
# ---------------------------------------------------------------------------


def _make_query_tools(store):
    import json

    from server import views
    from core.store import AuditFilter

    def _ok(obj) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2)

    def _err(msg) -> str:
        return json.dumps({"error": str(msg)})

    def gate_metrics(args, **kwargs) -> str:
        try:
            return _ok(views.compute_metrics(store))
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    def gate_audit_query(args, **kwargs) -> str:
        try:
            a = args or {}
            flt = AuditFilter(tier=a.get("tier"), verdict=a.get("verdict"),
                              decision=a.get("decision"), limit=int(a.get("limit", 50)))
            return _ok([views.record_summary(r) for r in store.query(flt)])
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    def gate_recent_verdicts(args, **kwargs) -> str:
        try:
            a = args or {}
            rows = store.all()
            date = a.get("date")
            if date:
                rows = [r for r in rows if r.ts.startswith(date)]
            return _ok([views.record_summary(r) for r in rows[-int(a.get("limit", 25)):]])
        except Exception as exc:  # noqa: BLE001
            return _err(exc)

    schemas = {
        "gate_metrics": {
            "name": "gate_metrics",
            "description": "HermesUltraCode: gate metrics — dispatch counts, fail-closed counter, latency p50/p95, added tokens.",
            "parameters": {"type": "object", "properties": {}},
        },
        "gate_audit_query": {
            "name": "gate_audit_query",
            "description": "HermesUltraCode: query the immutable gate audit trail by tier/verdict/decision.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tier": {"type": "string", "description": "merge_adjacent|elevated|standard|trivial"},
                    "verdict": {"type": "string", "description": "pass|revise|block|unavailable"},
                    "decision": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
        "gate_recent_verdicts": {
            "name": "gate_recent_verdicts",
            "description": "HermesUltraCode: recent gate verdicts, optionally for an ISO date (YYYY-MM-DD).",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "limit": {"type": "integer"},
                },
            },
        },
    }
    handlers = {"gate_metrics": gate_metrics, "gate_audit_query": gate_audit_query,
                "gate_recent_verdicts": gate_recent_verdicts}
    return schemas, handlers


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------


def register(ctx) -> None:
    _ensure_importable()
    from adapters.hermes_hook import HermesDispatchGate

    store = None
    gate = None
    config_error = ""
    try:
        from core.store_sqlite import SqliteAuditStore

        store = SqliteAuditStore(_default_store_path())
    except Exception as exc:  # noqa: BLE001
        config_error = f"audit store unavailable: {exc}"
        log.error("hermesultracode: %s", config_error)

    try:
        if store is not None:
            gate = _build_gate(store)
    except Exception as exc:  # noqa: BLE001
        config_error = (config_error + " " if config_error else "") + str(exc)
        log.error("hermesultracode: gate not configured, entering FAIL-CLOSED mode: %s", exc)

    hdg = HermesDispatchGate(gate=gate, config_error=config_error)

    # The block seam first — the fail-closed guarantee must hold even if a later
    # registration call were to fail.
    ctx.register_hook("pre_tool_call", hdg.pre_tool_call)
    ctx.register_middleware("tool_request", hdg.tool_request)
    log.info("hermesultracode: gate %s on delegate_task",
             "ACTIVE" if gate is not None else "FAIL-CLOSED (blocking)")

    # Read-only query tools so the Hermes agent can answer "show me today's verdicts".
    if store is not None:
        try:
            schemas, handlers = _make_query_tools(store)
            for tname, schema in schemas.items():
                ctx.register_tool(
                    name=tname, toolset="hermesultracode", schema=schema,
                    handler=handlers[tname], description=schema["description"],
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("hermesultracode: query tools not registered: %s", exc)

    # Neckbeard ruleset as a companion skill (opt-in load).
    try:
        from pathlib import Path

        skill_md = Path(_HERE) / "skills" / "neckbeard" / "SKILL.md"
        if skill_md.exists():
            ctx.register_skill("neckbeard", skill_md,
                               description="Neckbeard minimalism ladder + extended protected set.")
    except Exception as exc:  # noqa: BLE001
        log.debug("hermesultracode: neckbeard skill not registered: %s", exc)

    # Dashboard launcher: `hermes ultracode-dashboard`.
    try:
        _register_dashboard_cli(ctx, _default_store_path())
    except Exception as exc:  # noqa: BLE001
        log.debug("hermesultracode: dashboard CLI not registered: %s", exc)


def _register_dashboard_cli(ctx, store_path: str) -> None:
    def setup(subparser) -> None:
        subparser.add_argument("--host", default="127.0.0.1")
        subparser.add_argument("--port", type=int, default=9120)
        subparser.add_argument("--store", default=store_path)
        subparser.add_argument("--token", default="")

    def handler(args) -> None:
        _ensure_importable()
        from core.config import ReadApiConfig
        from core.store_sqlite import SqliteAuditStore
        from server.read_api import ReadApiContext, run

        ctx_obj = ReadApiContext(
            store=SqliteAuditStore(args.store),
            config=ReadApiConfig(host=args.host, port=args.port, session_token=args.token),
            surfaced_config={"store": args.store, "host": args.host, "port": args.port},
        )
        run(ctx_obj)

    ctx.register_cli_command(
        "ultracode-dashboard",
        help="Launch the read-only HermesUltraCode gate dashboard (loopback + token).",
        setup_fn=setup,
        handler_fn=handler,
        description="Read-only gate observability dashboard.",
    )
