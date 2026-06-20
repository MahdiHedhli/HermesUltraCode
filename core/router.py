"""Cost-aware model router (ADVISORY).

Given a released dispatch's blast-radius tier and an optional difficulty hint, pick the
cheapest catalog model that clears the required capability tier, strongly preferring a
local model whose marginal cost is ~0. Pure and deterministic. It runs ONLY after the gate
has already released a dispatch and it NEVER blocks — today it annotates the audit row with
the model it *would* pick and the dollars that choice would save (real binding waits on a
per-task model override landing in Hermes; see docs/upstream-routing.md).

Two safety rules are structural here:
  * **Risk overrides cost.** A local model is never eligible for an escalating-tier task
    (elevated / merge-authority), no matter how cheap.
  * **Difficulty only raises, never lowers.** required_tier = max(blast-radius floor,
    difficulty hint). A missing/garbage hint contributes nothing (the deterministic
    blast-radius tier is the floor), so a bad hint can neither fail-close nor under-route.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.catalog import CAP_MAX, CAP_MIN, CatalogEntry
from core.tiering import (
    ESCALATING_TIERS,
    TIER_ELEVATED,
    TIER_MERGE_ADJACENT,
    TIER_STANDARD,
    TIER_TRIVIAL,
)

TIER_TO_MIN_CAP: dict[str, int] = {
    TIER_TRIVIAL: 1,
    TIER_STANDARD: 2,
    TIER_ELEVATED: 3,
    TIER_MERGE_ADJACENT: 3,
}

# Default token estimate when the caller has no better number (in / out).
_DEFAULT_IN, _DEFAULT_OUT = 3000, 1500


@dataclass(frozen=True)
class RouterConfig:
    local_bias: float = 0.15        # (0,1]; lower => prefer local more aggressively
    box_watts: float = 120.0        # local box draw under load, for the electricity estimate
    usd_per_kwh: float = 0.20
    local_trusted_tier: int = 2     # capability ceiling for any local model
    default_api_worker: str = ""    # sanctioned fallback model id when nothing is eligible


@dataclass(frozen=True)
class RouteDecision:
    model_id: str
    lab: str
    is_local: bool
    required_tier: int
    reason: str                     # "local" | "api_cheapest" | "fallback_no_candidate"
    est_cost_usd: float             # real $ of the chosen model for this dispatch (0 for local)
    baseline_cloud_usd: float       # real $ of the cheapest eligible CLOUD model (savings denom)
    est_savings_usd: float          # max(0, baseline_cloud_usd - est_cost_usd)

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id, "lab": self.lab, "is_local": self.is_local,
            "required_tier": self.required_tier, "reason": self.reason,
            "est_cost_usd": round(self.est_cost_usd, 6),
            "baseline_cloud_usd": round(self.baseline_cloud_usd, 6),
            "est_savings_usd": round(self.est_savings_usd, 6),
        }


def required_tier(tier: str, difficulty: int = 0) -> int:
    """Capability floor a worker must clear: max of the blast-radius tier and the reviewer's
    difficulty hint. difficulty 0 (unspecified/garbage) contributes nothing, so the
    deterministic blast-radius tier is always the safety floor and a bad hint can only ever
    *raise* the bar, never lower it."""
    base = TIER_TO_MIN_CAP.get(tier, CAP_MAX)
    hint = difficulty if CAP_MIN <= difficulty <= CAP_MAX else 0
    return min(CAP_MAX, max(CAP_MIN, base, hint))


def real_cost_usd(m: CatalogEntry, est_in: int, est_out: int) -> float:
    """Actual marginal dollars for this dispatch. 0 for a local model."""
    if m.is_local:
        return 0.0
    return (est_in / 1e6) * m.usd_per_mtok_in + (est_out / 1e6) * m.usd_per_mtok_out


def effective_cost(m: CatalogEntry, est_in: int, est_out: int, cfg: RouterConfig) -> float:
    """The quantity the router MINIMIZES. API = real dollars. Local = a tiny electricity
    estimate scaled by local_bias, so a capable local model wins by default, yet a slow box
    on a huge job is not costed as exactly free."""
    if not m.is_local:
        return real_cost_usd(m, est_in, est_out)
    seconds = (est_in + est_out) / max(m.tok_per_s, 1e-6)
    c_elec = (seconds / 3600.0) * (cfg.box_watts / 1000.0) * cfg.usd_per_kwh
    return cfg.local_bias * c_elec


def choose_worker(
    tier: str,
    catalog: dict[str, CatalogEntry],
    *,
    difficulty: int = 0,
    est_in: int | None = None,
    est_out: int | None = None,
    local_alive: bool = True,
    cfg: RouterConfig | None = None,
) -> RouteDecision:
    """Pick the cheapest eligible model. Advisory: never blocks. Local is excluded for
    escalating tiers (risk > cost), when the box is not alive, or when its trusted ceiling
    is below the required tier."""
    cfg = cfg or RouterConfig()
    est_in = _DEFAULT_IN if est_in is None else int(est_in)
    est_out = _DEFAULT_OUT if est_out is None else int(est_out)
    ctx_need = est_in + est_out
    req = required_tier(tier, difficulty)
    risk_gated = tier in ESCALATING_TIERS

    candidates: list[CatalogEntry] = []
    for m in catalog.values():
        if m.trusted_tier(cfg.local_trusted_tier) < req:    continue   # capability floor
        if m.ctx < ctx_need:                                continue   # context window
        if m.is_local and (risk_gated or not local_alive):  continue   # risk + liveness gate
        candidates.append(m)

    cloud = [m for m in candidates if not m.is_local]
    baseline_cloud = min((real_cost_usd(m, est_in, est_out) for m in cloud), default=0.0)

    if not candidates:
        # Nothing eligible: name the sanctioned default if it's a REAL catalog model, else the
        # cheapest cloud model in the whole catalog (under-tier, flagged by the reason). Purely
        # advisory — the gate already released, so this can never block a dispatch.
        entry = catalog.get(cfg.default_api_worker)        # None if unset OR typo'd (N1)
        if entry is None:
            pool = [m for m in catalog.values() if not m.is_local] or list(catalog.values())
            entry = min(pool, key=lambda m: real_cost_usd(m, est_in, est_out)) if pool else None
        if entry is None:
            return RouteDecision("", "", False, req, "fallback_no_candidate", 0.0, baseline_cloud, 0.0)
        # Report the fallback's REAL cost (S1): an under-tier cloud fallback still spends dollars,
        # so a 0.0 here would silently under-count spend in the savings ledger.
        est = real_cost_usd(entry, est_in, est_out)
        return RouteDecision(entry.model_id, entry.lab, entry.is_local, req,
                             "fallback_no_candidate", est, baseline_cloud,
                             max(0.0, baseline_cloud - est))

    chosen = min(candidates, key=lambda m: effective_cost(m, est_in, est_out, cfg))
    est = real_cost_usd(chosen, est_in, est_out)
    reason = "local" if chosen.is_local else "api_cheapest"
    savings = max(0.0, baseline_cloud - est)
    return RouteDecision(chosen.model_id, chosen.lab, chosen.is_local, req, reason,
                         est, baseline_cloud, savings)
