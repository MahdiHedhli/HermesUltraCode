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
        # Fail-closed is a deliberate, safe degradation (it blocks dispatch), not a crash —
        # WARNING, not ERROR, so an intentionally-unconfigured process isn't log-spam.
        log.warning("hermesultracode: gate not configured, entering FAIL-CLOSED mode: %s", exc)

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

    # /ultracode slash command — the explicit, reliable way to delegate through the gate.
    try:
        ctx.register_command(
            "ultracode",
            _make_ultracode_command(ctx, hdg, _default_store_path()),
            description="Delegate a task to a subagent, reviewed by the HermesUltraCode gate. `/ultracode <task>`, or `/ultracode status` for the dashboard URL+token.",
            args_hint="<task>",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("hermesultracode: /ultracode command not registered: %s", exc)

    # On session start: auto-start the dashboard and surface its URL + token (via the
    # log pipeline — inject_message would start an agent turn, which we don't want).
    try:
        ctx.register_hook("on_session_start", _make_session_start(hdg, _default_store_path()))
    except Exception as exc:  # noqa: BLE001
        log.debug("hermesultracode: on_session_start not registered: %s", exc)

    # Live subagent progress: feed the dashboard's Live tab from the real Hermes
    # subagent lifecycle (these are observer hooks — they never block or alter a call).
    try:
        start_cb, tool_cb, stop_cb = _make_progress_hooks()
        ctx.register_hook("subagent_start", start_cb)
        ctx.register_hook("post_tool_call", tool_cb)
        ctx.register_hook("subagent_stop", stop_cb)
    except Exception as exc:  # noqa: BLE001
        log.debug("hermesultracode: subagent progress hooks not registered: %s", exc)


def _make_progress_hooks():
    """Observer hooks that record subagent lifecycle into the in-memory progress store
    (server.progress.PROGRESS) for the dashboard Live view. Each is fail-safe and returns
    None (never blocks or rewrites a call)."""
    from server.progress import PROGRESS

    def on_subagent_start(**kw):
        try:
            PROGRESS.subagent_started(
                kw.get("child_subagent_id", ""), kw.get("child_session_id", ""),
                kw.get("child_goal", ""), kw.get("child_role", ""),
            )
        except Exception:  # noqa: BLE001
            pass
        return None

    def on_post_tool_call(**kw):
        try:
            PROGRESS.tool_event(
                kw.get("session_id", ""), kw.get("tool_name", ""),
                kw.get("status", ""), kw.get("duration_ms", 0),
            )
        except Exception:  # noqa: BLE001
            pass
        return None

    def on_subagent_stop(**kw):
        try:
            PROGRESS.subagent_stopped(
                kw.get("child_session_id", ""), kw.get("child_summary", "") or "",
                kw.get("child_status", "completed"), kw.get("duration_ms", 0),
            )
        except Exception:  # noqa: BLE001
            pass
        return None

    return on_subagent_start, on_post_tool_call, on_subagent_stop


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
        from server.progress import PROGRESS
        from server.read_api import ReadApiContext, run
        from server.views import make_progress_live_source

        ctx_obj = ReadApiContext(
            store=SqliteAuditStore(args.store),
            config=ReadApiConfig(host=args.host, port=args.port, session_token=args.token),
            surfaced_config={"store": args.store, "host": args.host, "port": args.port},
            live_source=make_progress_live_source(PROGRESS),
        )
        run(ctx_obj)

    ctx.register_cli_command(
        "ultracode-dashboard",
        help="Launch the read-only HermesUltraCode gate dashboard (loopback + token).",
        setup_fn=setup,
        handler_fn=handler,
        description="Read-only gate observability dashboard.",
    )


# ---------------------------------------------------------------------------
# Auto-started dashboard (one daemon HTTP server per Hermes process)
# ---------------------------------------------------------------------------

# Process-wide state so the dashboard starts at most once, even across sessions.
_DASH: dict = {"started": False, "url": None, "token": None, "port": None}
import threading as _threading  # noqa: E402

_DASH_LOCK = _threading.Lock()


def _auto_dashboard_enabled() -> bool:
    return os.environ.get("HERMESULTRACODE_AUTO_DASHBOARD", "1").lower() not in (
        "0", "false", "no", "off",
    )


def _ensure_dashboard(store_path: str):
    """Start the read-only dashboard in a daemon thread, at most once per process.
    Returns (url, token) — url has the token embedded for one-click open — or
    (None, None) if disabled/failed. Safe to call repeatedly."""
    with _DASH_LOCK:
        if _DASH["started"]:
            return _DASH["url"], _DASH["token"]
        _DASH["started"] = True  # guard: don't retry-spam, even if start fails
        if not _auto_dashboard_enabled():
            return None, None
        try:
            _ensure_importable()
            import secrets

            from core.config import ReadApiConfig
            from core.store_sqlite import SqliteAuditStore
            from server.progress import PROGRESS
            from server.read_api import ReadApiContext, serve
            from server.views import make_progress_live_source

            host = os.environ.get("HERMESULTRACODE_DASHBOARD_HOST", "127.0.0.1")
            base_port = int(os.environ.get("HERMESULTRACODE_DASHBOARD_PORT", "9120"))
            token = secrets.token_urlsafe(24)
            store = SqliteAuditStore(store_path)
            httpd = bound = None
            last_err = None
            for port in range(base_port, base_port + 6):
                cfg = ReadApiConfig(
                    host=host, port=port, session_token=token,
                    allowed_hosts=(f"{host}:{port}", f"localhost:{port}", f"127.0.0.1:{port}"),
                )
                ctx_obj = ReadApiContext(
                    store=store, config=cfg,
                    surfaced_config={"store": store_path, "host": host, "port": port},
                    live_source=make_progress_live_source(PROGRESS),
                )
                try:
                    httpd = serve(ctx_obj)
                    bound = port
                    break
                except OSError as exc:  # port in use -> try the next one
                    last_err = exc
            if httpd is None:
                log.warning("hermesultracode: dashboard could not bind %s:%d-%d (%s)",
                            host, base_port, base_port + 5, last_err)
                return None, None
            _threading.Thread(
                target=httpd.serve_forever, name="hermesultracode-dashboard", daemon=True
            ).start()
            url = f"http://{host}:{bound}/?token={token}"
            _DASH.update(url=url, token=token, port=bound)
            log.info("hermesultracode: dashboard on %s", url)
            return url, token
        except Exception as exc:  # noqa: BLE001
            log.warning("hermesultracode: dashboard auto-start failed: %s", exc)
            return None, None


def _gate_status(hdg) -> str:
    if getattr(hdg, "gate", None) is not None:
        return "ACTIVE — reviewing every delegate_task"
    return "FAIL-CLOSED (blocking all delegate_task) — " + (
        getattr(hdg, "config_error", "") or "reviewer not configured"
    )


def _hyperlink(url: str, label: str | None = None) -> str:
    """Optionally wrap a URL as an OSC 8 terminal hyperlink. OPT-IN: many terminals and
    the Hermes TUI do NOT support OSC 8 and render it as literal ``]8;;`` junk, so the
    default is a plain, copyable URL. Set HERMESULTRACODE_HYPERLINKS=1 only if your
    terminal supports OSC 8 hyperlinks."""
    if os.environ.get("HERMESULTRACODE_HYPERLINKS", "0").lower() not in ("1", "true", "yes", "on"):
        return label or url
    esc = "\033"
    return f"{esc}]8;;{url}{esc}\\{label or url}{esc}]8;;{esc}\\"


# ---------------------------------------------------------------------------
# /ultracode slash command + on_session_start banner
# ---------------------------------------------------------------------------


_UC_HELP = """\
Commands:
  /ultracode <task>      delegate <task> to a subagent — gate-reviewed (tightened, or blocked)
  /ultracode status      gate state + clickable dashboard link
  /ultracode agents      active subagents + recently completed (what's running, where it is)
  /ultracode verdicts    recent gate verdicts (tier · verdict · decision)
  /ultracode dashboard   the dashboard URL + session token
  /ultracode help        this help"""


def _make_ultracode_command(ctx, hdg, store_path: str):
    """`/ultracode` dispatches sub-views (help/status/agents/verdicts/dashboard) or, for
    anything else, treats the args as a task and delegates it through the gate. The return
    string is printed to the user (it does not feed the agent), so views are rendered as
    text tables and delegation is done directly via ctx.dispatch_tool — which is also how a
    plugin shows tasks/subagents inside the TUI (there is no plugin TUI-panel API)."""

    def handler(raw_args: str):
        low = (raw_args or "").strip().lower()
        if low in ("", "help", "?", "-h", "--help"):
            return f"🛂 HermesUltraCode gate: {_gate_status(hdg)}\n\n{_UC_HELP}"
        if low == "status":
            return _uc_status(hdg, store_path)
        if low in ("dashboard", "ui", "open"):
            return _uc_dashboard(store_path)
        if low in ("agents", "subagents", "tasks", "sub"):
            return _uc_agents()
        if low in ("verdicts", "log", "audit", "gate", "history"):
            return _uc_verdicts(store_path)
        return _uc_delegate(ctx, hdg, (raw_args or "").strip())

    return handler


def _short(s, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_table(headers, rows) -> str:
    cols = len(headers)
    w = [len(h) for h in headers]
    for r in rows:
        for i in range(cols):
            w[i] = max(w[i], len(str(r[i])))
    line = lambda c: "  " + "  ".join(str(c[i]).ljust(w[i]) for i in range(cols))
    return "\n".join([line(headers), line(["-" * x for x in w])] + [line(r) for r in rows])


def _uc_status(hdg, store_path: str) -> str:
    url, token = _ensure_dashboard(store_path)
    dash = (f"📊 Dashboard: {_hyperlink(url)}\n   (run `/ultracode dashboard` to open it in your browser)"
            if url else "📊 Dashboard: run `hermes ultracode-dashboard` (auto-start off or no free port)")
    return (f"🛂 HermesUltraCode gate: {_gate_status(hdg)}\n{dash}\n"
            "Type `/ultracode help` for all commands.")


def _uc_dashboard(store_path: str) -> str:
    url, token = _ensure_dashboard(store_path)
    if not url:
        return "📊 Dashboard auto-start is off or no port is free. Run `hermes ultracode-dashboard`."
    # The TUI's text output isn't clickable, so open the browser directly (the token is
    # embedded in the URL, so the page connects itself). HERMESULTRACODE_OPEN_BROWSER=0 to skip.
    opened = ""
    if os.environ.get("HERMESULTRACODE_OPEN_BROWSER", "1").lower() not in ("0", "false", "no", "off"):
        try:
            import webbrowser

            if webbrowser.open(url):
                opened = "  ✓ opening in your browser…"
        except Exception:  # noqa: BLE001
            pass
    return f"📊 HermesUltraCode dashboard{opened}\n   {url}\n   token: {token}"


def _uc_agents() -> str:
    from core.redact import redact
    from server.progress import PROGRESS

    snap = PROGRESS.snapshot()
    active = list(snap["active"])
    try:
        from tools.delegate_tool import list_active_subagents  # type: ignore

        reg = list_active_subagents()
        if reg:
            active = [{"goal": r.get("goal", ""), "status": r.get("status", "running"),
                       "last_tool": r.get("last_tool", ""), "tool_count": r.get("tool_count", 0),
                       "elapsed_s": None} for r in reg]
    except Exception:  # noqa: BLE001
        pass

    out = []
    if active:
        out.append("Active subagents:")
        out.append(_fmt_table(
            ["GOAL", "STATUS", "LAST TOOL", "TOOLS", "ELAPSED"],
            [[_short(redact(a.get("goal")), 40), a.get("status", ""), a.get("last_tool", "") or "—",
              str(a.get("tool_count", 0)),
              (f"{a['elapsed_s']}s" if a.get("elapsed_s") is not None else "—")] for a in active]))
    else:
        out.append("No active subagents. Start one with `/ultracode <task>`.")
    if snap["completed"]:
        out.append("\nRecently completed:")
        out.append(_fmt_table(
            ["GOAL", "STATUS", "TOOLS", "SUMMARY"],
            [[_short(redact(c.get("goal")), 32), c.get("status", ""), str(c.get("tool_count", 0)),
              _short(redact(c.get("summary")), 48)] for c in snap["completed"][:8]]))
    return "\n".join(out)


def _uc_verdicts(store_path: str) -> str:
    _ensure_importable()
    from core.store_sqlite import SqliteAuditStore

    rows = SqliteAuditStore(store_path).all()[-12:][::-1]  # newest first
    if not rows:
        return "No gate verdicts yet — delegate a task to populate the trail."
    return "Recent gate verdicts (newest first):\n" + _fmt_table(
        ["TIME", "TIER", "VERDICT", "DECISION", "GOAL"],
        [[(r.ts[11:19] if len(r.ts) >= 19 else r.ts), r.tier, r.verdict, r.decision,
          _short(r.base_prompt, 38)] for r in rows])  # base_prompt is already redacted on write


def _uc_delegate(ctx, hdg, text: str) -> str:
    from adapters.hermes_hook import GateBlocked

    args = {"goal": text, "context": ""}
    try:
        tightened = hdg.assert_release(dict(args))  # runs the gate (review/tighten/block)
    except GateBlocked as exc:
        r = exc.result
        return (f"🛑 HermesUltraCode did NOT release this delegation "
                f"(decision={r.decision}, tier={r.tier}).\n"
                f"Reason: {r.fail_closed_reason or r.rationale or 'blocked'}")
    changed = tightened.strip() != text
    args["goal"] = tightened
    try:
        result = ctx.dispatch_tool("delegate_task", args)
    except Exception as exc:  # noqa: BLE001
        return (f"⚠️ Gate released the goal ({'tightened' if changed else 'unchanged'}), "
                f"but delegate_task failed: {exc}")
    verb = "TIGHTENED the goal and released it" if changed else "passed the goal unchanged"
    return f"✓ HermesUltraCode reviewed and {verb}; ran the subagent.\n\n{result}"


def _make_session_start(hdg, store_path: str):
    """Surface the gate status + dashboard URL/token when a session starts. Logs (does
    NOT inject a message — that would start an agent turn)."""

    def on_session_start(**kwargs):
        try:
            url, _ = _ensure_dashboard(store_path)
            status = "ACTIVE" if getattr(hdg, "gate", None) is not None else "FAIL-CLOSED (blocking delegate_task)"
            if url:
                log.info("🛂 HermesUltraCode gate %s · dashboard %s · `/ultracode <task>` to delegate",
                         status, url)
            else:
                log.info("🛂 HermesUltraCode gate %s · `/ultracode` for status · `hermes ultracode-dashboard` for the UI",
                         status)
        except Exception as exc:  # noqa: BLE001
            log.debug("hermesultracode on_session_start: %s", exc)
        return None

    return on_session_start
