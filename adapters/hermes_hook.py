"""Hermes adapter — the ONLY Hermes-coupled file (lift, don't fork: invariant 5).

It hooks the gate into the Hermes subagent dispatch boundary. If it cannot intercept
dispatch, it fails closed: it raises and refuses to let any worker prompt through
un-vetted, rather than degrading to pass-through (invariant 1).

The Hermes runtime is imported lazily and never at module import time, so the gate
core stays Hermes-free and offline-testable. This file is intentionally thin; all
policy lives in ``core/``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from core.gate import Gate, DispatchResult
from core.tiering import DispatchMeta

log = logging.getLogger("hermesultracode.adapter")


class AdapterUnavailable(RuntimeError):
    """Raised when the gate cannot attach to the Hermes dispatch boundary.

    This is a fail-closed condition: callers must NOT dispatch if registration fails.
    """


class GateBlocked(RuntimeError):
    """Raised to Hermes to abort a dispatch the gate did not release.

    Carries the ``DispatchResult`` so the orchestrator/UI can show why (blocked vs
    escalated, the rationale, the audit record id)."""

    def __init__(self, result: DispatchResult) -> None:
        super().__init__(
            f"gate did not release dispatch: decision={result.decision} "
            f"tier={result.tier} record={result.record_id} reason={result.fail_closed_reason or result.rationale!r}"
        )
        self.result = result


@dataclass
class HermesGateAdapter:
    """Wraps a :class:`core.gate.Gate` and exposes a dispatch interceptor.

    ``meta_builder`` maps a Hermes dispatch request to a :class:`DispatchMeta`. A
    default best-effort mapping is provided; override it to read your runtime's exact
    fields (merge authority, touched paths, file count, read-only, cost).
    """

    gate: Gate
    meta_builder: Callable[[Any], DispatchMeta] | None = None

    # -- the boundary the dispatcher must consult -------------------------------

    def intercept(self, dispatch_request: Any) -> str:
        """Vet a pending dispatch. Return the dispatched prompt to forward to the
        worker, or raise :class:`GateBlocked` so Hermes aborts. Never returns an
        un-vetted prompt; never silently passes a base through on error."""
        base_prompt = _extract_base_prompt(dispatch_request)
        meta = (self.meta_builder or default_meta_builder)(dispatch_request)
        result = self.gate.review_and_dispatch(base_prompt, meta)
        if not result.released or result.dispatched_prompt is None:
            log.warning(
                "adapter.blocked",
                extra={"event": "blocked", "record": result.record_id, "decision": result.decision},
            )
            raise GateBlocked(result)
        log.info(
            "adapter.released",
            extra={"event": "released", "record": result.record_id, "tier": result.tier},
        )
        return result.dispatched_prompt

    # -- registration against the Hermes hook surface ---------------------------

    def register(self, hermes_runtime: Any | None = None) -> None:
        """Attach ``intercept`` to the Hermes subagent-dispatch hook.

        Fails closed: if no usable hook surface is found, raises
        :class:`AdapterUnavailable` so the operator cannot accidentally run with the
        gate detached. Wiring is intentionally duck-typed because the exact Hermes
        hook API is environment-specific; supply ``hermes_runtime`` exposing one of
        the recognised registration hooks below.
        """
        runtime = hermes_runtime
        if runtime is None:
            runtime = _try_import_hermes_runtime()
        if runtime is None:
            raise AdapterUnavailable(
                "no Hermes runtime available to attach the gate; refusing to run with "
                "the dispatch boundary unguarded (fail closed)"
            )

        for hook_name in (
            "register_predispatch_hook",
            "on_subagent_dispatch",
            "add_dispatch_interceptor",
            "set_dispatch_gate",
        ):
            hook = getattr(runtime, hook_name, None)
            if callable(hook):
                hook(self.intercept)
                log.info("adapter.registered", extra={"event": "registered", "hook": hook_name})
                return

        raise AdapterUnavailable(
            "Hermes runtime exposes no recognised dispatch hook "
            "(register_predispatch_hook / on_subagent_dispatch / add_dispatch_interceptor / "
            "set_dispatch_gate); cannot intercept dispatch — fail closed"
        )


def _try_import_hermes_runtime() -> Any | None:
    """Best-effort lazy import of a Hermes runtime singleton. Returns None if Hermes
    is not importable here (the gate core never depends on it)."""
    try:  # pragma: no cover - depends on a Hermes install not present in tests
        import hermes  # type: ignore

        return getattr(hermes, "runtime", None) or getattr(hermes, "get_runtime", lambda: None)()
    except Exception:  # noqa: BLE001
        return None


def _extract_base_prompt(request: Any) -> str:
    """Pull the worker base prompt from a dispatch request, tolerating shapes."""
    for attr in ("base_prompt", "prompt", "worker_prompt", "instructions"):
        val = getattr(request, attr, None)
        if isinstance(val, str) and val.strip():
            return val
    if isinstance(request, dict):
        for key in ("base_prompt", "prompt", "worker_prompt", "instructions"):
            val = request.get(key)
            if isinstance(val, str) and val.strip():
                return val
    raise GateBlockedExtractionError(
        "could not extract a base prompt from the dispatch request; failing closed"
    )


class GateBlockedExtractionError(RuntimeError):
    """A dispatch request without an extractable base prompt is refused (fail closed)."""


def default_meta_builder(request: Any) -> DispatchMeta:
    """Best-effort mapping from a Hermes dispatch request to DispatchMeta.

    Conservative defaults: when a field is unknown, assume the HIGHER blast radius is
    NOT trivially granted — unknown merge authority defaults False, but unknown
    read-only defaults False too, so an ambiguous dispatch lands in standard/elevated,
    never silently in trivial."""

    def pick(*names, default=None):
        for n in names:
            v = getattr(request, n, None)
            if v is None and isinstance(request, dict):
                v = request.get(n)
            if v is not None:
                return v
        return default

    touched = pick("touched_paths", "files", "paths", default=()) or ()
    if isinstance(touched, str):
        touched = (touched,)
    return DispatchMeta(
        carries_merge_authority=bool(pick("carries_merge_authority", "can_merge", default=False)),
        touched_paths=tuple(touched),
        file_count=pick("file_count", default=None),
        read_only=bool(pick("read_only", default=False)),
        estimated_cost_usd=float(pick("estimated_cost_usd", "cost", default=0.0) or 0.0),
        task_id=str(pick("task_id", "id", default="") or ""),
        description=str(pick("description", "title", default="") or ""),
    )
