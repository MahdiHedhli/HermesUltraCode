"""`hermes ultracode-setup` — progressive, human, one-time. A single authenticated provider
is a FIRST-CLASS complete setup (orchestrator + worker + reviewer at once), never a degraded
form to work around. The reviewer mode is always resolved to an explicit, NAMED value and a
same-lab reviewer can never present as cross-lab (invariant #8).

The PLANNING logic here is pure and tested. The interactive driver only *sequences* Hermes's
own ``setup``/``auth`` (never a credential UI, invariant #10) and gates on a live auth check.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.roster import (
    REVIEWER_OFF,
    Endpoint,
    Roster,
    resolve_reviewer_mode,
)


@dataclass(frozen=True)
class ProviderChoice:
    profile: str
    provider: str
    lab: str
    model: str | None = None       # pinned default (local/aggregator)
    flat_rate: bool = False
    description: str = ""


def plan_roster(orchestrator: ProviderChoice, providers: list[ProviderChoice], *,
                reviewer_profile: str | None = None, reviewer_mode: str | None = None,
                budget_mode: str = "subscription-first") -> dict:
    """Build a roster dict from the wizard's choices. Resolves reviewer_mode explicitly
    (raising if cross_lab is requested for a same-lab reviewer). The returned dict is
    ``Roster.from_dict``-valid — a single-provider install yields a complete roster with
    reviewer null + reviewer_mode ``off``."""
    reviewer_ep = None
    if reviewer_profile:
        rp = next((p for p in providers if p.profile == reviewer_profile), None)
        reviewer_ep = Endpoint(reviewer_profile, rp.lab if rp else "")

    mode = resolve_reviewer_mode(Endpoint(orchestrator.profile, orchestrator.lab),
                                 reviewer_ep, requested=reviewer_mode)

    return {
        "orchestrator": {"profile": orchestrator.profile, "lab": orchestrator.lab},
        "reviewer": ({"profile": reviewer_ep.profile, "lab": reviewer_ep.lab}
                     if reviewer_ep else None),
        "reviewer_mode": mode,
        "budget_mode": budget_mode,
        "providers": [_provider_dict(p) for p in providers],
        "routing": default_routing(providers),
    }


def _provider_dict(p: ProviderChoice) -> dict:
    d = {"profile": p.profile, "provider": p.provider, "lab": p.lab, "flat_rate": p.flat_rate}
    if p.model:
        d["model"] = p.model
    if p.description:
        d["description"] = p.description
    return d


def default_routing(providers: list[ProviderChoice]) -> dict:
    """A safe, working default the operator refines. Local floors the low-risk tiers and is
    EXCLUDED from elevated/merge_adjacent (risk over cost). Tier->model SLOTS stay empty
    (operator-supplied) — this seeds only the candidate-profile ordering."""
    local = [p.profile for p in providers if p.lab == "local"]
    nonlocal_ = [p.profile for p in providers if p.lab != "local"]
    all_cheap_first = local + nonlocal_
    frontier = nonlocal_ or all_cheap_first          # elevated/merge floor; never local if others exist
    return {
        "trivial": all_cheap_first,
        "standard": all_cheap_first,
        "elevated": frontier,
        "merge_adjacent": frontier,
    }


def plan_and_validate(orchestrator: ProviderChoice, providers: list[ProviderChoice],
                      **kw) -> Roster:
    """Convenience: plan then load (raises RosterError if the plan is somehow invalid)."""
    return Roster.from_dict(plan_roster(orchestrator, providers, **kw))


# ---------------------------------------------------------------------------
# Interactive driver (lazy Hermes; never a credential UI)
# ---------------------------------------------------------------------------


def run_setup(write_path: str, *, io=None) -> None:  # pragma: no cover - interactive shell
    """Detect authenticated providers, ask which to use, resolve the reviewer mode, write
    roster.yaml, and reconcile. Credential wiring is delegated to ``hermes auth``/``hermes
    setup`` (shelled out), gated on a live auth check — this never prompts for a secret."""
    raise NotImplementedError(
        "run_setup is the interactive driver; wired in register() via ctx.register_cli_command. "
        "The tested logic is plan_roster/default_routing above."
    )
