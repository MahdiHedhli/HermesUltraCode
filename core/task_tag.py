"""Task tagging: carry ``(provider-profile, tier[, model])`` on a Kanban task through a
structured ``kanban_comment`` line — the durable annotation channel the review pattern
already uses. The model decision rides the TASK, not the call, so the board carries it and
the cascade has a concrete thing to escalate against.

Prefer a native ``task.model`` (Hermes ``model_override``) when present; the comment is the
fallback until Part A lands. One serializer, one parser, round-trip tested. Pure + offline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

_TAG_PREFIX = "ultracode-route:"


class TagError(ValueError):
    """Raised when a tag line cannot be parsed."""


@dataclass(frozen=True)
class RouteTag:
    profile: str
    tier: str
    model: str | None = None


def serialize_tag(profile: str, tier: str, model: str | None = None) -> str:
    """Render a single, greppable comment line. Empty model is omitted (inherit default)."""
    if not profile or not tier:
        raise TagError("tag requires a profile and a tier")
    payload = {"profile": profile, "tier": tier}
    if model:
        payload["model"] = model
    return _TAG_PREFIX + json.dumps(payload, sort_keys=True, ensure_ascii=False)


def parse_tag(text: str, *, native_model: str | None = None) -> RouteTag | None:
    """Parse the LAST tag line in ``text`` (newest wins). Returns None when no tag is
    present. A native ``task.model`` (Hermes ``model_override``), when supplied, ALWAYS
    overrides the comment's model — the native field is authoritative."""
    if not text:
        return _native_only(native_model)
    line = None
    for candidate in str(text).splitlines():
        s = candidate.strip()
        if s.startswith(_TAG_PREFIX):
            line = s
    if line is None:
        return _native_only(native_model)
    try:
        payload = json.loads(line[len(_TAG_PREFIX):].strip())
    except (json.JSONDecodeError, ValueError) as exc:
        raise TagError(f"malformed route tag: {exc}") from exc
    if not isinstance(payload, dict) or not payload.get("profile") or not payload.get("tier"):
        raise TagError("route tag missing profile/tier")
    model = native_model or (payload.get("model") or None)
    return RouteTag(profile=str(payload["profile"]), tier=str(payload["tier"]),
                    model=(str(model) if model else None))


def _native_only(native_model: str | None) -> RouteTag | None:
    # No comment tag, but a native model override alone is not a route (no profile/tier).
    return None
