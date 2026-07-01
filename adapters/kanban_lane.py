"""Kanban worker lane (the re-framed B5): plugin lane registration with a custom spawn_fn
does not exist in Hermes, so the ``ultracode`` lane is a POLLER. Tasks are tagged with a
non-profile assignee (the dispatcher auto-skips them as ``skipped_nonspawnable``); this
plugin-owned poller ``claim_task``s each ready one, gates it, and spawns
``hermes -p <provider-profile> chat -m <model>`` itself, owning the terminal transitions.

Fail closed EVERYWHERE (invariant #7 + #9): a not-ready profile, a review block, or any
reviewer error/timeout/quota/empty blocks the task (no fallback, no un-vetted spawn). The
gate core is unchanged — this lane is just a second front door onto it.

Hermes coupling is quarantined behind ``KanbanBackend`` so the decision logic is offline-
testable. Control-plane (profiles/creds) is never reachable from here on the LLM path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from adapters.profiles import ProfileBackend, Readiness, validate_ready
from core.roster import Roster
from core.routing import Route, escalate, resolve
from core.task_tag import RouteTag, parse_tag
from core.tiering import DispatchMeta

LANE_ASSIGNEE = "ultracode"


# ---------------------------------------------------------------------------
# Backend seam
# ---------------------------------------------------------------------------


class KanbanBackend(Protocol):
    def list_ready(self) -> list: ...                       # ready tasks assigned to the lane
    def claim(self, task_id: str, claimer: str) -> bool: ...  # atomic ready->running
    def add_comment(self, task_id: str, author: str, body: str) -> None: ...
    def block(self, task_id: str, reason: str) -> bool: ...
    def spawn(self, profile: str, model: str | None, prompt: str, workspace: str | None) -> int | None: ...


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    action: str                 # "spawned" | "blocked"
    reason: str
    pid: int | None = None
    profile: str | None = None
    model: str | None = None


def build_spawn_argv(profile: str, model: str | None, prompt: str) -> list[str]:
    """`hermes -p <profile> [-m <model>] chat -q <prompt>` — model is a TOP-LEVEL flag
    before `chat` (matching Hermes ``_default_spawn``); omitted => the profile default."""
    argv = ["hermes", "-p", profile]
    if model:
        argv += ["-m", model]
    argv += ["chat", "-q", prompt]
    return argv


def _meta_for(tier: str, goal: str) -> DispatchMeta:
    """A DispatchMeta whose deterministic classification matches the route's risk tier, so
    the gate applies the tier-appropriate review policy."""
    desc = (goal or "")[:500]
    if tier == "merge_adjacent":
        return DispatchMeta(carries_merge_authority=True, description=desc)
    if tier == "elevated":
        return DispatchMeta(touched_paths=("infra/ultracode",), description=desc)
    return DispatchMeta(touched_paths=("src/a.py", "src/b.py"), read_only=False, description=desc)


def _verdict_comment(result, tier: str, model: str | None, *, spawned: bool) -> str:
    dirs = " | ".join(getattr(result, "added_directives", ()) or ())
    head = "dispatched" if spawned else f"blocked ({getattr(result, 'decision', '')})"
    return (f"ultracode gate: {head} · tier={tier} · model={model or 'profile-default'}"
            + (f" · directives: {dirs}" if dirs else ""))


def _pick_ready(tag: RouteTag, roster: Roster, pbackend: ProfileBackend, *, cap: int = 6):
    """Find a profile that clears the tier and is authenticated+serving the model, escalating
    across the tier's candidates on a not-ready profile (config cascade). Returns
    ``(profile, model, Readiness)`` — profile is None when the tier is exhausted (fail closed).
    An escalated profile uses ITS default model, not the tag's (the tag's model was for the
    original provider)."""
    prof = roster.provider(tag.profile)
    model = tag.model or (resolve(prof, tag.tier) if prof else None)
    cur = Route(tag.profile, prof.lab if prof else "", tag.tier)
    last = Readiness(tag.profile, False, "unknown profile")
    for _ in range(cap):
        if prof is None:
            last = Readiness(cur.profile, False, "unknown profile")
        else:
            last = validate_ready(cur.profile, {model} if model else set(), pbackend)
            if last.ready:
                return cur.profile, model, last
        nxt = escalate(cur, tag.tier, roster)
        if nxt is None:
            return None, None, last
        cur = nxt
        prof = roster.provider(cur.profile)
        model = resolve(prof, tag.tier) if prof else None
    return None, None, last


def process_claimed_task(task, roster: Roster, gate, kanban: KanbanBackend,
                         pbackend: ProfileBackend) -> Outcome:
    """The full fail-closed decision for one CLAIMED (running) task. Pure over the backends."""
    tag = parse_tag(getattr(task, "comments", "") or "",
                    native_model=getattr(task, "model_override", None))
    if tag is None:
        kanban.block(task.id, "review-required: no ultracode route tag")
        return Outcome("blocked", "no route tag")

    # 1) fail-closed served-model validation BEFORE any review or spawn
    profile, model, readiness = _pick_ready(tag, roster, pbackend)
    if profile is None:
        kanban.add_comment(task.id, LANE_ASSIGNEE, f"ultracode fail-closed: {readiness.reason}")
        kanban.block(task.id, f"review-required: unroutable ({readiness.reason})")
        return Outcome("blocked", readiness.reason, profile=tag.profile, model=model)

    # 2) the gate — any error/timeout/quota/empty is fail-closed to a block
    try:
        result = gate.review_and_dispatch(task.goal, _meta_for(tag.tier, task.goal))
    except Exception as exc:  # noqa: BLE001 - reviewer fault must never dispatch un-vetted
        kanban.block(task.id, f"review-required: gate error {exc}")
        return Outcome("blocked", f"gate error: {exc}", profile=profile, model=model)

    if not getattr(result, "released", False):
        kanban.add_comment(task.id, LANE_ASSIGNEE, _verdict_comment(result, tag.tier, model, spawned=False))
        kanban.block(task.id, f"review-required: {getattr(result, 'decision', 'blocked')}")
        return Outcome("blocked", getattr(result, "decision", "blocked"), profile=profile, model=model)

    # 3) released -> spawn the worker ourselves (base verbatim + directives)
    pid = kanban.spawn(profile, model, result.dispatched_prompt, getattr(task, "workspace", None))
    kanban.add_comment(task.id, LANE_ASSIGNEE, _verdict_comment(result, tag.tier, model, spawned=True))
    return Outcome("spawned", "dispatched", pid=pid, profile=profile, model=model)


def poll_once(roster: Roster, gate, kanban: KanbanBackend, pbackend: ProfileBackend,
              *, claimer: str = LANE_ASSIGNEE) -> list[Outcome]:
    """One poll pass: claim each ready lane task, then gate+spawn/block it. Never raises out
    (a per-task fault blocks that task, the loop continues)."""
    out: list[Outcome] = []
    for task in kanban.list_ready():
        if not kanban.claim(task.id, claimer):
            continue
        try:
            out.append(process_claimed_task(task, roster, gate, kanban, pbackend))
        except Exception as exc:  # noqa: BLE001 - last-resort per-task fail-closed
            try:
                kanban.block(task.id, f"review-required: lane error {exc}")
            except Exception:  # noqa: BLE001
                pass
            out.append(Outcome("blocked", f"lane error: {exc}"))
    return out


# ---------------------------------------------------------------------------
# Real backend (lazy Hermes; runs only in the poller daemon)
# ---------------------------------------------------------------------------


class HermesKanbanBackend:
    """Production backend over ``hermes_cli.kanban_db`` (+ a subprocess worker spawn). Lazy
    imports so this module loads without Hermes; never exercised by the offline tests."""

    def __init__(self, conn_factory, *, workspace_root: str | None = None):
        self._conn_factory = conn_factory      # callable -> sqlite connection to the board
        self._workspace_root = workspace_root

    def list_ready(self) -> list:
        from hermes_cli import kanban_db
        conn = self._conn_factory()
        return [t for t in kanban_db.list_tasks(conn, status="ready")
                if getattr(t, "assignee", "") == LANE_ASSIGNEE]

    def claim(self, task_id: str, claimer: str) -> bool:
        from hermes_cli import kanban_db
        return bool(kanban_db.claim_task(self._conn_factory(), task_id, claimer=claimer))

    def add_comment(self, task_id: str, author: str, body: str) -> None:
        from hermes_cli import kanban_db
        kanban_db.add_comment(self._conn_factory(), task_id, author=author, body=body)

    def block(self, task_id: str, reason: str) -> bool:
        from hermes_cli import kanban_db
        return bool(kanban_db.block_task(self._conn_factory(), task_id, reason=reason))

    def spawn(self, profile: str, model: str | None, prompt: str, workspace: str | None) -> int | None:
        import subprocess
        argv = build_spawn_argv(profile, model, prompt)
        proc = subprocess.Popen(argv, cwd=workspace or self._workspace_root)
        return proc.pid
