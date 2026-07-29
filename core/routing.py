"""Router: a deterministic TABLE lookup over the roster (invariant #3 — no LLM, no
per-provider manager agent, no in-process delegate swarm). Pure and offline: no Hermes
import, no network. It MATCHES an existing profile or fails closed (invariant #6 — never
creates one). Tier is a RISK floor (invariant #4); the working model within the floor is
decided by the cascade (invariant #5), not predicted here.

``resolve`` is LIVE (it was stubbed while a per-task model field was pending upstream;
upstream closed per-call/per-task selection as not-planned — see docs/upstream-routing.md —
and the ultracode lane binds ``-m`` at its own spawn, so the tier->model table applies today
with no upstream seam). ``next_rung`` is the cascade step: the same provider's stronger slot
first (model cascade), then the next candidate profile (provider cascade).
"""

from __future__ import annotations

from dataclasses import dataclass

from core.roster import ProviderProfile, Roster

# Risk tier -> the operator's slot name in a provider's `tiers:` mapping. Low-risk tiers
# draw the cheap slot; escalating tiers draw the heavy slot (the model floor rises with
# risk). A direct risk-tier key in `tiers:` (e.g. `standard: <slug>`) overrides the slot.
_TIER_SLOT = {
    "trivial": "cheap",
    "standard": "cheap",
    "elevated": "heavy",
    "merge_adjacent": "heavy",
}


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


def _slot_value(provider: ProviderProfile, key: str) -> str | None:
    """An operator-supplied model slug from the provider's ``tiers:`` mapping. ``<SLOT>``
    placeholders (an unfilled roster) read as absent — we never invent a model."""
    v = (provider.tiers or {}).get(key)
    v = str(v).strip() if v else ""
    return v if v and not v.startswith("<") else None


def resolve(provider: ProviderProfile, tier: str) -> str | None:
    """Tier-aware model lookup (the tier-to-model FLOOR): a direct risk-tier key in the
    provider's ``tiers:`` wins, then the cheap/heavy slot for the tier, then the pinned
    profile default. ``None`` means "inherit the profile's own config default" (the lane
    passes no ``-m``). Slugs are OPERATOR-supplied in the roster; never invented here."""
    if provider is None:
        return None
    return (_slot_value(provider, tier)
            or _slot_value(provider, _TIER_SLOT.get(tier, ""))
            or provider.model
            or None)


def next_rung(prev: Route, prev_model: str | None, tier: str,
              roster: Roster) -> tuple[Route, str | None] | None:
    """The next-stronger rung after a VERIFIED failure or a not-ready profile: first the
    SAME provider's heavy slot (model cascade within one provider — live now that the lane
    binds ``-m`` itself), then the next candidate profile with its resolved model (provider
    cascade). ``None`` when the tier's ladder is exhausted (the caller fails closed)."""
    prof = roster.provider(prev.profile)
    if prof is not None:
        heavy = _slot_value(prof, "heavy")
        if heavy and heavy != prev_model:
            return prev, heavy                      # step up within the provider first
    nxt = escalate(prev, tier, roster)
    if nxt is None:
        return None
    nprof = roster.provider(nxt.profile)
    return nxt, resolve(nprof, tier)


def _candidates(tier: str, roster: Roster) -> list[ProviderProfile]:
    names = roster.routing.get(tier, ())
    out = []
    for n in names:
        p = roster.provider(n)
        if p is not None:
            out.append(p)
    return out
