"""In-memory, thread-safe live progress of subagent execution (POST-dispatch).

This is ephemeral, process-local state — deliberately separate from the immutable audit
store (which is the gate's pre-dispatch evidence trail). The plugin's subagent lifecycle
hooks write here:

  * ``subagent_start``  -> ``subagent_started``   (a child agent was spawned)
  * ``post_tool_call``  -> ``tool_event``         (the child called a tool — the "feed")
  * ``subagent_stop``   -> ``subagent_stopped``   (the child finished, with its summary)

and the dashboard's Live view reads ``PROGRESS.snapshot()``. In standalone (non-Hermes)
mode nothing writes here, so the snapshot is empty and the Live view falls back gracefully.

Sizes are capped (ring buffers) so a long session can't grow this unboundedly.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque

_MAX_ACTIVE = 64
_MAX_FEED = 100
_MAX_DONE = 40


class LiveProgress:
    """Tracks running/finished subagents and a rolling feed of their tool calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_session: dict[str, dict] = {}     # subagent session_id -> active record
        self._active: "OrderedDict[str, dict]" = OrderedDict()  # subagent_id -> record
        self._feed: deque = deque(maxlen=_MAX_FEED)
        self._done: deque = deque(maxlen=_MAX_DONE)

    def subagent_started(self, subagent_id: str, session_id: str, goal: str = "", role: str = "") -> None:
        if not subagent_id:
            return
        rec = {
            "subagent_id": subagent_id,
            "session_id": session_id or "",
            "goal": goal or "",
            "role": role or "leaf",
            "started_at": time.time(),
            "status": "running",
            "tool_count": 0,
            "last_tool": "",
        }
        with self._lock:
            self._active[subagent_id] = rec
            if session_id:
                self._by_session[session_id] = rec
            while len(self._active) > _MAX_ACTIVE:
                self._active.popitem(last=False)

    def tool_event(self, session_id: str, tool_name: str, status: str = "", duration_ms: int = 0) -> None:
        with self._lock:
            rec = self._by_session.get(session_id)
            if rec is None:
                return  # not a tracked subagent (parent agent or unknown) — ignore
            rec["tool_count"] += 1
            rec["last_tool"] = tool_name or ""
            self._feed.append({
                "ts": time.time(),
                "subagent_id": rec["subagent_id"],
                "goal": rec["goal"][:70],
                "tool": tool_name or "",
                "status": status or "",
                "duration_ms": int(duration_ms or 0),
            })

    def subagent_stopped(self, session_id: str, summary: str = "",
                         status: str = "completed", duration_ms: int = 0) -> None:
        with self._lock:
            rec = self._by_session.pop(session_id, None)
            if rec is None:
                return
            self._active.pop(rec["subagent_id"], None)
            self._done.appendleft({
                "subagent_id": rec["subagent_id"],
                "goal": rec["goal"],
                "status": status or "completed",
                "summary": (summary or "")[:4000],
                "tool_count": rec["tool_count"],
                "duration_ms": int(duration_ms or 0),
                "ended_at": time.time(),
            })

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            active = [
                {**r, "elapsed_s": round(now - r["started_at"], 1)}
                for r in self._active.values()
            ]
            feed = list(self._feed)[-50:][::-1]   # most recent first
            done = list(self._done)[:20]
        return {"active": active, "feed": feed, "completed": done}

    def clear(self) -> None:  # used by tests
        with self._lock:
            self._by_session.clear()
            self._active.clear()
            self._feed.clear()
            self._done.clear()


# One process-wide instance: the plugin hooks and the in-process dashboard share it.
PROGRESS = LiveProgress()
