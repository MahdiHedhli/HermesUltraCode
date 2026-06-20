"""Read-only view builders over the audit store: live state, queue, gate panel,
neckbeard ledger, and metrics. Pure functions so they are unit-testable without a
socket. The read API (``read_api.py``) is a thin HTTP shell over these.
"""

from __future__ import annotations

from typing import Any, Callable

from core.neckbeard import harvest_markers
from core.redact import redact_obj
from core.store import AuditFilter, AuditStore, DispatchRecord

LiveSource = Callable[[AuditStore], dict]
QueueSource = Callable[[AuditStore], list]


def record_summary(r: DispatchRecord) -> dict[str, Any]:
    """A compact, dashboard-friendly row (full prompts elided to a length)."""
    return {
        "id": r.id,
        "ts": r.ts,
        "tier": r.tier,
        "verdict": r.verdict,
        "decision": r.decision,
        "round_count": r.round_count,
        "released": r.decision.startswith("dispatched"),
        "escalated": r.escalated,
        "fail_closed": r.fail_closed,
        "dissent_logged": r.dissent_logged,
        "neckbeard_block": r.neckbeard_block,
        "n_directives": len(r.added_directives),
        "reviewer_model": r.reviewer_model,
        "latency_ms": r.latency_ms,
        "added_tokens": r.added_tokens,
    }


def gate_panel(r: DispatchRecord) -> dict[str, Any]:
    """Per-dispatch gate panel: verdict, rounds, the appended directives (the actual
    'tighten'), rationale, reviewer model, and the final decision."""
    return {
        **record_summary(r),
        "base_prompt": r.base_prompt,
        "added_directives": list(r.added_directives),
        "dispatched_prompt": r.dispatched_prompt,
        "rationale": r.rationale,
        "scope_assessment": r.scope_assessment,
        "fail_closed_reason": r.fail_closed_reason,
    }


def default_live_source(store: AuditStore) -> dict[str, Any]:
    """Derive a 'live' view from the store: the orchestrator plus the most recent
    dispatches as stand-ins for active workers. Real runtimes can inject a richer
    ``live_source`` (e.g. polling Hermes for actual worker backends/status)."""
    recent = store.query(AuditFilter(limit=8))
    recent = list(reversed(recent))  # newest first
    workers = [
        {
            "worker": f"subagent-{r.id[:6]}",
            "backend": r.reviewer_model or "(skipped)",
            "status": _status_for(r),
            "tier": r.tier,
            "dispatch_id": r.id,
        }
        for r in recent[:5]
    ]
    current = record_summary(recent[0]) if recent else None
    return {
        "orchestrator": {"status": "online", "backend": "hermes-orchestrator"},
        "workers": workers,
        "current_dispatch": current,
    }


def _status_for(r: DispatchRecord) -> str:
    if r.fail_closed:
        return "blocked (fail-closed)"
    if r.escalated:
        return "escalated"
    if r.decision.startswith("dispatched"):
        return "dispatched"
    return "blocked"


def make_progress_live_source(progress) -> LiveSource:
    """Live source backed by the subagent progress store + Hermes's own
    ``list_active_subagents()`` registry (when running in-process under Hermes). Shows
    real active subagents, a tool-by-tool activity feed, and finished subagents with
    their output. Everything is secret-redacted before it leaves the process."""
    import time as _time

    from server.progress import PLAN_STATE, plan_progress

    def src(store: AuditStore) -> dict[str, Any]:
        snap = progress.snapshot()
        active = list(snap["active"])
        # Authoritative active list from Hermes's registry (keep our per-agent tool logs).
        try:
            from tools.delegate_tool import list_active_subagents  # type: ignore

            now = _time.time()
            log_by_id = {a["subagent_id"]: a.get("log", []) for a in snap["active"]}
            reg = []
            for r in list_active_subagents():
                started = r.get("started_at") or now
                sid = r.get("subagent_id", "")
                reg.append({
                    "subagent_id": sid,
                    "goal": r.get("goal", "") or "",
                    "role": r.get("role") or ("orchestrator" if r.get("depth") else "leaf"),
                    "depth": r.get("depth", 0),
                    "model": r.get("model", ""),
                    "status": r.get("status", "running"),
                    "last_tool": r.get("last_tool", ""),
                    "tool_count": r.get("tool_count", 0),
                    "elapsed_s": round(now - started, 1) if isinstance(started, (int, float)) else None,
                    "log": log_by_id.get(sid, []),
                })
            if reg:
                active = reg
        except Exception:  # noqa: BLE001 - registry only exists in a live Hermes process
            pass

        # Reviewer impact: link each active agent to the gate decision that shaped its task
        # (base goal -> appended directives), so the dashboard shows the reviewer's influence.
        recent = store.query(AuditFilter(limit=40))

        def gate_for(goal: str):
            g = goal or ""
            match = None
            for r in recent:  # ascending -> the last match is the newest
                bp = (r.base_prompt or "").strip()
                if bp and (bp[:60] in g or g[:60] in bp):
                    match = r
            if match is None:
                return None
            return {"id": match.id, "tier": match.tier, "verdict": match.verdict,
                    "decision": match.decision, "rationale": match.rationale,
                    "added_directives": list(match.added_directives)}

        for a in active:
            a["gate"] = gate_for(a.get("goal", ""))

        plan = snap["plan"]
        plan_items = [{**it, "state": PLAN_STATE.get((it.get("status") or "").lower(), "todo")}
                      for it in plan["items"]]
        recent_release = store.query(AuditFilter(limit=1))
        return redact_obj({
            "orchestrator": {**snap["orchestrator"], "status": "online", "backend": "hermes-orchestrator"},
            "active": active,
            "feed": snap["feed"],
            "completed": snap["completed"],
            "plan": {"items": plan_items, "updated_at": plan["updated_at"],
                     "source": plan["source"], "progress": plan_progress(plan["items"])},
            "last_gate_decision": record_summary(recent_release[-1]) if recent_release else None,
        })

    return src


