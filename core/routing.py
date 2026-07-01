"""Router: a deterministic TABLE lookup over the roster (invariant #3 — no LLM, no
per-provider manager agent, no in-process delegate swarm). Pure and offline: no Hermes
import, no network. It MATCHES an existing profile or fails closed (invariant #6 — never
creates one). Tier is a RISK floor (invariant #4); the working model within the floor is
decided by the cascade (invariant #5), not predicted here.

``resolve`` is a deliberate STUB until the Part A per-task ``model`` field merges upstream:
it returns the provider profile's default model (``tier`` ignored). The call path is the
same now and post-PR, so filling the body later is a swap, not a refactor.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.roster import ProviderProfile, Roster


class RoutingError(RuntimeError):
    """No candidate profile clears the tier's risk floor — routing fails closed."""


@dataclass(frozen=True)
class Route:
    profile: str
    lab: str
    tier: str


def route(tier: str, roster: Roster, *, budget_mode: str | None = None) -> Route:
    """Pick the cheapest candidate profile that clears ``tier``'s risk floor. The tier's
    ``routing`` list is authored cheapest-first. Under ``subscription-first`` a flat-rate
    seat among the candidates is preferred (its marginal cost is not tokens x price) until
    it rate-limits, at which point the caller drives ``escalate`` to spill. Fails closed
    when no candidate is declared for the tier."""
    mode = (budget_mode or roster.budget_mode or "").strip()
    candidates = _candidates(tier, roster)
    if not candidates:
        raise RoutingError(f"no candidate profile clears the {tier!r} risk floor")
    chosen = candidates[0]
    if mode == "subscription-first":
        chosen = next((p for p in candidates if p.flat_rate), candidates[0])
    return Route(profile=chosen.profile, lab=chosen.lab, tier=tier)


def escalate(prev: Route, tier: str, roster: Roster) -> Route | None:
    """The next-stronger candidate after ``prev`` in the tier's list, or None when
    exhausted. Drives the cascade on a VERIFIED failure (invariant #5) — never a
    pre-spawned insurance model."""
    candidates = _candidates(tier, roster)
    seen = False
    for p in candidates:
        if seen:
            return Route(profile=p.profile, lab=p.lab, tier=tier)
        if p.profile == prev.profile:
            seen = True
    return None


def resolve(provider: ProviderProfile, tier: str) -> str | None:
    """STUB (until Part A's per-task model field lands): return the provider profile's
    DEFAULT model; ``tier`` is ignored. ``None`` means "inherit the profile's own config
    default" (don't pass --model). Post-PR this becomes ``provider.tiers.get(tier)`` with a
    floor lookup — same signature, same callers, no refactor."""
    if provider is None:
        return None
    return provider.model or None


def _candidates(tier: str, roster: Roster) -> list[ProviderProfile]:
    names = roster.routing.get(tier, ())
    out = []
    for n in names:
        p = roster.provider(n)
        if p is not None:
            out.append(p)
    return out
