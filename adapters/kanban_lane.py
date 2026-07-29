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
from core.routing import Route, RoutingError, next_rung, resolve, route
from core.task_tag import RouteTag, parse_tag, serialize_tag
from core.tiering import DispatchMeta

LANE_ASSIGNEE = "ultracode"


# ---------------------------------------------------------------------------
# Backend seam
# ---------------------------------------------------------------------------


class KanbanBackend(Protocol):
    # Every backend is scoped to ONE board (the roster's board slug); all ops below act on it.
    board: str
    def ensure_board(self) -> None: ...                     # create-if-absent (idempotent)
    def list_ready(self) -> list: ...                       # ready lane tasks on THIS board only
    def claim(self, task_id: str, claimer: str) -> bool: ...  # atomic ready->running
    def create(self, goal: str, tag: str) -> str: ...       # create a lane task on THIS board
    def referenced_profiles(self) -> set: ...               # profiles routed-to by non-terminal tasks (incl blocked)
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


def _pick_ready(tag: RouteTag, roster: Roster, pbackend: ProfileBackend, *, cap: int = 8):
    """Find a profile that clears the tier and is authenticated+serving the model, stepping
    the cascade RUNGS on a not-ready profile: the same provider's heavy slot first (model
    cascade), then the next candidate profile (provider cascade). Returns
    ``(profile, model, Readiness)`` — profile is None when the ladder is exhausted (fail
    closed). An escalated profile uses ITS resolved model, not the tag's (the tag's model
    was for the original provider)."""
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
        rung = next_rung(cur, model, tag.tier, roster)
        if rung is None:
            return None, None, last
        cur, model = rung
        prof = roster.provider(cur.profile)
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


@dataclass(frozen=True)
class EnqueueDecision:
    ok: bool
    tier: str
    reason: str = ""
    profile: str | None = None
    model: str | None = None
    tag: str | None = None


def plan_enqueue(tier: str, roster: Roster, pbackend: ProfileBackend) -> EnqueueDecision:
    """Fail-closed enqueue gate (B7): route the tier, then require a READY profile that
    serves the resolved model, escalating across the tier's candidates. Returns the tag to
    write, or ``ok=False`` with a reason to reject BEFORE the task ever reaches ``ready``
    (never dispatch on a fallback). Pure over the backend."""
    try:
        r = route(tier, roster)
    except RoutingError as exc:
        return EnqueueDecision(False, tier, f"no candidate clears the {tier!r} floor: {exc}")
    prof = roster.provider(r.profile)
    model = resolve(prof, tier)
    profile, model2, readiness = _pick_ready(RouteTag(r.profile, tier, model), roster, pbackend)
    if profile is None:
        return EnqueueDecision(False, tier, f"unroutable: {readiness.reason}", model=model)
    return EnqueueDecision(True, tier, "ready", profile, model2,
                           serialize_tag(profile, tier, model2))


def create_ultracode_task(goal: str, tier: str, roster: Roster, kanban: KanbanBackend,
                          pbackend: ProfileBackend):
    """The lane's create path: fail-closed enqueue validation, then create the task on the
    LANE'S BOARD (``kanban.board`` == ``roster.board``, never a literal) with the route tag.
    Returns ``(task_id, EnqueueDecision)``; ``task_id`` is None when validation rejects it —
    an unroutable task is never landed."""
    decision = plan_enqueue(tier, roster, pbackend)
    if not decision.ok:
        return None, decision
    return kanban.create(goal, decision.tag), decision


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


@dataclass(frozen=True)
class _LaneTask:
    """The shape ``process_claimed_task`` expects, adapted from a Hermes ``Task`` (which has
    title/body and keeps comments in a separate table, and only gains ``model_override`` once
    Part A merges)."""
    id: str
    goal: str
    comments: str
    model_override: str | None
    workspace: str | None