def make_store_live_source(store) -> LiveSource:
    """Live source that reads the persisted progress snapshot from SQLite (written by the
    agent process's hooks) — for a dashboard running in a DIFFERENT process (the Hermes
    web-dashboard plugin), which can't see the agent's in-memory PROGRESS. The snapshot is
    the already-enriched, already-redacted live view; returned verbatim with an empty
    fallback so the view degrades gracefully when no build is running."""

    def src(_store: AuditStore | None = None) -> dict[str, Any]:
        try:
            snap = store.get_progress_snapshot()
        except Exception:  # noqa: BLE001
            snap = None
        if not snap:
            return {
                "orchestrator": {"status": "online", "backend": "hermes-orchestrator",
                                 "active": False, "tool_count": 0, "last_tool": "", "log": []},
                "active": [], "feed": [], "completed": [],
                "plan": {"items": [], "progress": {"total": 0, "done": 0, "active": 0, "todo": 0, "pct": 0}},
                "last_gate_decision": None,
            }
        return snap

    return src


def default_queue_source(store: AuditStore) -> list[dict[str, Any]]:
    """Recent dispatches as a queue, newest first, each with its tier badge."""
    recent = list(reversed(store.query(AuditFilter(limit=25))))
    return [
        {**record_summary(r), "tier_badge": r.tier}
        for r in recent
    ]


def neckbeard_view(store: AuditStore, repo_root: str) -> dict[str, Any]:
    """Debt ledger (harvested `neckbeard:` markers) + protected-set violations the
    gate blocked."""
    debt = [
        {"file": it.file, "line": it.line, "note": it.note}
        for it in harvest_markers(repo_root)
    ]
    protected_blocks = [
        gate_panel(r)
        for r in store.all()
        if r.neckbeard_block
    ]
    return {"debt_ledger": debt, "protected_set_violations": protected_blocks}


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    frac = k - lo
    return float(s[lo] + (s[hi] - s[lo]) * frac)


def compute_metrics(store: AuditStore, benchmark: dict | None = None) -> dict[str, Any]:
    """Gate-on metrics from the store, merged with gate-on-vs-off benchmark results.

    Store-derived: dispatch counts, released/blocked/escalated, the fail-closed
    counter (so silent degradation is visible), latency p50/p95, avg added tokens,
    and per-tier breakdown. Benchmark-derived (optional): first-pass worker success
    gate-on vs gate-off and guideline-violation rate."""
    rows = store.all()
    total = len(rows)
    released = sum(1 for r in rows if r.decision.startswith("dispatched"))
    blocked = sum(1 for r in rows if r.decision == "blocked")
    escalated = sum(1 for r in rows if r.escalated)
    fail_closed = sum(1 for r in rows if r.fail_closed)
    latencies = [float(r.latency_ms) for r in rows]
    tokens = [r.added_tokens for r in rows if r.decision.startswith("dispatched")]

    by_tier: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    for r in rows:
        by_tier[r.tier] = by_tier.get(r.tier, 0) + 1
        by_verdict[r.verdict] = by_verdict.get(r.verdict, 0) + 1

    metrics: dict[str, Any] = {
        "total_dispatches": total,
        "released": released,
        "blocked": blocked,
        "escalated": escalated,
        "fail_closed_count": fail_closed,
        "gate_latency_ms_p50": round(_percentile(latencies, 0.50), 2),
        "gate_latency_ms_p95": round(_percentile(latencies, 0.95), 2),
        "avg_added_tokens_per_dispatch": round(sum(tokens) / len(tokens), 2) if tokens else 0.0,
        "by_tier": by_tier,
        "by_verdict": by_verdict,
    }
    if benchmark:
        metrics["benchmark"] = benchmark
    return metrics
