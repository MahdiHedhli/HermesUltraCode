"""Benchmark harness: run a fixed task corpus through the pipeline twice — gate ON
and gate OFF — and emit the four metrics:

  * first-pass worker success rate,
  * guideline-violation rate,
  * added latency per dispatch,
  * added cost per dispatch.

Offline and deterministic: the reviewer is a scripted MockProvider (an oracle that
appends the tightening a good reviewer would), and the worker is a deterministic
simulator whose success/violations depend on whether the dispatched prompt addresses
each task's guideline risks. Ship this harness + 2-3 example tasks; the real corpus
is user-provided (`--tasks path.json`).

Usage:
    python -m bench.harness                       # uses bench/tasks.example.json
    python -m bench.harness --tasks corpus.json --out results.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.gate import Gate
from core.providers import MockProvider
from core.store_sqlite import SqliteAuditStore
from core.tiering import DispatchMeta, TieringConfig

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TASKS = os.path.join(HERE, "tasks.example.json")

# Each guideline risk maps to (the directive a good reviewer appends, the keyword the
# worker scans the dispatched prompt for to decide the risk was addressed).
RISK_DIRECTIVES: dict[str, tuple[str, str]] = {
    "input_validation": ("Validate and sanitize all external inputs at the trust boundary.", "validat"),
    "scope_creep": ("Keep the change strictly scoped to the named files; do not refactor unrelated code.", "scope"),
    "missing_tests": ("Add unit tests covering the new behavior and its edge cases.", "test"),
    "no_audit_log": ("Emit a structured audit-log entry for each state change.", "audit"),
    "no_idempotency": ("Make the operation idempotent and safe to retry with bounded backoff.", "idempot"),
    "data_loss": ("Guard against data loss: transactionally wrap or back up destructive operations.", "transactional"),
}

# Cost model (USD per 1k tokens) for the "added cost per dispatch" metric.
DEFAULT_PRICE_PER_1K = 0.003


@dataclass
class WorkerSim:
    """Deterministic worker. A task succeeds first-pass iff every guideline risk it
    carries is addressed in the dispatched prompt; each unaddressed risk is one
    guideline violation."""

    def run(self, risks: list[str], dispatched_prompt: str) -> tuple[bool, int]:
        text = dispatched_prompt.lower()
        violations = 0
        for risk in risks:
            keyword = RISK_DIRECTIVES.get(risk, (None, risk))[1]
            if keyword.lower() not in text:
                violations += 1
        success = violations == 0
        return success, violations


def directives_for(risks: list[str]) -> list[str]:
    return [RISK_DIRECTIVES[r][0] for r in risks if r in RISK_DIRECTIVES]


def _verdict_json(directives: list[str]) -> str:
    return json.dumps({
        "verdict": "pass" if not directives else "revise",
        "added_directives": directives,
        "rationale": "tightened to address guideline risks",
        "scope_assessment": "in_scope",
        "round": 0,
        "reviewer_model": "bench-oracle",
    })


def _meta(task: dict) -> DispatchMeta:
    m = task.get("meta", {})
    paths = tuple(m.get("touched_paths", []))
    return DispatchMeta(
        carries_merge_authority=bool(m.get("carries_merge_authority", False)),
        touched_paths=paths,
        file_count=m.get("file_count"),
        read_only=bool(m.get("read_only", False)),
        estimated_cost_usd=float(m.get("estimated_cost_usd", 0.0)),
        task_id=task.get("id", ""),
    )


def _run_side(tasks: list[dict], *, gate_on: bool, price_per_1k: float) -> dict:
    worker = WorkerSim()
    successes = 0
    total_violations = 0
    latencies: list[float] = []
    added_tokens: list[int] = []

    for task in tasks:
        risks = list(task.get("guideline_risks", []))
        base = task["base_prompt"]
        meta = _meta(task)

        if gate_on:
            directives = directives_for(risks)
            reviewer = MockProvider(lab="reviewer-lab", model="bench-oracle",
                                    responses=[_verdict_json(directives), _verdict_json([])])
            orchestrator = MockProvider(lab="orch-lab", model="bench-orch")
            gate = Gate(
                reviewer_provider=reviewer,
                orchestrator_provider=orchestrator,
                store=SqliteAuditStore(":memory:"),
                round_cap=2,
                reviewer_timeout_s=5.0,
                tiering_config=TieringConfig(),
            )
            t0 = time.monotonic()
            res = gate.review_and_dispatch(base, meta)
            latencies.append((time.monotonic() - t0) * 1000.0)
            if res.released and res.dispatched_prompt is not None:
                dispatched = res.dispatched_prompt
                added_tokens.append(res.added_tokens)
            else:
                # Gate blocked/escalated: the worker never runs -> counts as a
                # first-pass non-success but ZERO guideline violations reached prod.
                dispatched = None
                added_tokens.append(res.added_tokens)
        else:
            # Gate OFF: the base prompt is dispatched as-is, no review, no latency.
            t0 = time.monotonic()
            dispatched = base
            latencies.append((time.monotonic() - t0) * 1000.0)
            added_tokens.append(0)

        if dispatched is None:
            successes += 0  # blocked: not a first-pass success, but also no violation
        else:
            ok, violations = worker.run(risks, dispatched)
            successes += 1 if ok else 0
            total_violations += violations

    n = len(tasks)
    total_risks = sum(len(t.get("guideline_risks", [])) for t in tasks)
    avg_tokens = sum(added_tokens) / n if n else 0.0
    return {
        "n_tasks": n,
        "first_pass_success_rate": round(successes / n, 4) if n else 0.0,
        "guideline_violation_rate": round(total_violations / total_risks, 4) if total_risks else 0.0,
        "guideline_violations_total": total_violations,
        "avg_added_tokens_per_dispatch": round(avg_tokens, 2),
        "avg_added_cost_usd_per_dispatch": round(avg_tokens / 1000.0 * price_per_1k, 6),
        "avg_latency_ms_per_dispatch": round(sum(latencies) / n, 4) if n else 0.0,
    }


def run_benchmark(tasks: list[dict], *, price_per_1k: float = DEFAULT_PRICE_PER_1K) -> dict:
    """Run the corpus gate-on and gate-off and return the comparison + deltas."""
    on = _run_side(tasks, gate_on=True, price_per_1k=price_per_1k)
    off = _run_side(tasks, gate_on=False, price_per_1k=price_per_1k)
    return {
        "gate_on": on,
        "gate_off": off,
        "added_latency_ms_per_dispatch": round(
            on["avg_latency_ms_per_dispatch"] - off["avg_latency_ms_per_dispatch"], 4
        ),
        # delta (on - off), consistent with the other metrics; gate-off cost is 0 today
        # but compute the difference explicitly rather than relying on that.
        "added_cost_usd_per_dispatch": round(
            on["avg_added_cost_usd_per_dispatch"] - off["avg_added_cost_usd_per_dispatch"], 6
        ),
        "first_pass_success_delta": round(
            on["first_pass_success_rate"] - off["first_pass_success_rate"], 4
        ),
        "guideline_violation_delta": round(
            on["guideline_violation_rate"] - off["guideline_violation_rate"], 4
        ),
    }


def load_tasks(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data["tasks"] if isinstance(data, dict) else data


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HermesUltraCode gate benchmark (on vs off)")
    ap.add_argument("--tasks", default=DEFAULT_TASKS, help="path to a task corpus JSON")
    ap.add_argument("--price-per-1k", type=float, default=DEFAULT_PRICE_PER_1K)
    ap.add_argument("--out", default=None, help="write results JSON here as well as stdout")
    args = ap.parse_args(argv)

    tasks = load_tasks(args.tasks)
    results = run_benchmark(tasks, price_per_1k=args.price_per_1k)
    blob = json.dumps(results, indent=2)
    print(blob)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
