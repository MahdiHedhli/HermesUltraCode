"""Shared test helpers. Offline only — every provider is a MockProvider."""

from __future__ import annotations

import json
import logging
import os
import sys

# Keep test output clean — the gate logs warnings on (expected) fail-closed paths.
logging.getLogger("hermesultracode").setLevel(logging.CRITICAL)

# Make the repo root importable when run via `python -m unittest` from anywhere.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.gate import Gate  # noqa: E402
from core.providers import MockProvider  # noqa: E402
from core.store_sqlite import SqliteAuditStore  # noqa: E402
from core.tiering import DispatchMeta, TieringConfig  # noqa: E402


def verdict_json(verdict="pass", added_directives=None, rationale="ok",
                 scope="in_scope", round=0, model="reviewer-x") -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "added_directives": added_directives or [],
            "rationale": rationale,
            "scope_assessment": scope,
            "round": round,
            "reviewer_model": model,
        }
    )


def make_gate(reviewer_responses=None, *, reviewer=None, store=None,
              round_cap=2, cheap=None, tiering_config=None, reviewer_timeout_s=5.0,
              workspace_directive=None) -> Gate:
    """Build a Gate wired to mock providers and an in-memory store."""
    if reviewer is None:
        reviewer = MockProvider(lab="reviewer-lab", model="reviewer-x")
        if reviewer_responses is not None:
            reviewer.responses = list(reviewer_responses)
    orchestrator = MockProvider(lab="orch-lab", model="orch-1")
    store = store or SqliteAuditStore(":memory:")
    return Gate(
        reviewer_provider=reviewer,
        orchestrator_provider=orchestrator,
        store=store,
        round_cap=round_cap,
        reviewer_timeout_s=reviewer_timeout_s,
        tiering_config=tiering_config or TieringConfig(),
        cheap_reviewer_provider=cheap,
        workspace_directive=workspace_directive,
    )


def standard_meta(**kw) -> DispatchMeta:
    """A dispatch that classifies as 'standard' by default (multi-file, mutating)."""
    base = dict(touched_paths=("src/feature_a.py", "src/feature_b.py"), read_only=False)
    base.update(kw)
    return DispatchMeta(**base)