class HermesKanbanBackend:
    """Production backend over ``hermes_cli.kanban_db`` (+ a subprocess worker spawn). Lazy
    imports so this module loads without Hermes; never exercised by the offline tests. One
    cached connection to the board's own DB — a task on any other board is physically
    invisible here (board = blast-radius containment)."""

    def __init__(self, board: str, *, workspace_root: str | None = None):
        self.board = board                         # roster's slug, NOT a literal
        self._workspace_root = workspace_root
        self._c = None

    def _conn(self):
        if self._c is None:
            from hermes_cli import kanban_db
            self._c = kanban_db.connect(board=self.board)
        return self._c

    def ensure_board(self) -> None:
        from hermes_cli import kanban_db
        kanban_db.create_board(self.board)         # idempotent (mkdir -p semantics)

    def _enrich(self, task):
        from hermes_cli import kanban_db
        comments = "\n".join(c.body for c in kanban_db.list_comments(self._conn(), task.id))
        goal = (task.title or "") + (("\n\n" + task.body) if task.body else "")
        return _LaneTask(id=task.id, goal=goal, comments=comments,
                         model_override=getattr(task, "model_override", None),
                         workspace=task.workspace_path)

    def list_ready(self) -> list:
        from hermes_cli import kanban_db
        return [self._enrich(t)
                for t in kanban_db.list_tasks(self._conn(), assignee=LANE_ASSIGNEE, status="ready")]

    def claim(self, task_id: str, claimer: str) -> bool:
        from hermes_cli import kanban_db
        return kanban_db.claim_task(self._conn(), task_id, claimer=claimer) is not None

    def create(self, goal: str, tag: str, *, ready: bool = True) -> str:
        """Create on the lane's board, tag it, and (when validated) promote to ``ready`` for
        the poller. Hermes only allows an initial status of running/blocked, and ``ready`` is
        reached via ``promote_task`` (blocked→ready), so enqueue = create-blocked → tag →
        promote. Leave ``ready=False`` to hold a task that failed enqueue validation."""
        from hermes_cli import kanban_db
        conn = self._conn()
        res = kanban_db.create_task(conn, title=goal, assignee=LANE_ASSIGNEE, initial_status="blocked")
        tid = res if isinstance(res, str) else res.id
        kanban_db.add_comment(conn, tid, author=LANE_ASSIGNEE, body=tag)
        if ready:
            kanban_db.promote_task(conn, tid, actor=LANE_ASSIGNEE, reason="ultracode enqueue")
        return tid

    def referenced_profiles(self) -> set:
        """Profiles routed-to by NON-TERMINAL tasks on this board (from each task's route tag),
        `blocked` included — so a blocked-pre-promotion task protects its profile from reaping.
        Delegates the terminal/non-terminal decision to the pure ``profiles.referenced_profiles``."""
        from hermes_cli import kanban_db

        from adapters.profiles import NON_TERMINAL_STATUSES
        from adapters.profiles import referenced_profiles as _ref
        from core.task_tag import parse_tag
        conn = self._conn()
        rows = []
        for status in NON_TERMINAL_STATUSES:
            for t in kanban_db.list_tasks(conn, status=status):
                comments = "\n".join(c.body for c in kanban_db.list_comments(conn, t.id))
                tag = parse_tag(comments, native_model=getattr(t, "model_override", None))
                rows.append((status, tag.profile if tag else None))
        return _ref(rows)

    def add_comment(self, task_id: str, author: str, body: str) -> None:
        from hermes_cli import kanban_db
        kanban_db.add_comment(self._conn(), task_id, author=author, body=body)

    def block(self, task_id: str, reason: str) -> bool:
        from hermes_cli import kanban_db
        return bool(kanban_db.block_task(self._conn(), task_id, reason=reason))

    def spawn(self, profile: str, model: str | None, prompt: str, workspace: str | None) -> int | None:
        import subprocess
        argv = build_spawn_argv(profile, model, prompt)
        proc = subprocess.Popen(argv, cwd=workspace or self._workspace_root)
        return proc.pid
