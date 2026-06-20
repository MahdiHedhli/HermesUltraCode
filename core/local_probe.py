"""A non-raising, TTL-cached liveness probe for a local OpenAI-compatible endpoint
(LM Studio default http://localhost:1234/v1; Ollama at :11434/v1). The router consults it
so it never 'routes' a task to a box that is down or has no model loaded.

stdlib only (urllib); NEVER raises — any failure is reported as not-alive, because local
unavailability must only change *who* would run a task, never block the dispatch.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request


class LocalProbe:
    def __init__(self, base_url: str, *, timeout: float = 1.5, ttl: float = 15.0,
                 clock=time.monotonic) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self._url = self.base_url + "/models"
        self.timeout = timeout
        self.ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._checked_at: float | None = None
        self._alive = False
        self._models: tuple[str, ...] = ()

    def alive(self) -> bool:
        """True if the endpoint answered /models within the TTL window. Cached so a busy
        router doesn't probe on every dispatch."""
        if not self.base_url:
            return False
        with self._lock:
            now = self._clock()
            if self._checked_at is not None and (now - self._checked_at) < self.ttl:
                return self._alive
            self._checked_at = now
            self._alive, self._models = self._probe()
            return self._alive

    @property
    def models(self) -> tuple[str, ...]:
        return self._models

    def _probe(self) -> tuple[bool, tuple[str, ...]]:
        try:
            req = urllib.request.Request(self._url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 (local URL)
                body = resp.read()
            data = json.loads(body)
            items = data.get("data", []) if isinstance(data, dict) else []
            models = tuple(str(m.get("id", "")) for m in items if isinstance(m, dict))
            return True, models
        except Exception:  # noqa: BLE001 - any failure => not alive, never raises
            return False, ()
