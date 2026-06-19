"""Hermes adapter — the only Hermes-coupled file (invariant 5: lift, don't fork).

Wires the gate into the REAL Hermes subagent-dispatch boundary. Hermes dispatches a
worker subagent through the ``delegate_task`` tool (args: ``goal`` + ``context`` +
``toolsets`` — see Hermes ``tools/delegate_tool.py``). The gate intercepts that
dispatch through two Hermes plugin seams, which the agent loop runs in this order for
every tool call (verified in Hermes ``agent/agent_runtime_helpers.py``):

  1. ``tool_request`` middleware — may rewrite the effective tool args BEFORE hooks
     see them, by returning ``{"args": {...}}``. This is where the gate applies its
     append-only TIGHTEN (it rewrites ``goal`` to base + appended directives).
  2. ``pre_tool_call`` hook — may return ``{"action": "block", "message": ...}`` to
     refuse the call. This is where the gate enforces a BLOCK / fail-closed.

Both seams receive the same ``tool_call_id``, so the gate runs ONCE per dispatch
(memoised by ``tool_call_id``) and both seams read the same decision.

Fail closed (invariant 1): if the gate is misconfigured (no reviewer, or the reviewer
shares the orchestrator's lab), the ``pre_tool_call`` seam BLOCKS every
``delegate_task`` rather than letting an un-vetted worker run. The plugin's
``register()`` must therefore NEVER fail to install this hook — see ``__init__.py``.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from core.gate import Gate, DispatchResult
from core.tighten import _DIRECTIVE_HEADER
from core.tiering import DispatchMeta

log = logging.getLogger("hermesultracode.adapter")

# The Hermes tool that dispatches a worker subagent, and its argument names.
DISPATCH_TOOL = "delegate_task"
GOAL_ARG = "goal"        # the subagent's task — the gate's immutable "base prompt"
CONTEXT_ARG = "context"  # background data the subagent receives
TOOLSETS_ARG = "toolsets"

# Blast-radius hints from the subagent's granted toolsets (touched paths are unknown
# before the subagent runs, so tiering is necessarily coarser at this boundary).
_READONLY_TOOLSETS = {"search", "read", "web", "browse", "retrieval", "research"}
_MERGE_TOOLSETS = {"merge", "deploy", "release", "publish"}
_ELEVATED_TOOLSETS = {"git", "infra", "terminal", "shell", "ci", "k8s", "docker"}


class GateBlocked(RuntimeError):
    """Raised by :meth:`HermesDispatchGate.assert_release` when the gate refuses a
    dispatch. Carries the :class:`DispatchResult` for callers that want the detail."""

    def __init__(self, result: DispatchResult) -> None:
        super().__init__(
            f"gate did not release dispatch: decision={result.decision} "
            f"tier={result.tier} record={result.record_id} "
            f"reason={result.fail_closed_reason or result.rationale!r}"
        )
        self.result = result


@dataclass
class HermesDispatchGate:
    """Adapts a :class:`core.gate.Gate` to the Hermes ``tool_request`` + ``pre_tool_call``
    seams. Provider/storage-agnostic; the gate does the policy, this just maps shapes.

    ``gate`` may be ``None`` to run in fail-closed mode (every ``delegate_task`` is
    blocked with ``config_error``) — used when the reviewer cannot be configured, so
    enabling the plugin can never silently let dispatch through un-vetted.
    """

    gate: Gate | None
    config_error: str = ""
    cache_size: int = 256

    def __post_init__(self) -> None:
        self._cache: "OrderedDict[str, DispatchResult]" = OrderedDict()

    # -- Hermes seam 1: tool_request middleware (the TIGHTEN) -----------------

    def tool_request(self, tool_name: str, args: Any, tool_call_id: str = "", **ctx: Any):
        """Return ``{"args": {...}}`` with the tightened ``goal`` when the gate releases
        a (possibly tightened) dispatch; ``None`` to leave args unchanged. A non-release
        returns ``None`` here — the block is enforced by :meth:`pre_tool_call`."""
        if tool_name != DISPATCH_TOOL or not isinstance(args, dict):
            return None
        if self.gate is None:
            return None  # fail-closed block happens in pre_tool_call
        result = self._decide(tool_call_id, args)
        if result.released and result.dispatched_prompt is not None:
            new_args = dict(args)
            new_args[GOAL_ARG] = result.dispatched_prompt
            log.info(
                "adapter.tighten",
                extra={"event": "tighten", "record": result.record_id,
                       "tier": result.tier, "n_directives": len(result.added_directives)},
            )
            return {"args": new_args}
        return None

    # -- Hermes seam 2: pre_tool_call hook (the BLOCK / fail-closed) ----------

    def pre_tool_call(self, tool_name: str, args: Any, tool_call_id: str = "", **ctx: Any):
        """Return ``{"action": "block", "message": ...}`` to refuse a dispatch the gate
        did not release; ``None`` to allow it."""
        if tool_name != DISPATCH_TOOL or not isinstance(args, dict):
            return None
        if self.gate is None:
            return {
                "action": "block",
                "message": (
                    "[HermesUltraCode gate · fail-closed] the pre-dispatch gate is not "
                    f"configured, so worker dispatch is refused. {self.config_error} "
                    "Set the reviewer provider env (a DIFFERENT lab from the orchestrator) "
                    "and re-enable, or disable the plugin to dispatch un-vetted."
                ),
            }
        result = self._decide(tool_call_id, args)
        if not result.released:
            reason = result.fail_closed_reason or result.rationale or "gate did not release this dispatch"
            log.warning(
                "adapter.block",
                extra={"event": "block", "record": result.record_id,
                       "decision": result.decision, "tier": result.tier},
            )
            return {
                "action": "block",
                "message": (
                    f"[HermesUltraCode gate · {result.decision} · tier={result.tier}"
                    f"{' · escalated' if result.escalated else ''}] {reason}"
                ),
            }
        return None

    # -- one-shot helper for non-Hermes callers (tests, embedding) -----------

    def assert_release(self, args: dict) -> str:
        """Run the gate and return the tightened ``goal``, or raise :class:`GateBlocked`."""
        result = self._decide("", args)
        if self.gate is None or not result.released or result.dispatched_prompt is None:
            raise GateBlocked(result)
        return result.dispatched_prompt

    # -- gate evaluation, memoised by tool_call_id ---------------------------

    def _decide(self, tool_call_id: str, args: dict) -> DispatchResult:
        key = tool_call_id or _args_key(args)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached
        base, meta = self._extract(args)
        try:
            result = self.gate.review_and_dispatch(base, meta)  # never raises by design
        except Exception as exc:  # noqa: BLE001 - last-resort fail-closed guard
            log.exception("adapter.gate_error")
            result = _synthetic_block(base, meta.task_id, str(exc))
        self._cache[key] = result
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return result

    def _extract(self, args: dict) -> tuple[str, DispatchMeta]:
        raw_goal = str(args.get(GOAL_ARG) or "")
        # Idempotency: if a prior pass already appended directives (e.g. tool_request
        # ran and pre_tool_call now sees the rewritten goal), recover the ORIGINAL base
        # so the gate reviews the same text and never tightens the tightened.
        base = raw_goal.split(_DIRECTIVE_HEADER, 1)[0]
        context = str(args.get(CONTEXT_ARG) or "")
        toolsets = args.get(TOOLSETS_ARG) or []
        if isinstance(toolsets, str):
            toolsets = [toolsets]
        return base, _meta_from(toolsets, context)


def _meta_from(toolsets, context: str) -> DispatchMeta:
    """Approximate blast radius from the subagent's toolsets (paths are unknown until
    the subagent runs). Read-only -> trivial; merge/deploy -> merge_adjacent;
    git/infra/terminal -> elevated (via a synthetic protected path); else standard."""
    ts = {str(t).strip().lower() for t in toolsets if str(t).strip()}
    read_only = bool(ts) and ts.issubset(_READONLY_TOOLSETS)
    carries_merge = bool(ts & _MERGE_TOOLSETS)
    elevated = bool(ts & _ELEVATED_TOOLSETS)
    return DispatchMeta(
        carries_merge_authority=carries_merge,
        # a synthetic protected path makes the deterministic tiering classify elevated
        touched_paths=("infra/subagent",) if (elevated and not carries_merge) else (),
        # Touched paths are unknown until the subagent runs, so a write-capable dispatch
        # must default to STANDARD (reviewed), NOT trivial. A read-only subagent is the
        # only thing we trivial-skip. (file_count 2 => standard; 0 + read_only => trivial.)
        file_count=(0 if read_only else 2),
        read_only=read_only,
        description=context[:500],
    )


def _args_key(args: dict) -> str:
    import hashlib
    import json

    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        blob = repr(sorted(args.items(), key=lambda kv: kv[0]))
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()


def _synthetic_block(base: str, task_id: str, reason: str) -> DispatchResult:
    return DispatchResult(
        released=False, decision="blocked", tier="standard", verdict="unavailable",
        round_count=0, record_id="", dispatched_prompt=None, added_directives=(),
        rationale=reason, fail_closed=True, fail_closed_reason=reason,
    )
