"""In-memory, thread-safe live progress of an active build (POST-dispatch).

Tracks the ORCHESTRATOR and its SUBAGENTS, each one's recent tool log, a rolling activity
feed, finished subagents with their output, and the orchestrator's live PLAN (its ``todo``
list — stages done/active/todo). Ephemeral, process-local — deliberately separate from the
immutable audit store (the gate's pre-dispatch evidence trail). The plugin's hooks write:

  * ``subagent_start`` -> ``subagent_started``   (a child agent was spawned)
  * ``post_tool_call`` -> ``tool_event``         (orchestrator or a child called a tool)
                       -> ``set_plan``           (the orchestrator's ``todo`` write = plan)
  * ``subagent_stop``  -> ``subagent_stopped``   (the child finished, with its summary)

The dashboard's Live + Plan views read ``PROGRESS.snapshot()``. In standalone (non-Hermes)
mode nothing writes here, so the snapshot is empty and the views fall back gracefully.

Sizes are capped (ring buffers) so a long session can't grow this unboundedly.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

_MAX_ACTIVE = 64
_MAX_FEED = 120
_MAX_DONE = 40
_MAX_LOG = 12          # per-agent recent tool calls


class LiveProgress:
    """Tracks the orchestrator, running/finished subagents, their tool logs, and the plan."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, dict] = {}      # subagent session_id -> active record
        self._active: "OrderedDict[str, dict]" = OrderedDict()  # subagent_id -> record
        self._feed: deque = deque(maxlen=_MAX_FEED)
        self._done: deque = deque(maxlen=_MAX_DONE)
        self._orch = self._new_orch()
        self._plan: dict = {"items": [], "updated_at": None, "source": ""}

    @staticmethod
    def _new_orch() -> dict:
        return {"session_id": "", "started_at": None, "tool_count": 0,
                "last_tool": "", "log": deque(maxlen=_MAX_LOG)}

    def subagent_started(self, subagent_id: str, session_id: str, goal: str = "", role: str = "") -> None:
        if not subagent_id:
            return
        rec = {
            "subagent_id": subagent_id, "session_id": session_id or "", "goal": goal or "",
            "role": role or "leaf", "started_at": time.time(), "status": "running",
            "tool_count": 0, "last_tool": "", "log": deque(maxlen=_MAX_LOG),
        }
        with self._lock:
            self._active[subagent_id] = rec
            if session_id:
                self._by_session[session_id] = rec
            while len(self._active) > _MAX_ACTIVE:
                self._active.popitem(last=False)

    def tool_event(self, session_id: str, tool_name: str, status: str = "", duration_ms: int = 0) -> None:
        """A tool call by the orchestrator (untracked session) or a subagent (tracked)."""
        entry = {"ts": time.time(), "tool": tool_name or "", "status": status or "",
                 "duration_ms": int(duration_ms or 0)}
        with self._lock:
            rec = self._by_session.get(session_id)
            if rec is None:                      # orchestrator / top-level activity
                o = self._orch
                if o["started_at"] is None:
                    o["started_at"] = entry["ts"]
                if not o["session_id"]:
                    o["session_id"] = session_id or ""
                o["tool_count"] += 1
                o["last_tool"] = tool_name or ""
                o["log"].appendleft(entry)
                who, goal = "orchestrator", "orchestrator"
            else:
                rec["tool_count"] += 1
                rec["last_tool"] = tool_name or ""
                rec["log"].appendleft(entry)
                who, goal = rec["subagent_id"], rec["goal"][:70]
            self._feed.append({**entry, "subagent_id": who, "goal": goal})

    def set_plan(self, items, source: str = "todo") -> None:
        """Merge a ``todo`` write into the live plan (by id, order-preserving) so both full
        and partial (merge-mode) writes accumulate into the current plan."""
        if not items:
            return
        with self._lock:
            order = [it["id"] for it in self._plan["items"]]
            by_id = {it["id"]: dict(it) for it in self._plan["items"]}
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                iid = str(raw.get("id") or "").strip() or f"_{len(order) + 1}"
                cur = by_id.get(iid, {"id": iid, "content": "", "status": "pending"})
                content = str(raw.get("content") or "").strip()
                status = str(raw.get("status") or "").strip().lower()
                if content:
                    cur["content"] = content
                if status:
                    cur["status"] = status
                if iid not in by_id:
                    order.append(iid)
                by_id[iid] = cur
            self._plan = {"items": [by_id[i] for i in order],
                          "updated_at": time.time(), "source": source}

    def subagent_stopped(self, session_id: str, summary: str = "",
                         status: str = "completed", duration_ms: int = 0) -> None:
        with self._lock:
            rec = self._by_session.pop(session_id, None)
            if rec is None:
                return
            self._active.pop(rec["subagent_id"], None)
            self._done.appendleft({
                "subagent_id": rec["subagent_id"], "goal": rec["goal"],
                "status": status or "completed", "summary": (summary or "")[:4000],
                "tool_count": rec["tool_count"], "duration_ms": int(duration_ms or 0),
                "ended_at": time.time(),
            })

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            active = [{
                "subagent_id": r["subagent_id"], "goal": r["goal"], "role": r["role"],
                "status": r["status"], "tool_count": r["tool_count"], "last_tool": r["last_tool"],
                "elapsed_s": round(now - r["started_at"], 1), "log": list(r["log"]),
            } for r in self._active.values()]
            feed = list(self._feed)[-60:][::-1]
            done = list(self._done)[:20]
            o = self._orch
            orch = {
                "session_id": o["session_id"], "tool_count": o["tool_count"],
                "last_tool": o["last_tool"], "log": list(o["log"]),
                "elapsed_s": round(now - o["started_at"], 1) if o["started_at"] else None,
                "active": o["started_at"] is not None,
            }
            plan = {"items": [dict(i) for i in self._plan["items"]],
                    "updated_at": self._plan["updated_at"], "source": self._plan["source"]}
        return {"orchestrator": orch, "active": active, "feed": feed,
                "completed": done, "plan": plan}

    def clear(self) -> None:  # used by tests
        with self._lock:
            self._by_session.clear()
            self._active.clear()
            self._feed.clear()
            self._done.clear()
            self._orch = self._new_orch()
            self._plan = {"items": [], "updated_at": None, "source": ""}


# Map the orchestrator's raw todo statuses to dashboard stage states.
PLAN_STATE = {"pending": "todo", "in_progress": "active", "completed": "done",
              "cancelled": "cancelled", "blocked": "blocked"}


def plan_progress(items) -> dict:
    """Counts + percent-done for a plan item list (for the Plan window header)."""
    total = len(items or [])
    done = sum(1 for it in items if PLAN_STATE.get(it.get("status", ""), "todo") == "done")
    active = sum(1 for it in items if PLAN_STATE.get(it.get("status", ""), "todo") == "active")
    todo = total - done - active
    pct = round(100 * done / total) if total else 0
    return {"total": total, "done": done, "active": active, "todo": todo, "pct": pct}


# One process-wide instance: the plugin hooks and the in-process dashboard share it.
PROGRESS = LiveProgress()
